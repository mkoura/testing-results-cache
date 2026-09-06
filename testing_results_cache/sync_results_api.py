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
import sqlite3
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import List
from typing import NoReturn

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import sync_results_cache

# A real sync-results bundle (JSON metrics plus a handful of PNGs) is a few
# MB uncompressed at most. This is a generous ceiling on top of that, not a
# tight one - it exists only to bound the cost of `_valid_zip` decompressing
# a crafted archive that stays under MAX_CONTENT_LENGTH on disk but expands
# to gigabytes (a "zip bomb").
MAX_UNCOMPRESSED_BYTES = 200 * 1000 * 1000
# A real bundle has a handful of members (the JSON plus a few PNGs). This
# bounds a different zip-bomb shape than the byte cap above: many small or
# empty members, each cheap on its own but costly in aggregate for testzip()
# to iterate (180,000 empty members measured at ~1.5s of CPU).
MAX_ZIP_MEMBERS = 1000

sync_results = flask.Blueprint("sync_results", __name__)


def _valid_zip(path: Path) -> bool:
    """Check the file is a real, readable zip, not just an EOCD signature.

    zipfile.is_zipfile() alone only scans for the end-of-central-directory
    signature - a truncated or CRC-corrupted zip can still pass it. There
    is no reject-as-duplicate fallback here (see the module docstring), so
    a corrupt-but-EOCD-shaped upload would otherwise silently overwrite a
    good stored zip.

    The declared uncompressed size and member count are both checked before
    testzip() decompresses anything, so a zip bomb (tiny on disk, huge once
    inflated, or with a huge number of trivial members) is rejected without
    paying for the inflate. testzip() also raises RuntimeError for an
    encrypted member, NotImplementedError for an unsupported compression
    method, and UnicodeDecodeError for a non-ASCII filename with invalid
    UTF-8 bytes - none of these are a corruption exactly, but all three mean
    this service cannot read the file back either, so they are treated the
    same as "not a valid zip" here.

    OSError is deliberately NOT caught here: a real disk fault (as opposed
    to bad zip content) can also raise it, and misclassifying that as the
    caller's fault would answer 400 for a server-side problem - it should
    propagate and become the generic 500 in upload_sync_results instead.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos or len(infos) > MAX_ZIP_MEMBERS:
                return False
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                return False
            return zf.testzip() is None
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        NotImplementedError,
        UnicodeDecodeError,
    ):
        return False


def _sync_results_file(version: str) -> Path:
    folder = Path(flask.current_app.config["SYNC_RESULTS_FOLDER"])
    return folder / f"{version}.zip"


def _rollback(conn: sqlite3.Connection, version: str) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        flask.current_app.logger.warning(
            f"Rollback failed for sync-results upload {version}", exc_info=True
        )


def _abort_storage_failure(version: str) -> NoReturn:
    flask.current_app.logger.exception(f"Failed to store sync results for {version}")
    common.abort_json(500, "Failed to store sync results")


def _abort_read_failure(context: str) -> NoReturn:
    # Only DB errors land here: the upload route already has its own
    # sqlite3.OperationalError handling for lock contention, but the two GET
    # routes had none - an unrun migration surfaced as an unhandled
    # exception, breaking the JSON-error contract every other response on
    # this blueprint keeps (Werkzeug's default HTML page, no logging).
    flask.current_app.logger.exception(f"Failed to read sync results ({context})")
    common.abort_json(500, "Failed to read sync results")


def _finalize_upload_disk_state(
    version: str,
    filepath: Path,
    prev_filepath: Path,
    committed: bool,
    entered_disk_phase: bool,
    had_previous_file: bool,
    previous_backed_up: bool,
) -> None:
    """Reconcile disk state with whether the DB commit actually landed.

    The rename can land before the commit that's meant to confirm it fails
    (a concurrent reader's transaction can block just the commit, not the
    earlier insert or the rename - see the module docstring). Left alone,
    that would mean the previous good zip is gone even though the upload
    that replaced it was never actually recorded.

    `entered_disk_phase` gates all of this: a failure before this point
    (bad content, a lock blocking the insert itself) never touched
    `filepath` at all, so `had_previous_file`/`previous_backed_up` are just
    unset defaults, not real information - acting on them here would delete
    a good file that was never actually touched.
    """
    if not entered_disk_phase:
        return
    if committed:
        if previous_backed_up:
            try:
                prev_filepath.unlink(missing_ok=True)
            except OSError:
                flask.current_app.logger.warning(
                    f"Could not remove backup zip for {version} after a successful upload",
                    exc_info=True,
                )
        return
    if previous_backed_up:
        try:
            prev_filepath.replace(filepath)
        except OSError:
            flask.current_app.logger.warning(
                f"Could not restore previous sync-results zip for {version} after a "
                "failed upload - it may now be missing its zip",
                exc_info=True,
            )
    elif not had_previous_file:
        # Brand-new version: if the rename landed before the commit failed,
        # there is now a file with no matching row. Remove it - if the
        # rename never landed, this is a harmless no-op.
        try:
            filepath.unlink(missing_ok=True)
        except OSError:
            flask.current_app.logger.warning(
                f"Could not remove orphaned sync-results zip for {version} after a failed upload",
                exc_info=True,
            )
    # else: had_previous_file is True but previous_backed_up is False, meaning
    # the backup itself failed (replace() raised) before ever reaching the
    # rename. filepath still holds the original good file untouched - a
    # rename/replace is atomic, so there is no partial state to clean up.


def _fsync_path(path: Path) -> None:
    """Flush a just-written file's data to disk before it gets renamed into place."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reject_invalid_zip_content(version: str, path: Path) -> None:
    # Logged at warning, not just returned to the caller: this check is the
    # endpoint's whole safety story (reject bad input rather than risk
    # overwriting good data), so an operator should be able to see it firing.
    if path.stat().st_size == 0:
        flask.current_app.logger.warning(f"Rejected empty sync-results upload for {version}")
        common.abort_json(400, "Empty file")
    if not _valid_zip(path):
        flask.current_app.logger.warning(f"Rejected invalid sync-results zip for {version}")
        common.abort_json(400, "Not a valid zip file")


