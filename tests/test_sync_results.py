"""Tests for the sync-results cache endpoints (/sync-results/...).

Deliberately separate from /history: this endpoint keeps at most one entry
per cardano-node version, and a new upload for a version replaces whatever
was stored before, instead of being rejected as a duplicate.
"""

import http
import io
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import List

import flask
import flask.testing
import pytest

# `types-werkzeug` (pulled in transitively by `types-flask`) still ships stubs for an
# older werkzeug API and doesn't know about this class, even though it's real at
# runtime (werkzeug.test.TestResponse, a Response subclass) - pre-existing stub/
# runtime-version mismatch, not something introduced here.
from werkzeug.test import TestResponse  # type: ignore[attr-defined]

from testing_results_cache import flask_db
from testing_results_cache import sync_results_api
from testing_results_cache import sync_results_cache


def _make_zip(*, node_sync_results: bytes = b'{"tag_no1": "11.1.0"}') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("node_sync_results.json", node_sync_results)
        zf.writestr("graphs/nodesync_cpu_consumption.png", b"not-a-real-png-but-thats-fine")
    return buffer.getvalue()


SAMPLE_ZIP = _make_zip()
OTHER_ZIP = _make_zip(node_sync_results=b'{"tag_no1": "11.1.0", "note": "rerun"}')


def _corrupt_member_zip(good_zip: bytes) -> bytes:
    """Build a zip that is corrupt but still looks intact at a glance.

    Passes zipfile.is_zipfile() (EOCD record intact, near the end of the
    file) but fails zipfile.testzip() (a payload byte flipped early in the
    file, corrupting one member's CRC).
    """
    corrupted = bytearray(good_zip)
    corrupted[40] ^= 0xFF
    return bytes(corrupted)


CORRUPT_ZIP = _corrupt_member_zip(SAMPLE_ZIP)


