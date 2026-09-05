"""Endpoints for storing and retrieving the cached sync-test-results zip.

Deliberately separate from /history: /history keeps one entry per
testrun+job forever, appending. This endpoint keeps at most one entry per
cardano-node version, and a new upload for a version replaces whatever was
stored for it before. No parsing of the zip's contents happens here - the
caller (cardano-sync-tests CI) decides what goes inside, this service only
stores and hands it back.

Mounted at `/sync-results/...`, its own top-level prefix, so a version
string can never collide with a route under `/results/` or `/history/`.
"""

import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import List
from typing import NoReturn

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import sync_results_cache

MAX_PATH_SEGMENT_LENGTH = 200
# Dots are allowed so real-world version strings like "11.1.0" work, but a
# segment of dots only ("..", ".") is rejected in `_valid_path_segment`.
_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")

sync_results = flask.Blueprint("sync_results", __name__)


def _abort_json(status_code: int, message: str) -> NoReturn:
    """Abort the request with a JSON error body."""
    response = flask.jsonify(message=message)
    response.status_code = status_code
    flask.abort(response)


def _valid_path_segment(value: str) -> bool:
    # fullmatch, not match: `$` in a pattern would still accept a trailing
    # newline ("11.1.0%0A" in the URL), fullmatch requires the whole string.
    return (
        len(value) <= MAX_PATH_SEGMENT_LENGTH
        and _SAFE_SEGMENT_RE.fullmatch(value) is not None
        and value.strip(".") != ""
    )


def _reject_invalid_version(version: str) -> None:
    if not _valid_path_segment(version):
        _abort_json(
            400,
            f"Invalid path segment {version!r}: only [A-Za-z0-9_.-] "
            f"(not dots alone), max {MAX_PATH_SEGMENT_LENGTH} chars",
        )


def _sync_results_file(version: str) -> Path:
    folder = Path(flask.current_app.config["SYNC_RESULTS_FOLDER"])
    return folder / f"{version}.zip"


@sync_results.route("/sync-results/<version>", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_sync_results(version: str) -> dict:
    """Store the sync-results zip for a version, replacing any prior upload."""
    _reject_invalid_version(version)

    if "syncresults" not in flask.request.files:
        _abort_json(400, "No file part")

    file = flask.request.files["syncresults"]
    # `not file.filename` also covers None (malformed multipart part).
    if (
        not file.filename
        or Path(file.filename).suffix.lower() not in common.ALLOWED_SYNC_RESULTS_EXTENSIONS
    ):
        _abort_json(400, "Unexpected file type")

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]
    filepath = _sync_results_file(version=version)

    # mkstemp, not a random-suffix name: its O_EXCL creation guarantees each
    # request its own file instead of silently colliding with a concurrent
    # upload for a different version (preforked WSGI workers can share PRNG
    # state, so "random" suffixes do collide in practice).
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=filepath.parent, prefix=".upload-", suffix=".tmp")
        os.close(tmp_fd)
    except OSError:
        flask.current_app.logger.exception(f"Sync-results storage unavailable for {version}")
        _abort_json(500, "Failed to store sync results")
    tmp_filepath = Path(tmp_name)

    # There is no reject-as-duplicate fallback here like /history has - a
    # bad upload would overwrite and destroy the last good one for this
    # version, with no way back. So the content is validated (non-empty,
    # actually a zip) before either the DB row or the real file is ever
    # touched, and the DB write only commits after the real rename
    # succeeds, so a crash or a failed rename can never leave a committed
    # row pointing at a missing/stale file.
    try:
        file.save(str(tmp_filepath))

        if tmp_filepath.stat().st_size == 0:
            _abort_json(400, "Empty file")
        if not zipfile.is_zipfile(tmp_filepath):
            _abort_json(400, "Not a valid zip file")

        sync_results_cache.save_sync_results_entry(conn=conn, version=version, user_id=user_id)
        tmp_filepath.rename(filepath)
        conn.commit()
    except HTTPException:
        # Never swallow an intentional abort into the generic 500 below.
        raise
    except Exception:
        flask.current_app.logger.exception(f"Failed to store sync results for {version}")
        try:
            conn.rollback()
        except sqlite3.Error:
            flask.current_app.logger.warning(
                f"Rollback failed for sync-results upload {version}", exc_info=True
            )
        _abort_json(500, "Failed to store sync results")
    finally:
        # Janitorial only - never mask the primary outcome with an unlink
        # error. But log it: a successful rename already moved the file
        # away, so any OSError here on a validation-rejected upload is real
        # storage trouble, not the expected case.
        try:
            tmp_filepath.unlink(missing_ok=True)
        except OSError:
            flask.current_app.logger.warning(
                f"Could not remove temp upload file {tmp_filepath} for {version}", exc_info=True
            )

    return {"sync_results": version}


@sync_results.route("/sync-results", methods=["GET"])
@flask_auth.auth.login_required
def list_sync_results() -> List[dict]:
    """List every version that currently has a stored sync-results zip."""
    conn = flask_db.get_db()
    entries = sync_results_cache.list_sync_results(conn=conn)
    return [{"version": e.version, "timestamp": e.timestamp.isoformat()} for e in entries]


@sync_results.route("/sync-results/<version>/zip", methods=["GET"])
@flask_auth.auth.login_required
def get_sync_results_zip(version: str) -> flask.Response:
    """Download the stored sync-results zip for a version."""
    _reject_invalid_version(version)

    conn = flask_db.get_db()
    if not sync_results_cache.sync_results_exists(conn=conn, version=version):
        _abort_json(404, "No sync results found for this version")

    filepath = _sync_results_file(version=version)
    if not filepath.is_file():
        # DB says yes, disk says no - that's server-side data loss, not a
        # client error. Log it loudly; the 404 alone would hide the
        # distinction from the operator.
        flask.current_app.logger.error(
            f"Sync-results DB row exists for {version} but file {filepath} is missing"
        )
        _abort_json(404, "No sync results found for this version")

    return flask.send_file(filepath, mimetype="application/zip")
