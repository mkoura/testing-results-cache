"""Tests for the nightly-history dump/list/download endpoints (/history/...).

This is a deliberately separate use case from /results/.../import: no
parsing, no verdicts, just storing whatever XML the caller sends and
handing it back by job_id or by a recent time window.
"""

import http
import io
import pathlib
import sqlite3
import tempfile
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
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

from testing_results_cache import history_cache

SAMPLE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" errors="0" failures="1" skipped="0" tests="2" \
time="1.23" timestamp="2026-08-05T01:00:00.000000">
<testcase classname="tests.test_foo" name="test_bar" time="0.1"/>
<testcase classname="tests.test_foo" name="test_baz" time="0.2">\
<failure message="boom">Traceback...</failure></testcase>
</testsuite>
</testsuites>
"""


OTHER_XML = b'<?xml version="1.0" encoding="utf-8"?>\n<testsuites/>\n'


def _upload(
    client: flask.testing.FlaskClient,
    headers: dict,
    testrun_name: str,
    job_id: str,
    content: bytes = SAMPLE_XML,
    filename: str = "results.xml",
) -> TestResponse:
    return client.put(
        f"/history/{testrun_name}/{job_id}",
        headers=headers,
        data={"junitxml": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def _tmp_files(app: flask.Flask) -> List[Path]:
    return list(Path(app.config["HISTORY_FOLDER"]).rglob("*.tmp"))


class TestUploadAndDownload:
    def test_upload_list_and_download(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-dbsync", "2026-08-05")
        assert resp.status_code == http.HTTPStatus.OK
        assert resp.get_json() == {"history": "nightly-dbsync/2026-08-05"}

        list_resp = client.get("/history/nightly-dbsync?days=5", headers=auth_headers)
        assert list_resp.status_code == http.HTTPStatus.OK
        entries = list_resp.get_json()
        assert [e["job_id"] for e in entries] == ["2026-08-05"]

        # The timestamp must survive the store-and-parse round trip as
        # tz-aware UTC and be plausibly "now" - see the history.timestamp
        # comment in schema.sql for the converter bug this guards against.
        timestamp = datetime.fromisoformat(entries[0]["timestamp"])
        assert timestamp.utcoffset() == timedelta(0)
        assert abs(datetime.now(timezone.utc) - timestamp) < timedelta(minutes=1)

        with client.get("/history/nightly-dbsync/2026-08-05/xml", headers=auth_headers) as xml_resp:
            assert xml_resp.status_code == http.HTTPStatus.OK
            assert xml_resp.mimetype == "application/xml"
            assert xml_resp.data == SAMPLE_XML

    def test_successful_upload_leaves_no_temp_files(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-dbsync", "job1")
        assert resp.status_code == http.HTTPStatus.OK
        assert _tmp_files(app) == []

    def test_rejects_duplicate_upload(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        first = _upload(client, auth_headers, "nightly-cli", "job1")
        assert first.status_code == http.HTTPStatus.OK

        second = _upload(client, auth_headers, "nightly-cli", "job1", content=OTHER_XML)
        assert second.status_code == http.HTTPStatus.BAD_REQUEST
        assert second.get_json()["message"] == "History already recorded for this testrun and job"

        # The rejected upload must not touch the stored file or leave temp litter.
        with client.get("/history/nightly-cli/job1/xml", headers=auth_headers) as xml_resp:
            assert xml_resp.data == SAMPLE_XML
        assert _tmp_files(app) == []

    def test_post_upload(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        """The route accepts POST as well as PUT."""
        resp = client.post(
            "/history/nightly-cli/job1",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(SAMPLE_XML), "results.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.OK

    def test_excludes_different_testrun(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "nightly-pv11", "job1")

        resp = client.get("/history/nightly-cli?days=5", headers=auth_headers)
        assert resp.get_json() == []

    def test_requires_auth(self, client: flask.testing.FlaskClient) -> None:
        assert client.get("/history/nightly-cli?days=5").status_code == http.HTTPStatus.UNAUTHORIZED
        assert (
            client.get("/history/nightly-cli/job1/xml").status_code == http.HTTPStatus.UNAUTHORIZED
        )
        assert client.put("/history/nightly-cli/job1").status_code == http.HTTPStatus.UNAUTHORIZED

    def test_not_found(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        resp = client.get("/history/nightly-cli/no-such-job/xml", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.NOT_FOUND

    def test_missing_file_is_404(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """DB row exists but the file was deleted from disk."""
        _upload(client, auth_headers, "nightly-cli", "job1")
        Path(app.config["HISTORY_FOLDER"], "nightly-cli", "job1.xml").unlink()

        resp = client.get("/history/nightly-cli/job1/xml", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.NOT_FOUND

    def test_rejects_missing_file_part(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.put(
            "/history/nightly-cli/job1",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "No file part"

    def test_rejected_extension_does_not_consume_the_job_slot(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A rejected upload must leave no trace - the retry with a valid file must work."""
        resp = _upload(client, auth_headers, "nightly-cli", "job1", filename="results.txt")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Unexpected file type"

        retry = _upload(client, auth_headers, "nightly-cli", "job1")
        assert retry.status_code == http.HTTPStatus.OK

    def test_rejects_empty_filename(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-cli", "job1", filename="")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_accepts_uppercase_extension(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-cli", "job1", filename="RESULTS.XML")
        assert resp.status_code == http.HTTPStatus.OK

    def test_rejects_zero_and_negative_days(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        for days in ("0", "-1"):
            resp = client.get(f"/history/nightly-cli?days={days}", headers=auth_headers)
            assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_non_numeric_days(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A typo must be a 400, not a silent fallback to the default window."""
        for days in ("abc", "5.5", ""):
            resp = client.get(f"/history/nightly-cli?days={days}", headers=auth_headers)
            assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_days_boundary(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        assert (
            client.get("/history/nightly-cli?days=3650", headers=auth_headers).status_code
            == http.HTTPStatus.OK
        )
        assert (
            client.get("/history/nightly-cli?days=3651", headers=auth_headers).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    def test_rejects_huge_days_value(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.get("/history/nightly-cli?days=999999999999999999999", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST


def _set_timestamp(db_path: str, job_id: str, when: datetime) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE history SET timestamp = ? WHERE job_id = ?",
        (when.strftime(history_cache.TIMESTAMP_FORMAT), job_id),
    )
    conn.commit()
    conn.close()


class TestTimeWindow:
    def test_excludes_entries_outside_window(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "nightly-dbsync", "old-job")
        _set_timestamp(
            app.config["DATABASE"], "old-job", datetime.now(timezone.utc) - timedelta(days=30)
        )

        resp = client.get("/history/nightly-dbsync?days=5", headers=auth_headers)
        assert resp.get_json() == []

    def test_orders_newest_first(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "nightly-dbsync", "job-a")
        _upload(client, auth_headers, "nightly-dbsync", "job-b")
        _set_timestamp(
            app.config["DATABASE"], "job-a", datetime.now(timezone.utc) - timedelta(days=2)
        )

        resp = client.get("/history/nightly-dbsync?days=5", headers=auth_headers)
        job_ids = [entry["job_id"] for entry in resp.get_json()]
        assert job_ids == ["job-b", "job-a"]

    def test_default_window_is_five_days(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """No `days` param - the most common real-world call - defaults to 5 days."""
        _upload(client, auth_headers, "nightly-dbsync", "fresh-job")
        _upload(client, auth_headers, "nightly-dbsync", "old-job")
        _set_timestamp(
            app.config["DATABASE"], "old-job", datetime.now(timezone.utc) - timedelta(days=6)
        )

        resp = client.get("/history/nightly-dbsync", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        assert [entry["job_id"] for entry in resp.get_json()] == ["fresh-job"]


class TestPathValidation:
    def test_rejects_traversal_job_id_on_download(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "attacker-testrun", "job1")

        resp = client.get("/history/attacker-testrun/%2e%2e/xml", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_traversal_job_id_on_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "attacker-testrun", "%2e%2e")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_overly_long_segment(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-cli", "j" * 300)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_trailing_newline(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """`$` in a regex matches before a trailing newline - fullmatch must be used."""
        resp = _upload(client, auth_headers, "nightly-cli", "job1%0A")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

        resp = client.get("/history/nightly-cli%0A?days=5", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_list_rejects_invalid_testrun_name(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.get("/history/%2e%2e?days=5", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_accepts_dots_in_segments(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Real-world names like "node-8.5.0" contain dots; only dots-alone are rejected."""
        resp = _upload(client, auth_headers, "node-8.5.0", "job.2026-08-05")
        assert resp.status_code == http.HTTPStatus.OK

        with client.get("/history/node-8.5.0/job.2026-08-05/xml", headers=auth_headers) as xml_resp:
            assert xml_resp.status_code == http.HTTPStatus.OK
            assert xml_resp.data == SAMPLE_XML


class TestAnyLoggedInUserCanRead:
    """This is one team's data, not separate tenants.

    Any valid login can read any testrun's history - only being logged in
    at all is required.
    """

    def test_other_user_can_list_and_download(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        other_auth_headers: dict,
    ) -> None:
        _upload(client, auth_headers, "shared-testrun", "job1")

        list_resp = client.get("/history/shared-testrun?days=5", headers=other_auth_headers)
        entries = list_resp.get_json()
        assert entries == [{"job_id": "job1", "timestamp": entries[0]["timestamp"]}]

        with client.get("/history/shared-testrun/job1/xml", headers=other_auth_headers) as xml_resp:
            assert xml_resp.status_code == http.HTTPStatus.OK
            assert xml_resp.data == SAMPLE_XML

    def test_other_user_cannot_reupload_same_job_id(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        other_auth_headers: dict,
    ) -> None:
        """The unique constraint is global, not per-user - job_id identifies one run."""
        assert (
            _upload(client, auth_headers, "shared-testrun", "job1").status_code
            == http.HTTPStatus.OK
        )
        assert (
            _upload(client, other_auth_headers, "shared-testrun", "job1").status_code
            == http.HTTPStatus.BAD_REQUEST
        )


class TestStorageUnavailable:
    def test_mkstemp_failure_is_clean_500(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Storage trouble before any DB work: 500, no row, retry succeeds."""

        def _boom(**_kwargs: object) -> None:
            err = "Read-only file system"
            raise OSError(err)

        monkeypatch.setattr(tempfile, "mkstemp", _boom)
        resp = _upload(client, auth_headers, "nightly-cli", "job1")
        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        monkeypatch.undo()

        retry = _upload(client, auth_headers, "nightly-cli", "job1")
        assert retry.status_code == http.HTTPStatus.OK
        assert _tmp_files(app) == []


class TestMalformedTimestampRow:
    def test_error_names_the_offending_row(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A corrupted timestamp fails the listing loudly, naming the row."""
        _upload(client, auth_headers, "nightly-dbsync", "job1")
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.execute("UPDATE history SET timestamp = 'garbage' WHERE job_id = 'job1'")
        conn.commit()
        conn.close()

        # TESTING=True re-raises server errors into the test client.
        with pytest.raises(ValueError, match="'garbage' for nightly-dbsync/job1"):
            client.get("/history/nightly-dbsync?days=5", headers=auth_headers)


class TestFailedFileStore:
    def test_failed_rename_rolls_back_the_db_row(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed file store must not leave a DB row - the job_id would be wedged forever."""

        def _boom(_self: pathlib.Path, _target: pathlib.Path) -> None:
            err = "No space left on device"
            raise OSError(err)

        monkeypatch.setattr(pathlib.Path, "rename", _boom)
        resp = _upload(client, auth_headers, "nightly-cli", "job1")
        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        monkeypatch.undo()

        # No temp litter even on the failure path.
        assert _tmp_files(app) == []

        # The failure must be recoverable: retry succeeds and the XML is readable.
        retry = _upload(client, auth_headers, "nightly-cli", "job1")
        assert retry.status_code == http.HTTPStatus.OK
        with client.get("/history/nightly-cli/job1/xml", headers=auth_headers) as xml_resp:
            assert xml_resp.status_code == http.HTTPStatus.OK
            assert xml_resp.data == SAMPLE_XML


class TestSaveHistoryEntry:
    def test_non_unique_integrity_error_propagates(self, app: flask.Flask) -> None:
        """A NOT NULL violation must surface, not get reported as a duplicate."""
        conn = sqlite3.connect(app.config["DATABASE"])
        try:
            with pytest.raises(sqlite3.IntegrityError):
                history_cache.save_history_entry(
                    conn=conn,
                    testrun_name=None,  # type: ignore[arg-type]
                    job_id="job1",
                    user_id=1,
                )
        finally:
            conn.close()


class TestConcurrentUpload:
    ATTEMPTS = 10

    def test_concurrent_uploads_never_produce_duplicates(self, app: flask.Flask) -> None:
        """Use real concurrent threads, not sequential calls.

        A check-then-insert implementation races; only the UNIQUE constraint
        (via `ON CONFLICT ... DO NOTHING`) reliably prevents duplicate entries.
        """
        db_path = app.config["DATABASE"]
        results: List[bool] = []
        results_lock = threading.Lock()
        # The timeout keeps a thread that dies before reaching the barrier
        # (e.g. on connect) from stalling the rest forever: a timed-out wait
        # breaks the barrier for everyone, all threads exit, and the length
        # assert below fails fast instead of hanging CI.
        barrier = threading.Barrier(self.ATTEMPTS)

        def _attempt() -> None:
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                barrier.wait(timeout=30)
                saved = history_cache.save_history_entry(
                    conn=conn, testrun_name="race-testrun", job_id="job1", user_id=1
                )
                # save_history_entry does not commit - the caller owns the
                # transaction (mirroring upload_history), so commit here
                # before asserting.
                conn.commit()
                with results_lock:
                    results.append(saved)
            finally:
                conn.close()

        threads = [threading.Thread(target=_attempt) for _ in range(self.ATTEMPTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All attempts must complete - a thread killed by e.g. "database is
        # locked" would make the counts below misleading.
        assert len(results) == self.ATTEMPTS
        assert results.count(True) == 1
        assert results.count(False) == self.ATTEMPTS - 1

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM history WHERE testrun_name = ? AND job_id = ?",
            ("race-testrun", "job1"),
        ).fetchone()[0]
        conn.close()
        assert count == 1