def _invalid_utf8_filename_zip() -> bytes:
    """Build a zip with a non-ASCII filename containing invalid UTF-8 bytes.

    zipfile's writer clears a manually-set UTF-8 flag bit for an ASCII-only
    filename, so a genuinely non-ASCII name is needed to make it set the
    flag itself - then one byte of the encoded name is corrupted, in both
    the local header and the central directory, without touching the flag.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("café.json", b"{}")
    data = bytearray(buffer.getvalue())

    encoded_name = "café.json".encode()
    start = 0
    while True:
        idx = data.find(encoded_name, start)
        if idx == -1:
            break
        data[idx + 3] = 0xFF  # corrupt the continuation byte of "é"
        start = idx + 1
    return bytes(data)


def _password_protected_zip(tmp_path: Path) -> bytes:
    """Build a zip with one password-protected member.

    zipfile can only write encrypted zips it can also read, i.e. none - it
    has no writer for this. Shell out to the `zip` CLI (preinstalled on
    GitHub-hosted runners) instead.
    """
    plain = tmp_path / "payload.txt"
    plain.write_bytes(b"irrelevant, only the container needs to be encrypted")
    encrypted = tmp_path / "encrypted.zip"
    subprocess.run(["zip", "-q", "-j", "-P", "secret", str(encrypted), str(plain)], check=True)
    return encrypted.read_bytes()


def _upload(
    client: flask.testing.FlaskClient,
    headers: dict,
    version: str,
    content: bytes = SAMPLE_ZIP,
    filename: str = "sync_results.zip",
) -> TestResponse:
    return client.put(
        f"/sync-results/{version}",
        headers=headers,
        data={"syncresults": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def _tmp_files(app: flask.Flask) -> List[Path]:
    return list(Path(app.config["SYNC_RESULTS_FOLDER"]).rglob("*.tmp"))


class TestUploadAndDownload:
    def test_upload_list_and_download(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "11.1.0")
        assert resp.status_code == http.HTTPStatus.OK
        assert resp.get_json() == {"sync_results": "11.1.0"}

        list_resp = client.get("/sync-results", headers=auth_headers)
        assert list_resp.status_code == http.HTTPStatus.OK
        entries = list_resp.get_json()
        assert [e["version"] for e in entries] == ["11.1.0"]

        # The timestamp must survive the store-and-parse round trip as
        # tz-aware UTC and be plausibly "now" - see the sync_results.timestamp
        # comment in schema.sql for the converter bug this guards against.
        timestamp = datetime.fromisoformat(entries[0]["timestamp"])
        assert timestamp.utcoffset() == timedelta(0)
        assert abs(datetime.now(UTC) - timestamp) < timedelta(minutes=1)

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.status_code == http.HTTPStatus.OK
            assert zip_resp.mimetype == "application/zip"
            assert zip_resp.data == SAMPLE_ZIP

    def test_successful_upload_leaves_no_temp_files(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "11.1.0")
        assert resp.status_code == http.HTTPStatus.OK
        assert _tmp_files(app) == []

    def test_second_upload_replaces_the_first(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The one behavior that's the opposite of /history: no reject, no dedup."""
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        second = _upload(client, auth_headers, "11.1.0", content=OTHER_ZIP)
        assert second.status_code == http.HTTPStatus.OK

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == OTHER_ZIP

        # Still exactly one entry for this version, not two.
        entries = client.get("/sync-results", headers=auth_headers).get_json()
        assert [e["version"] for e in entries] == ["11.1.0"]

    def test_post_upload(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        """The route accepts POST as well as PUT."""
        resp = client.post(
            "/sync-results/11.1.0",
            headers=auth_headers,
            data={"syncresults": (io.BytesIO(SAMPLE_ZIP), "sync_results.zip")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.OK

    def test_excludes_different_version(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "11.0.1")

        resp = client.get("/sync-results/11.1.0/zip", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.NOT_FOUND

        entries = client.get("/sync-results", headers=auth_headers).get_json()
        assert [e["version"] for e in entries] == ["11.0.1"]

    def test_requires_auth(self, client: flask.testing.FlaskClient) -> None:
        assert client.get("/sync-results").status_code == http.HTTPStatus.UNAUTHORIZED
        assert client.get("/sync-results/11.1.0/zip").status_code == http.HTTPStatus.UNAUTHORIZED
        assert client.put("/sync-results/11.1.0").status_code == http.HTTPStatus.UNAUTHORIZED

    def test_not_found(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        resp = client.get("/sync-results/no-such-version/zip", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.NOT_FOUND

    def test_missing_file_is_404(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """DB row exists but the file was deleted from disk."""
        _upload(client, auth_headers, "11.1.0")
        Path(app.config["SYNC_RESULTS_FOLDER"], "11.1.0.zip").unlink()

        resp = client.get("/sync-results/11.1.0/zip", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.NOT_FOUND

    def test_missing_table_is_json_500_not_html(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """An unrun migration must not break the JSON-error contract.

        Every other error response on this blueprint is JSON - the upload
        route already handles a DB error this way, but the two GET routes
        had no such handling at all, so a missing table escaped uncaught
        into Werkzeug's default HTML error page with nothing logged.
        """
        with app.app_context():
            conn = flask_db.get_db()
            conn.execute("DROP TABLE sync_results")
            conn.commit()

        list_resp = client.get("/sync-results", headers=auth_headers)
        assert list_resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert list_resp.content_type == "application/json"
        assert list_resp.get_json()["message"] == "Failed to read sync results"

        zip_resp = client.get("/sync-results/11.1.0/zip", headers=auth_headers)
        assert zip_resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert zip_resp.content_type == "application/json"
        assert zip_resp.get_json()["message"] == "Failed to read sync results"

    def test_rejects_missing_file_part(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.put(
            "/sync-results/11.1.0",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "No file part"

    def test_rejected_extension_does_not_touch_existing_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A rejected upload must not destroy a version's existing good data."""
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        bad = _upload(client, auth_headers, "11.1.0", filename="results.txt")
        assert bad.status_code == http.HTTPStatus.BAD_REQUEST
        assert bad.get_json()["message"] == "Unexpected file type"

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    def test_rejects_empty_filename(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "11.1.0", filename="")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_accepts_uppercase_extension(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "11.1.0", filename="SYNC_RESULTS.ZIP")
        assert resp.status_code == http.HTTPStatus.OK

    def test_rejects_empty_file_content(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Content is validated before it can ever overwrite the stored file.

        Unlike /history, a bad upload here would destroy the last good one -
        there is no dedup safety net.
        """
        resp = _upload(client, auth_headers, "11.1.0", content=b"")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Empty file"
        assert _tmp_files(app) == []

    def test_rejects_non_zip_content(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "11.1.0", content=b"not actually a zip file")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_rejects_zip_with_a_corrupted_member(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Reject a corrupt member even though is_zipfile() alone would accept it.

        zipfile.is_zipfile() only checks the end-of-central-directory
        record, not each member's CRC. A corrupt-but-EOCD-shaped upload
        must still be rejected, since there is no dedup fallback here to
        protect a previously-good upload.
        """
        assert zipfile.is_zipfile(io.BytesIO(CORRUPT_ZIP))

        resp = _upload(client, auth_headers, "11.1.0", content=CORRUPT_ZIP)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_corrupted_member_does_not_overwrite_existing_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        bad = _upload(client, auth_headers, "11.1.0", content=CORRUPT_ZIP)
        assert bad.status_code == http.HTTPStatus.BAD_REQUEST

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    def test_bad_content_does_not_overwrite_existing_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        bad = _upload(client, auth_headers, "11.1.0", content=b"garbage")
        assert bad.status_code == http.HTTPStatus.BAD_REQUEST

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    def test_rejects_password_protected_zip(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: Path,
    ) -> None:
        """A password-protected member must be rejected like any other bad zip.

        testzip() raises RuntimeError for this, not BadZipFile - it still
        counts as "not a valid zip", since this service can never read the
        member back either way.
        """
        if shutil.which("zip") is None:
            pytest.skip("zip CLI not available")

        resp = _upload(client, auth_headers, "11.1.0", content=_password_protected_zip(tmp_path))
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_rejects_zip_over_the_uncompressed_size_cap(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A zip over the declared-size cap is rejected before decompressing.

        The real check bounds the cost of validating a zip bomb (tiny on
        disk, huge once inflated). The cap is lowered here instead of
        uploading an actually-huge file, so the test stays fast.
        """
        monkeypatch.setattr(sync_results_api, "MAX_UNCOMPRESSED_BYTES", 10)

        resp = _upload(client, auth_headers, "11.1.0", content=SAMPLE_ZIP)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_rejects_zip_with_invalid_utf8_filename(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A non-ASCII filename with invalid UTF-8 bytes must not 500.

        zipfile.ZipFile()'s constructor raises UnicodeDecodeError for this,
        not testzip() - a different call site, so it needs its own except
        clause rather than relying on the one around testzip().
        """
        resp = _upload(client, auth_headers, "11.1.0", content=_invalid_utf8_filename_zip())
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_rejects_zip_over_the_member_count_cap(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Many trivial members costs real CPU to check even at 0 bytes each.

        The byte-size cap alone doesn't bound this - 180,000 empty members
        measured at ~1.5s of CPU in the review that found this. The cap is
        lowered here instead of uploading that many members, so the test
        stays fast.
        """
        monkeypatch.setattr(sync_results_api, "MAX_ZIP_MEMBERS", 1)

        resp = _upload(client, auth_headers, "11.1.0", content=SAMPLE_ZIP)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Not a valid zip file"
        assert _tmp_files(app) == []

    def test_rejects_traversal_version_on_download(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "11.1.0")

        resp = client.get("/sync-results/%2e%2e/zip", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_traversal_version_on_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "%2e%2e")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_overly_long_segment(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "v" * 300)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_trailing_newline(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """`$` in a regex matches before a trailing newline - fullmatch must be used."""
        resp = _upload(client, auth_headers, "11.1.0%0A")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

        resp = client.get("/sync-results/11.1.0%0A/zip", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_accepts_dots_in_version(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Real version strings like "11.1.0" contain dots; only dots-alone are rejected."""
        resp = _upload(client, auth_headers, "11.1.0")
        assert resp.status_code == http.HTTPStatus.OK


def _set_timestamp(db_path: str, version: str, when: datetime) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sync_results SET timestamp = ? WHERE version = ?",
        (when.strftime(sync_results_cache.TIMESTAMP_FORMAT), version),
    )
    conn.commit()
    conn.close()


class TestListOrdering:
    def test_orders_newest_first(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "11.0.1")
        _upload(client, auth_headers, "11.1.0")
        _set_timestamp(app.config["DATABASE"], "11.0.1", datetime.now(UTC) - timedelta(days=2))

        resp = client.get("/sync-results", headers=auth_headers)
        versions = [entry["version"] for entry in resp.get_json()]
        assert versions == ["11.1.0", "11.0.1"]

    def test_replacing_a_version_updates_its_position(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "11.0.1")
        _upload(client, auth_headers, "11.1.0")
        _set_timestamp(app.config["DATABASE"], "11.0.1", datetime.now(UTC) - timedelta(days=2))

        # Re-upload the older version - it should now sort as the newest.
        _upload(client, auth_headers, "11.0.1", content=OTHER_ZIP)

        resp = client.get("/sync-results", headers=auth_headers)
        versions = [entry["version"] for entry in resp.get_json()]
        assert versions == ["11.0.1", "11.1.0"]

    def test_a_malformed_timestamp_is_skipped_not_fatal(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """One bad row must not hide every other version from the listing."""
        _upload(client, auth_headers, "11.0.1")
        _upload(client, auth_headers, "11.1.0")

        conn = sqlite3.connect(app.config["DATABASE"])
        conn.execute(
            "UPDATE sync_results SET timestamp = 'not-a-real-timestamp' WHERE version = ?",
            ("11.0.1",),
        )
        conn.commit()
        conn.close()

        resp = client.get("/sync-results", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        versions = [entry["version"] for entry in resp.get_json()]
        assert versions == ["11.1.0"]


# SQLITE_BUSY (5) with extended bits: SQLITE_BUSY_SNAPSHOT.
SQLITE_BUSY_SNAPSHOT = 517


class TestLockContention:
    def test_returns_503_not_500_when_the_db_is_locked(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Surface database lock contention as a retryable 503, not a 500.

        A concurrent writer elsewhere in the service (a /results import, a
        /history upload, another sync-results upload) can hold the write
        lock long enough that this request's own connection times out
        acquiring it. That must not touch the version's already-stored
        good zip either.

        Uses a real second connection holding a real file-level sqlite
        lock, not a mock - the same style as history_cache's own
        TestConcurrentUpload, just holding the lock instead of racing it.
        """
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        db_path = app.config["DATABASE"]
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def _hold_write_lock() -> None:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE sync_results SET user_id = user_id WHERE version = ?", ("11.1.0",)
                )
                lock_acquired.set()
                # A timeout here (rather than an unbounded wait) keeps a
                # test failure from hanging CI if the assertions below
                # never reach release_lock.set().
                release_lock.wait(timeout=30)
                conn.rollback()
            finally:
                conn.close()

        holder = threading.Thread(target=_hold_write_lock)
        holder.start()
        try:
            assert lock_acquired.wait(timeout=5), "lock-holding thread never acquired the lock"

            resp = _upload(client, auth_headers, "11.1.0", content=OTHER_ZIP)
        finally:
            release_lock.set()
            holder.join(timeout=30)

        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert resp.headers.get("Retry-After") == "5"

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    @pytest.mark.parametrize(
        ("errorcode", "message", "expected"),
        [
            # The whole point of the change: the code decides, not the words.
            (
                sqlite3.SQLITE_BUSY,
                "some other wording entirely",
                http.HTTPStatus.SERVICE_UNAVAILABLE,
            ),
            # Extended code. Under WAL a plain busy arrives as
            # SQLITE_BUSY_SNAPSHOT, and an equality test would drop it.
            (SQLITE_BUSY_SNAPSHOT, "database is locked", http.HTTPStatus.SERVICE_UNAVAILABLE),
            # Not a busy at all. Retrying this never clears it.
            (
                sqlite3.SQLITE_LOCKED,
                "database table is locked",
                http.HTTPStatus.INTERNAL_SERVER_ERROR,
            ),
        ],
        ids=["busy_with_other_wording", "wal_extended_busy", "locked_is_not_busy"],
    )
    def test_only_a_busy_code_is_retryable(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
        errorcode: int,
        message: str,
        expected: http.HTTPStatus,
    ) -> None:
        """Pins the branch to the error code rather than the message text.

        The message is not API and changes between SQLite builds, which is why
        the check moved off it. Without a case whose code and wording disagree,
        the old substring test passes every one of these tests too.
        """
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        class CodedError(sqlite3.OperationalError):
            sqlite_errorcode = errorcode

        class _FailingCommit:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def __getattr__(self, name: str) -> object:
                return getattr(self._conn, name)

            def commit(self) -> None:
                raise CodedError(message)

        real_get_db = flask_db.get_db
        monkeypatch.setattr(flask_db, "get_db", lambda: _FailingCommit(real_get_db()))
        resp = _upload(client, auth_headers, "11.1.0", content=OTHER_ZIP)
        monkeypatch.undo()

        assert resp.status_code == expected

        # Whatever the verdict, the good zip it was replacing is still served.
        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    def test_a_non_lock_database_error_is_still_a_500(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only lock contention is retryable. Everything else is a real failure.

        Telling a caller to retry a broken schema or a full disk sends it round
        the same wall forever. This is the branch the lock check has to get
        right, so it is tested with an error that carries no sqlite error code
        at all, which is what a hand-constructed OperationalError looks like.
        """
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        class _FailingCommit:
            """Delegates everything to the real connection except the commit."""

            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def __getattr__(self, name: str) -> object:
                return getattr(self._conn, name)

            def commit(self) -> None:
                msg = "disk I/O error"
                raise sqlite3.OperationalError(msg)

        real_get_db = flask_db.get_db
        monkeypatch.setattr(flask_db, "get_db", lambda: _FailingCommit(real_get_db()))
        resp = _upload(client, auth_headers, "11.1.0", content=OTHER_ZIP)
        monkeypatch.undo()

        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert "Retry-After" not in resp.headers

        # And the good zip it was replacing is still the one served.
        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

    def test_survives_a_commit_failure_after_the_rename_has_landed(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A commit failure after the rename must not destroy the old zip.

        Different lock type from the test above, and that difference is
        the point: a reader's transaction (BEGIN, then only a SELECT) is
        compatible with this handler's own insert, so the insert and the
        rename both succeed and only the commit blocks. Left unhandled,
        that would mean the previous good zip is gone even though the
        upload that replaced it was never actually recorded - the client
        would see a safe-looking 503 while data was actually lost.
        """
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        db_path = app.config["DATABASE"]
        reader = sqlite3.connect(db_path, timeout=1)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM sync_results").fetchone()
        try:
            resp = _upload(client, auth_headers, "11.1.0", content=OTHER_ZIP)
        finally:
            reader.close()

        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

        # No backup file left lying around either.
        leftover = list(Path(app.config["SYNC_RESULTS_FOLDER"]).glob("*.prev"))
        assert leftover == []

    def test_commit_failure_on_a_brand_new_version_leaves_no_orphan(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Same race as above, but with nothing previously stored to restore.

        The rename can still land before the commit fails, so without this
        the version would end up with a file on disk, no database row, a
        permanent 404 on download, and nothing in the listing either.
        """
        db_path = app.config["DATABASE"]
        reader = sqlite3.connect(db_path, timeout=1)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM sync_results").fetchone()
        try:
            resp = _upload(client, auth_headers, "99.9.9")
        finally:
            reader.close()

        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE

        sync_results_folder = Path(app.config["SYNC_RESULTS_FOLDER"])
        assert list(sync_results_folder.iterdir()) == []

        listing = client.get("/sync-results", headers=auth_headers).get_json()
        assert listing == []


class TestUploadSizeLimit:
    def test_oversized_upload_is_413_json_not_html(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Confirm the 413 error handler returns JSON, not Werkzeug's HTML page.

        MAX_CONTENT_LENGTH is enforced app-wide by Werkzeug, before this
        blueprint's own code ever runs - this is app.py's error handler for
        it, not sync_results_api.py, so a request through this endpoint is
        just how it's exercised here.
        """
        oversized = b"0" * (17 * 1000 * 1000)  # just over the 16MB cap
        resp = _upload(client, auth_headers, "11.1.0", content=oversized)
        assert resp.status_code == http.HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        assert resp.get_json()["message"] == "Request too large"
