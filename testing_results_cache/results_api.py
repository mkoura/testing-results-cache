import hashlib
import random
import string
from pathlib import Path
from typing import List
from typing import Optional
from typing import Set

import flask
from werkzeug import security

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import junittools
from testing_results_cache import results_cache
from testing_results_cache import users


ALLOWED_EXTENSIONS = {".xml"}

results = flask.Blueprint("results", __name__)


def checksum(filename: Path, blocksize: int = 65536) -> str:
    """Return file checksum."""
    hash_o = hashlib.sha1()
    with open(filename, "rb") as f:
        for block in iter(lambda: f.read(blocksize), b""):
            hash_o.update(block)
    return hash_o.hexdigest()


def allowed_file(filename: Path) -> bool:
    return filename.suffix.lower() in ALLOWED_EXTENSIONS


def get_passed(tests_verdicts: List[common.TestVerdict]) -> Set[str]:
    """Get tests that already passed."""
    passed = {
        r.testid
        for r in tests_verdicts
        if r.verdict in [common.VerdictValues.PASSED, common.VerdictValues.XFAILED]
    }
    return passed


def get_nonpassed(tests_verdicts: List[common.TestVerdict]) -> Set[str]:
    """Get tests that haven't passed yet."""
    passed = {
        r.testid
        for r in tests_verdicts
        if r.verdict in [common.VerdictValues.PASSED, common.VerdictValues.XFAILED]
    }
    all_tests = {r.testid for r in tests_verdicts}
    return all_tests - passed


@flask_auth.auth.verify_password
def verify_password(username: str, password: str) -> Optional[dict]:
    conn = flask_db.get_db()
    user_id, db_password = users.get_user(conn=conn, user_name=username)
    if user_id == -1:
        return None

    if security.check_password_hash(db_password, password):
        return {"user_id": user_id, "username": username}

    return None


def import_testrun(junit_file: Path, testrun_name: str, user_id: int) -> int:
    """Import a testrun to db from a JUnit XML file."""
    testsuite_data = junittools.get_testsuite_data(junit_file=junit_file)

    conn = flask_db.get_db()
    testrun_id = results_cache.save_testrun(
        conn=conn, testrun_name=testrun_name, user_id=user_id, testsuite_data=testsuite_data
    )
    return testrun_id


@results.route("/results/<testrun_name>/<job_id>/import", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def import_results(testrun_name: str, job_id: str) -> dict:
    """Upload a JUnit XML file for a given testrun."""
    if "junitxml" not in flask.request.files:
        response = flask.jsonify(message="No file part")
        response.status_code = 400
        flask.abort(response)

    file = flask.request.files["junitxml"]
    if file.filename == "":
        response = flask.jsonify(message="No selected file")
        response.status_code = 400
        flask.abort(response)

    if not (file and allowed_file(Path(file.filename))):
        response = flask.jsonify(message="Unexpected file type")
        response.status_code = 400
        flask.abort(response)

    upload_folder = Path(flask.current_app.config["UPLOAD_FOLDER"])

    # enable multiple uploads for the same testrun and job, because the same job can be re-run
    rand_str = "".join(random.choice(string.ascii_lowercase) for __ in range(5))
    upload_filepath = upload_folder / testrun_name / job_id / f"upload-{rand_str}.xml"

    upload_filepath.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(upload_filepath))

    file_checksum = checksum(upload_filepath)
    filepath = upload_folder / testrun_name / job_id / f"{file_checksum}.xml"
    if filepath.exists():
        upload_filepath.unlink()
        response = flask.jsonify(message="File was already uploaded")
        response.status_code = 400
        flask.abort(response)

    upload_filepath.rename(filepath)

    try:
        testrun_id = import_testrun(
            junit_file=filepath,
            testrun_name=testrun_name,
            user_id=flask_auth.auth.current_user()["user_id"],
        )
    except ValueError:
        flask.current_app.logger.exception(f"Failed to import testrun '{testrun_name}'")
        filepath.unlink()
        response = flask.jsonify(message="Failed to import testrun")
        response.status_code = 400
        flask.abort(response)

    return {
        "junitxml": f"{upload_folder.name}/{testrun_name}/{job_id}/{file_checksum}.xml",
        "testrun_id": testrun_id,
    }


