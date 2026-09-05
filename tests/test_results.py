"""Tests for the /results/... upload endpoint.

Scoped to the path-traversal fix in import_results: testrun_name and job_id
were used to build a filesystem path with no validation, unlike /history
and /sync-results, which both reject a non-alphanumeric segment before
touching disk. A happy-path test is included alongside the regression
tests, to confirm the fix doesn't also reject a normal upload.
"""

import http
import io

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