@sync_results.route("/sync-results/<version>", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_sync_results(version: str) -> dict:
    """Store the sync-results zip for a version, replacing any prior upload."""
    common.reject_invalid_segments(version)

    if "syncresults" not in flask.request.files:
        common.abort_json(400, "No file part")

    file = flask.request.files["syncresults"]
    # `not file.filename` also covers None (malformed multipart part).
    if (
        not file.filename
        or Path(file.filename).suffix.lower() not in common.ALLOWED_SYNC_RESULTS_EXTENSIONS
    ):
        common.abort_json(400, "Unexpected file type")

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
        common.abort_json(500, "Failed to store sync results")
    tmp_filepath = Path(tmp_name)
    prev_filepath = filepath.with_name(filepath.name + ".prev")

    # There is no reject-as-duplicate fallback here like /history has - a
    # bad upload would overwrite and destroy the last good one for this
    # version, with no way back. So the content is validated (non-empty,
    # actually a zip, readable end to end) before either the DB row or the
    # real file is ever touched, and the write is fsynced before the
    # rename.
    #
    # The rename can still land before the commit that's meant to confirm
    # it fails (a concurrent reader's transaction blocks only the commit,
    # not the earlier insert or the rename - see TestLockContention's two
    # reader-lock tests, distinct from its original writer-lock test which
    # blocks the insert instead and never reaches this path). Losing the
    # previous good zip to an upload that was never actually recorded
    # would defeat the entire point of validating first, so any existing
    # file is moved aside before the rename and only discarded once the
    # commit actually lands - `_finalize_upload_disk_state` puts it back
    # otherwise.
    committed = False
    entered_disk_phase = False
    had_previous_file = False
    previous_backed_up = False
    try:
        file.save(str(tmp_filepath))
        _fsync_path(tmp_filepath)
        _reject_invalid_zip_content(version, tmp_filepath)

        sync_results_cache.save_sync_results_entry(conn=conn, version=version, user_id=user_id)

        # From here on, filepath itself may be mutated - `entered_disk_phase`
        # marks that, so a failure after this point can be undone.
        entered_disk_phase = True
        had_previous_file = filepath.exists()
        if had_previous_file:
            filepath.replace(prev_filepath)
            previous_backed_up = True
        tmp_filepath.rename(filepath)

        conn.commit()
        committed = True
    except HTTPException:
        # Never swallow an intentional abort into the generic 500 below.
        raise
    except sqlite3.OperationalError as exc:
        # A concurrent writer elsewhere in the service (a /results import,
        # a /history upload, another sync-results upload) can hold the
        # write lock long enough to time out here. Transient, so tell the
        # caller to retry rather than reporting a hard failure. Any other
        # OperationalError falls through to the same handling as the
        # generic Exception branch below.
        _rollback(conn, version)
        # `sqlite_errorcode`, not the message text: the message is not API and
        # changes between SQLite builds. SQLITE_BUSY only - SQLITE_LOCKED is a
        # shared-cache conflict this service cannot produce, and treating it as
        # transient would tell the caller to retry something that will not clear.
        # Masked to the low byte: the attribute holds the extended code, so
        # under WAL a plain busy arrives as SQLITE_BUSY_SNAPSHOT (517) and an
        # equality test would drop it, turning a retryable 503 into a 500.
        # getattr: the driver always sets the attribute, but an OperationalError
        # constructed by hand does not have it, and an AttributeError raised here
        # would escape as an unlogged 500 instead of the handled one below.
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY:
            flask.current_app.logger.warning(f"Sync-results upload for {version} hit database lock")
            common.abort_json(503, "Server busy, try again", headers={"Retry-After": "5"})
        _abort_storage_failure(version)
    except Exception:
        _rollback(conn, version)
        _abort_storage_failure(version)
    finally:
        _finalize_upload_disk_state(
            version,
            filepath,
            prev_filepath,
            committed,
            entered_disk_phase,
            had_previous_file,
            previous_backed_up,
        )

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
    try:
        entries = sync_results_cache.list_sync_results(conn=conn)
    except sqlite3.Error:
        _abort_read_failure("listing")
    return [{"version": e.version, "timestamp": e.timestamp.isoformat()} for e in entries]


@sync_results.route("/sync-results/<version>/zip", methods=["GET"])
@flask_auth.auth.login_required
def get_sync_results_zip(version: str) -> flask.Response:
    """Download the stored sync-results zip for a version."""
    common.reject_invalid_segments(version)

    conn = flask_db.get_db()
    try:
        exists = sync_results_cache.sync_results_exists(conn=conn, version=version)
    except sqlite3.Error:
        _abort_read_failure(version)
    if not exists:
        common.abort_json(404, "No sync results found for this version")

    filepath = _sync_results_file(version=version)
    if not filepath.is_file():
        # DB says yes, disk says no - usually server-side data loss, not a
        # client error, so log it loudly; the 404 alone would hide the
        # distinction from the operator. There is one narrow, self-healing
        # exception: an overwrite in upload_sync_results moves the old file
        # to <name>.prev, then renames the new one into place - a request
        # landing in that microsecond gap sees a real but transient miss,
        # not data loss. Check for a matching .prev file before treating
        # this log line as an incident.
        flask.current_app.logger.error(
            f"Sync-results DB row exists for {version} but file {filepath} is missing"
        )
        common.abort_json(404, "No sync results found for this version")

    return flask.send_file(filepath, mimetype="application/zip")