def _get_passed_common(testrun_name: str) -> List[str]:
    """Get tests that already passed."""
    conn = flask_db.get_db()
    tests_verdicts = results_cache.load_testrun(
        conn=conn, testrun_name=testrun_name, user_id=flask_auth.auth.current_user()["user_id"]
    )
    tests = sorted(get_passed(tests_verdicts=tests_verdicts))
    return tests


def _get_nonpassed_common(testrun_name: str) -> List[str]:
    """Get tests that haven't passed yet."""
    conn = flask_db.get_db()
    tests_verdicts = results_cache.load_testrun(
        conn=conn, testrun_name=testrun_name, user_id=flask_auth.auth.current_user()["user_id"]
    )
    tests = sorted(get_nonpassed(tests_verdicts=tests_verdicts))
    return tests


def _pytestify(tests: List[str]) -> List[str]:
    """Reformat test names to pytest nodeid format.

    Pytest nodeid format: "path/to/test_file.py::TestClass::test_name"
    """
    nodeids = []
    for t in tests:
        classname, title = t.split("::")
        classparts = classname.split(".")

        # e.g. find "test_lobster" in
        # cardano_node_tests.tests.test_plutus.test_lobster.TestLobsterChallenge
        test_idx = -1
        for i, p in enumerate(classparts):
            if p.startswith("test_"):
                test_idx = i

        if test_idx == -1:
            flask.current_app.logger.warning(f"Cannot find test file in {t}")
            continue

        file_class_parts = [f"{classparts[test_idx]}.py"] + classparts[test_idx + 1 :]
        if len(file_class_parts) > 2:
            flask.current_app.logger.warning(f"Unexpected test name {t}")
            continue

        file_class = "::".join(file_class_parts)
        node_path = "/".join(classparts[:test_idx])
        if node_path:
            node_path += "/"
        nodeid = f"{node_path}{file_class}::{title}"
        nodeids.append(nodeid)

    return nodeids


def _tests_response(tests: List[str]) -> flask.Response:
    tests_str = "\n".join(tests)

    response = flask.make_response(tests_str, 200)
    response.mimetype = "text/plain"
    return response


@results.route("/results/<testrun_name>/passed", methods=["GET"])
@flask_auth.auth.login_required
def get_passed_api(testrun_name: str) -> flask.Response:
    """Get tests that already passed."""
    tests = _get_passed_common(testrun_name=testrun_name)
    return _tests_response(tests=tests)


@results.route("/results/<testrun_name>/pypassed", methods=["GET"])
@flask_auth.auth.login_required
def get_pypassed_api(testrun_name: str) -> flask.Response:
    """Get tests that already passed - pytest nodeid format."""
    tests = _pytestify(_get_passed_common(testrun_name=testrun_name))
    return _tests_response(tests=tests)


@results.route("/results/<testrun_name>/rerun", methods=["GET"])
@flask_auth.auth.login_required
def get_nonpassed_api(testrun_name: str) -> flask.Response:
    """Get tests haven't passed yet."""
    tests = _get_nonpassed_common(testrun_name=testrun_name)
    return _tests_response(tests=tests)


@results.route("/results/<testrun_name>/pyrerun", methods=["GET"])
@flask_auth.auth.login_required
def get_pynonpassed_api(testrun_name: str) -> flask.Response:
    """Get tests that haven't passed yet - pytest nodeid format."""
    tests = _pytestify(_get_nonpassed_common(testrun_name=testrun_name))
    return _tests_response(tests=tests)
