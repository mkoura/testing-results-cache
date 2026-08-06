"""Tests for the nightly-history dump/list/download endpoints (/history/...).

This is a deliberately separate use case from /results/.../import: no
parsing, no verdicts, just storing whatever XML the caller sends and
handing it back by job_id or by a recent time window.
"""

import http
import io
import sqlite3
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List
from typing import Tuple

import flask
import flask.testing

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


def _junit_file(content: bytes = SAMPLE_XML, name: str = "results.xml") -> Tuple[io.BytesIO, str]:
    return (io.BytesIO(content), name)


def _upload(
    client: flask.testing.FlaskClient,
    headers: dict,
    testrun_name: str,
    job_id: str,
    content: bytes = SAMPLE_XML,
) -> TestResponse:
    return client.put(
        f"/history/{testrun_name}/{job_id}",
        headers=headers,
        data={"junitxml": _junit_file(content)},
        content_type="multipart/form-data",
    )


class TestUploadAndDownload:
    def test_upload_list_and_download(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _upload(client, auth_headers, "nightly-dbsync", "2026-08-05")
        assert resp.status_code == http.HTTPStatus.OK

        list_resp = client.get("/history/nightly-dbsync?days=5", headers=auth_headers)
        assert list_resp.status_code == http.HTTPStatus.OK
        entries = list_resp.get_json()
        assert entries == [{"job_id": "2026-08-05", "timestamp": entries[0]["timestamp"]}]

        with client.get("/history/nightly-dbsync/2026-08-05/xml", headers=auth_headers) as xml_resp:
            assert xml_resp.status_code == http.HTTPStatus.OK
            assert xml_resp.data == SAMPLE_XML

    def test_rejects_duplicate_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        first = _upload(client, auth_headers, "nightly-cli", "job1")
        assert first.status_code == http.HTTPStatus.OK

        second = _upload(client, auth_headers, "nightly-cli", "job1")
        assert second.status_code == http.HTTPStatus.BAD_REQUEST

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

    def test_rejects_zero_and_negative_days(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        for days in ("0", "-1"):
            resp = client.get(f"/history/nightly-cli?days={days}", headers=auth_headers)
            assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_huge_days_value(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.get("/history/nightly-cli?days=999999999999999999999", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST


def _set_timestamp(db_path: str, job_id: str, when: datetime) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE history SET timestamp = ? WHERE job_id = ?",
        (when.strftime("%Y-%m-%d %H:%M:%S.%f"), job_id),
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


class TestConcurrentUpload:
    ATTEMPTS = 10

    def test_concurrent_uploads_never_produce_duplicates(self, app: flask.Flask) -> None:
        """Use real concurrent threads, not sequential calls.

        A sqlite3 IntegrityError under a UNIQUE constraint is the only thing
        that reliably prevents duplicates here, per the lesson learned
        building the previous version of this feature.
        """
        db_path = app.config["DATABASE"]
        results: List[bool] = []
        barrier = threading.Barrier(self.ATTEMPTS)

        def _attempt() -> None:
            conn = sqlite3.connect(db_path)
            barrier.wait()
            saved = history_cache.save_history_entry(
                conn=conn, testrun_name="race-testrun", job_id="job1", user_id=1
            )
            results.append(saved)
            conn.close()

        threads = [threading.Thread(target=_attempt) for _ in range(self.ATTEMPTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == self.ATTEMPTS - 1

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM history WHERE testrun_name = ? AND job_id = ?",
            ("race-testrun", "job1"),
        ).fetchone()[0]
        conn.close()
        assert count == 1
