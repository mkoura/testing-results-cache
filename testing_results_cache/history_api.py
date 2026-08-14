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

import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import List
from typing import NoReturn

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import history_cache

DEFAULT_HISTORY_DAYS = 5
MAX_HISTORY_DAYS = 3650
MAX_PATH_SEGMENT_LENGTH = 200
# Dots are allowed so real-world testrun names like "node-8.5.0" work, but a
# segment of dots only ("..", ".") is rejected in `_valid_path_segment`.
_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")

history = flask.Blueprint("history", __name__)


def _abort_json(status_code: int, message: str) -> NoReturn:
    """Abort the request with a JSON error body."""
    response = flask.jsonify(message=message)
    response.status_code = status_code
    flask.abort(response)


def _valid_path_segment(value: str) -> bool:
    # fullmatch, not match: `$` in a pattern would still accept a trailing
    # newline ("job1%0A" in the URL), fullmatch requires the whole string.
    return (
        len(value) <= MAX_PATH_SEGMENT_LENGTH
        and _SAFE_SEGMENT_RE.fullmatch(value) is not None
        and value.strip(".") != ""
    )


def _reject_invalid_segments(*values: str) -> None:
    for value in values:
        if not _valid_path_segment(value):
            _abort_json(
                400,
                f"Invalid path segment {value!r}: only [A-Za-z0-9_.-] "
                f"(not dots alone), max {MAX_PATH_SEGMENT_LENGTH} chars",
            )


def _history_file(testrun_name: str, job_id: str) -> Path:
    history_folder = Path(flask.current_app.config["HISTORY_FOLDER"])
    return history_folder / testrun_name / f"{job_id}.xml"


@history.route("/history/<testrun_name>/<job_id>", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_history(testrun_name: str, job_id: str) -> dict:
    """Dump the raw JUnit XML for a testrun+job. No parsing, just storage."""
    _reject_invalid_segments(testrun_name, job_id)

    if "junitxml" not in flask.request.files:
        _abort_json(400, "No file part")

    file = flask.request.files["junitxml"]
    # `not file.filename` also covers None (malformed multipart part).
    if not file.filename or Path(file.filename).suffix.lower() not in common.ALLOWED_EXTENSIONS:
        _abort_json(400, "Unexpected file type")

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]
    filepath = _history_file(testrun_name=testrun_name, job_id=job_id)

    # mkstemp, not a random-suffix name: its O_EXCL creation guarantees each
    # request its own file (a colliding candidate name is detected and
    # skipped, never opened) instead of silently storing one job's XML under
    # another's job_id (preforked WSGI workers can share PRNG state, so
    # "random" suffixes do collide in practice). Side effect: mkstemp
    # creates the file 0600, so stored XML ends up owner-only rather than
    # 0644 - fine, this service serves it itself via send_file.
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=filepath.parent, prefix=".upload-", suffix=".tmp")
        os.close(tmp_fd)
    except OSError:
        flask.current_app.logger.exception(
            f"History storage unavailable for {testrun_name}/{job_id}"
        )
        _abort_json(500, "Failed to store history XML")
    tmp_filepath = Path(tmp_name)

    # The row is inserted first but committed only after the XML is renamed
    # into place, so neither a handled failure nor a process crash can leave
    # a committed row without a readable file - that would wedge this
    # testrun+job forever behind the UNIQUE constraint. A crash or a failed
    # commit leaves at worst an orphaned file (final path or .tmp); a
    # retry's rename simply overwrites the final-path one. The
    # same-directory rename also means a torn write never occupies the
    # final path. (A power loss right after the commit can still lose the
    # un-fsynced file - accepted, not worth the fsync.)
    try:
        file.save(str(tmp_filepath))
        saved = history_cache.save_history_entry(
            conn=conn, testrun_name=testrun_name, job_id=job_id, user_id=user_id
        )
        if saved:
            tmp_filepath.rename(filepath)
            conn.commit()
        else:
            conn.rollback()
    except HTTPException:
        # Never swallow an intentional abort into the generic 500 below.
        raise
    except Exception:
        flask.current_app.logger.exception(
            f"Failed to store history XML for {testrun_name}/{job_id}"
        )
        # The insert is uncommitted, so even a failing rollback cannot leave
        # a row behind - connection teardown discards it. Still worth a log:
        # a rollback that raises means a sick connection or disk.
        try:
            conn.rollback()
        except sqlite3.Error:
            flask.current_app.logger.warning(
                f"Rollback failed for history upload {testrun_name}/{job_id}", exc_info=True
            )
        _abort_json(500, "Failed to store history XML")
    finally:
        # Janitorial only - never mask the primary outcome with an unlink
        # error. But log it: `missing_ok` already covers the renamed-away
        # success path, so any OSError here is real storage trouble and a
        # temp file orphaned in HISTORY_FOLDER.
        try:
            tmp_filepath.unlink(missing_ok=True)
        except OSError:
            flask.current_app.logger.warning(
                f"Could not remove temp upload file {tmp_filepath} for {testrun_name}/{job_id}",
                exc_info=True,
            )

    if not saved:
        _abort_json(400, "History already recorded for this testrun and job")

    return {"history": f"{testrun_name}/{job_id}"}


@history.route("/history/<testrun_name>", methods=["GET"])
@flask_auth.auth.login_required
def list_history(testrun_name: str) -> List[dict]:
    """List history entries for a testrun within a recent window of days."""
    _reject_invalid_segments(testrun_name)

    # Parse explicitly - `args.get(..., type=int)` silently swallows the
    # ValueError on e.g. `?days=abc` and returns the default, and a typo
    # must be a 400, not a plausible-but-wrong 5-day window.
    raw_days = flask.request.args.get("days")
    try:
        days = DEFAULT_HISTORY_DAYS if raw_days is None else int(raw_days)
    except ValueError:
        _abort_json(400, "Invalid 'days' parameter")
    if not 0 < days <= MAX_HISTORY_DAYS:
        _abort_json(400, "Invalid 'days' parameter")

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
        _abort_json(404, "No history found for this testrun and job")

    filepath = _history_file(testrun_name=testrun_name, job_id=job_id)
    if not filepath.is_file():
        # DB says yes, disk says no - that's server-side data loss (files
        # deleted under HISTORY_FOLDER?), not a client error. Log it loudly;
        # the 404 alone would hide the distinction from the operator.
        flask.current_app.logger.error(
            f"History DB row exists for {testrun_name}/{job_id} but file {filepath} is missing"
        )
        _abort_json(404, "No history found for this testrun and job")

    return flask.send_file(filepath, mimetype="application/xml")
