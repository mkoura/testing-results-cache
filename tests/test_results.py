"""Tests for the /results/... upload endpoint.

Scoped to the path-traversal fix in import_results: testrun_name and job_id
were used to build a filesystem path with no validation, unlike /history
and /sync-results, which both reject a non-alphanumeric segment before
touching disk. A happy-path test is included alongside the regression
tests, to confirm the fix doesn't also reject a normal upload.
"""

import base64
import http
import io
import sqlite3
import threading

import flask
import flask.testing

# `types-werkzeug` (pulled in transitively by `types-flask`) still ships stubs for an
# older werkzeug API and doesn't know about this class, even though it's real at
# runtime (werkzeug.test.TestResponse, a Response subclass) - pre-existing stub/
# runtime-version mismatch, not something introduced here.
from werkzeug.test import TestResponse  # type: ignore[attr-defined]

SAMPLE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" \
time="1.23" timestamp="2026-08-05T01:00:00.000000">
<testcase classname="tests.test_foo" name="test_bar" time="0.1"/>
</testsuite>
</testsuites>
"""


def _import(
    client: flask.testing.FlaskClient,
    headers: dict,
    testrun_name: str,
    job_id: str,
    content: bytes = SAMPLE_XML,
    filename: str = "results.xml",
) -> TestResponse:
    return client.put(
        f"/results/{testrun_name}/{job_id}/import",
        headers=headers,
        data={"junitxml": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


class TestImport:
    def test_import_and_read_back(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, "cardano-node-tests", "123")
        assert resp.status_code == http.HTTPStatus.OK

        passed_resp = client.get("/results/cardano-node-tests/passed", headers=auth_headers)
        assert passed_resp.status_code == http.HTTPStatus.OK
        assert passed_resp.data == b"tests.test_foo::test_bar"

    def test_rejects_traversal_testrun_name_on_import(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, "%2e%2e", "123")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Invalid path segment" in resp.get_json()["message"]

    def test_rejects_traversal_job_id_on_import(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, "cardano-node-tests", "%2e%2e")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Invalid path segment" in resp.get_json()["message"]


class TestLockContention:
    def test_returns_503_not_500_when_the_db_is_locked(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Surface database lock contention as a retryable 503, not a 500.

        A concurrent writer elsewhere in the service can hold the write
        lock long enough that this request's own connection times out
        acquiring it. Uses a real second connection holding a real
        file-level sqlite lock, not a mock - the same style as
        test_sync_results.py's TestLockContention.
        """
        db_path = app.config["DATABASE"]
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def _hold_write_lock() -> None:
            conn = sqlite3.connect(db_path, timeout=1)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO testrun(name) VALUES ('lockholder')")
                lock_acquired.set()
                # A timeout here (rather than an unbounded wait) keeps a test
                # failure from hanging CI if the assertions below never reach
                # release_lock.set().
                release_lock.wait(timeout=30)
                conn.rollback()
            finally:
                conn.close()

        holder = threading.Thread(target=_hold_write_lock)
        holder.start()
        try:
            assert lock_acquired.wait(timeout=5), "lock-holding thread never acquired the lock"

            resp = _import(client, auth_headers, "cardano-node-tests", "123")
        finally:
            release_lock.set()
            holder.join(timeout=30)

        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert resp.headers.get("Retry-After") == "5"

        # A retry after the lock clears must succeed cleanly - the failed
        # attempt must not have left the renamed file stuck in place.
        retry = _import(client, auth_headers, "cardano-node-tests", "123")
        assert retry.status_code == http.HTTPStatus.OK


class TestAuth:
    """Cover auth failure modes beyond "no credentials at all".

    The auth callback (flask_auth.verify_password) is shared by every
    blueprint, so testing it once here covers /history and /sync-results
    too - only "no credentials at all" was ever tested before, never a
    wrong password or an unknown username, both of which are ordinary
    everyday events (a mistyped account, a rotated secret).
    """

    def test_wrong_password_is_401(self, client: flask.testing.FlaskClient) -> None:
        creds = base64.b64encode(b"tester:not-the-real-password").decode()
        resp = _import(client, {"Authorization": f"Basic {creds}"}, "cardano-node-tests", "123")
        assert resp.status_code == http.HTTPStatus.UNAUTHORIZED

    def test_unknown_username_is_401(self, client: flask.testing.FlaskClient) -> None:
        creds = base64.b64encode(b"no-such-user:whatever").decode()
        resp = _import(client, {"Authorization": f"Basic {creds}"}, "cardano-node-tests", "123")
        assert resp.status_code == http.HTTPStatus.UNAUTHORIZED

    def test_malformed_auth_header_is_401(self, client: flask.testing.FlaskClient) -> None:
        resp = _import(
            client, {"Authorization": "Basic not-valid-base64!!"}, "cardano-node-tests", "123"
        )
        assert resp.status_code == http.HTTPStatus.UNAUTHORIZED


class TestUploadSizeLimit:
    def test_oversized_upload_is_413(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Confirm the 16MB upload cap is enforced, not just configured.

        MAX_CONTENT_LENGTH is enforced by Werkzeug for the whole app, before
        routing even happens - checked once here rather than duplicating the
        same check for /history and /sync-results too.
        """
        oversized = b"0" * (17 * 1000 * 1000)  # just over the 16MB cap
        resp = _import(client, auth_headers, "cardano-node-tests", "123", content=oversized)
        assert resp.status_code == http.HTTPStatus.REQUEST_ENTITY_TOO_LARGE
