"""Endpoints for dumping and retrieving raw JUnit XML for nightly test runs.

This is a deliberately separate use case from `/results/.../import`: no
parsing, no verdicts, just storing whatever XML the caller sends and
handing it back by job_id or by a recent time window. CI decides when to
call this (e.g. only on a failed nightly) - this service doesn't need to
know why.

Any logged-in user can list or fetch any testrun's history - job_id/
testrun_name is the identity that matters, not who uploaded it. This is
one team's data, not separate tenants, so there's no ownership check here
beyond being a valid login.

Mounted at `/history/...`, not `/results/history/...`, so a testrun
literally named "history" can never collide with these routes.
"""

import re
from pathlib import Path
from typing import List

import flask

from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import history_cache

ALLOWED_EXTENSIONS = {".xml"}
DEFAULT_HISTORY_DAYS = 5
MAX_HISTORY_DAYS = 3650
MAX_PATH_SEGMENT_LENGTH = 200
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

history = flask.Blueprint("history", __name__)


def _valid_path_segment(value: str) -> bool:
    return len(value) <= MAX_PATH_SEGMENT_LENGTH and _SAFE_SEGMENT_RE.match(value) is not None


def _reject_invalid_segments(*values: str) -> None:
    if not all(_valid_path_segment(v) for v in values):
        response = flask.jsonify(message="Invalid testrun_name or job_id")
        response.status_code = 400
        flask.abort(response)


def _history_file(history_folder: Path, testrun_name: str, job_id: str) -> Path:
    return history_folder / testrun_name / f"{job_id}.xml"


@history.route("/history/<testrun_name>/<job_id>", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_history(testrun_name: str, job_id: str) -> dict:
    """Dump the raw JUnit XML for a testrun+job. No parsing, just storage."""
    _reject_invalid_segments(testrun_name, job_id)

    if "junitxml" not in flask.request.files:
        response = flask.jsonify(message="No file part")
        response.status_code = 400
        flask.abort(response)

    file = flask.request.files["junitxml"]
    if file.filename == "" or Path(file.filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        response = flask.jsonify(message="Unexpected file type")
        response.status_code = 400
        flask.abort(response)

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]

    saved = history_cache.save_history_entry(
        conn=conn, testrun_name=testrun_name, job_id=job_id, user_id=user_id
    )
    if not saved:
        response = flask.jsonify(message="History already recorded for this testrun and job")
        response.status_code = 400
        flask.abort(response)

    history_folder = Path(flask.current_app.config["HISTORY_FOLDER"])
    filepath = _history_file(
        history_folder=history_folder, testrun_name=testrun_name, job_id=job_id
    )
    filepath.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(filepath))

    return {"history": f"{testrun_name}/{job_id}"}


@history.route("/history/<testrun_name>", methods=["GET"])
@flask_auth.auth.login_required
def list_history(testrun_name: str) -> List[dict]:
    """List history entries for a testrun within a recent window of days."""
    _reject_invalid_segments(testrun_name)

    days = flask.request.args.get("days", default=DEFAULT_HISTORY_DAYS, type=int)
    if not days or not 0 < days <= MAX_HISTORY_DAYS:
        response = flask.jsonify(message="Invalid 'days' parameter")
        response.status_code = 400
        flask.abort(response)

    conn = flask_db.get_db()
    entries = history_cache.get_history_entries(conn=conn, testrun_name=testrun_name, days=days)
    return [{"job_id": e.job_id, "timestamp": e.timestamp.isoformat()} for e in entries]


@history.route("/history/<testrun_name>/<job_id>/xml", methods=["GET"])
@flask_auth.auth.login_required
def get_history_xml(testrun_name: str, job_id: str) -> flask.Response:
    """Download the raw JUnit XML for a specific testrun+job."""
    _reject_invalid_segments(testrun_name, job_id)

    conn = flask_db.get_db()
    exists = history_cache.history_entry_exists(conn=conn, testrun_name=testrun_name, job_id=job_id)
    if not exists:
        response = flask.jsonify(message="No history found for this testrun and job")
        response.status_code = 404
        flask.abort(response)

    history_folder = Path(flask.current_app.config["HISTORY_FOLDER"])
    filepath = _history_file(
        history_folder=history_folder, testrun_name=testrun_name, job_id=job_id
    )
    if not filepath.is_file():
        response = flask.jsonify(message="No history found for this testrun and job")
        response.status_code = 404
        flask.abort(response)

    return flask.send_file(filepath, mimetype="application/xml")
