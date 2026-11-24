"""Endpoints for storing and retrieving the cached sync-test-results zip.

Deliberately separate from /history: /history keeps one entry per
testrun+job forever, appending. This endpoint keeps one entry per sync run
(network, cardano-node version and start time), and a new upload for the
same run replaces whatever was stored for it before. No parsing of the
zip's contents happens here - the caller (cardano-sync-tests CI) decides
what goes inside, this service stores it and hands it back. The sync
analyzer reads the stored zip afterwards, in sync_analyzer.py.

Mounted at `/sync-results/...`, its own top-level prefix, so a version
string can never collide with a route under `/results/` or `/history/`.
"""

import os
import sqlite3
import tempfile
import zipfile
import zlib
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import List
from typing import NoReturn

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import sync_analyzer
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


def _started() -> datetime:
    """Read the run's start time from the upload form.

    The analyzer orders runs by it and the row key includes it, so a
    naive time is refused: two uploaders in different zones would make two
    runs out of one.
    """
    raw = flask.request.form.get("started", "")
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        common.abort_json(400, "Missing or invalid 'started': expected an ISO 8601 time")
    if started.tzinfo is None:
        common.abort_json(400, "Invalid 'started': the time needs a UTC offset")
    try:
        return started.astimezone(UTC)
    except OverflowError:
        common.abort_json(400, "Invalid 'started': the time is out of range")


def _environment() -> str:
    """Read the network name from the upload form."""
    environment = flask.request.form.get("environment", "")
    if not common.valid_path_segment(environment):
        common.abort_json(400, "Missing or invalid 'environment'")
    return environment


def _check_version(version: str) -> None:
    """Refuse a version the service cannot store or draw."""
    common.reject_invalid_segments(version)
    if version.startswith("-"):
        # The version becomes a legend label on the graph script's command line.
        common.abort_json(400, "Invalid version: it cannot start with '-'")


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
    """Store the sync-results zip for one run, replacing any prior upload of it.

    The run is `version` plus the `environment` and `started` form fields.
    """
    _check_version(version)

    if "syncresults" not in flask.request.files:
        common.abort_json(400, "No file part")

    file = flask.request.files["syncresults"]
    # `not file.filename` also covers None (malformed multipart part).
    if (
        not file.filename
        or Path(file.filename).suffix.lower() not in common.ALLOWED_SYNC_RESULTS_EXTENSIONS
    ):
        common.abort_json(400, "Unexpected file type")
    environment = _environment()
    started = _started()

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]
    folder = Path(flask.current_app.config["SYNC_RESULTS_FOLDER"])

    # mkstemp, not a random-suffix name: its O_EXCL creation guarantees each
    # request its own file instead of silently colliding with a concurrent
    # upload for a different version (preforked WSGI workers can share PRNG
    # state, so "random" suffixes do collide in practice).
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".tmp")
        os.close(tmp_fd)
    except OSError:
        flask.current_app.logger.exception(f"Sync-results storage unavailable for {version}")
        common.abort_json(500, "Failed to store sync results")
    tmp_filepath = Path(tmp_name)
    # Set once the row exists: the row id names the file.
    filepath = tmp_filepath
    prev_filepath = tmp_filepath

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

        run_id = sync_results_cache.save_sync_results_entry(
            conn=conn,
            environment=environment,
            version=version,
            started=started,
            user_id=user_id,
            facts=sync_analyzer.facts_for_upload(tmp_filepath),
        )
        filepath = sync_analyzer.results_file(run_id)
        prev_filepath = filepath.with_name(filepath.name + ".prev")

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

    sync_analyzer.after_upload(conn)

    return {
        "sync_results": version,
        "id": run_id,
        "environment": environment,
        "started": started.isoformat(),
    }


def entry_dict(entry: common.SyncResultsEntry) -> dict:
    """Return the fields of a stored run that a caller sees."""
    return {
        "id": entry.id,
        "environment": entry.environment,
        "version": entry.version,
        "started": entry.started.isoformat(),
        "timestamp": entry.timestamp.isoformat(),
    }


@sync_results.route("/sync-results", methods=["GET"])
@flask_auth.auth.login_required
def list_sync_results() -> List[dict]:
    """List every run that currently has a stored sync-results zip."""
    conn = flask_db.get_db()
    try:
        entries = sync_results_cache.list_sync_results(conn=conn)
    except sqlite3.Error:
        _abort_read_failure("listing")
    return [entry_dict(e) for e in entries]


@sync_results.route("/sync-results/<int:run_id>/zip", methods=["GET"])
@flask_auth.auth.login_required
def get_sync_results_zip(run_id: int) -> flask.Response:
    """Download the stored sync-results zip of a run."""
    return sync_analyzer.send_zip(run_id)
