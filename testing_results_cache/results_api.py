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


def allowed_file(filename: Path) -> bool:
    return filename.suffix.lower() in ALLOWED_EXTENSIONS


def get_passed(tests_verdicts: List[common.TestVerdict]) -> Set[str]:
    """Get tests that already passed."""
    passed = {r.testid for r in tests_verdicts if r.verdict == common.VerdictValues.PASSED}
    return passed


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
        flask.abort(400, "No file part")

    file = flask.request.files["junitxml"]
    if file.filename == "":
        flask.abort(400, "No selected file")

    if not (file and allowed_file(Path(file.filename))):
        flask.abort(400, "Unexpected file type")

    upload_folder = Path(flask.current_app.config["UPLOAD_FOLDER"])
    filepath = upload_folder / testrun_name / job_id / "junit.xml"
    if filepath.exists():
        flask.abort(400, "File already exists")

    filepath.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(filepath))

    testrun_id = import_testrun(
        junit_file=filepath,
        testrun_name=testrun_name,
        user_id=flask_auth.auth.current_user()["user_id"],
    )

    return {
        "junitxml": f"{upload_folder.name}/{testrun_name}/{job_id}/junit.xml",
        "testrun_id": testrun_id,
    }


def _get_passed_common(testrun_name: str) -> List[str]:
    """Get tests that already passed."""
    conn = flask_db.get_db()
    tests_verdicts = results_cache.load_testrun(
        conn=conn, testrun_name=testrun_name, user_id=flask_auth.auth.current_user()["user_id"]
    )
    passed_tests = sorted(get_passed(tests_verdicts=tests_verdicts))
    return passed_tests


def _pytestify(tests: List[str]) -> List[str]:
    """Reformat test names to pytest nodeid format."""
    nodeids = []
    for t in tests:
        classname, title = t.split("::")
        classparts = classname.split(".")

        test_idx = -1
        for i, p in enumerate(classparts):
            if p.startswith("test_"):
                test_idx = i
                break
        else:
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


@results.route("/results/<testrun_name>/passed", methods=["GET"])
@flask_auth.auth.login_required
def get_passed_api(testrun_name: str) -> flask.Response:
    """Get tests that already passed."""
    passed_tests = _get_passed_common(testrun_name=testrun_name)
    passed_tests_str = "\n".join(passed_tests)

    response = flask.make_response(passed_tests_str, 200)
    response.mimetype = "text/plain"
    return response


@results.route("/results/<testrun_name>/pypassed", methods=["GET"])
@flask_auth.auth.login_required
def get_pypassed_api(testrun_name: str) -> flask.Response:
    """Get tests that already passed in pytest nodeid format."""
    passed_tests = _pytestify(_get_passed_common(testrun_name=testrun_name))
    passed_tests_str = "\n".join(passed_tests)

    response = flask.make_response(passed_tests_str, 200)
    response.mimetype = "text/plain"
    return response
