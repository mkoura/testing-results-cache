"""Read-only smoke tests against a deployed service."""

import base64
import json
import os
from dataclasses import dataclass
from dataclasses import field
from http import HTTPStatus
from http import client
from urllib.parse import SplitResult
from urllib.parse import urlsplit
from uuid import uuid4

import pytest


@dataclass
class Deployment:
    url: SplitResult
    authorization: str = field(repr=False)

    def get(self, path: str, *, authenticated: bool = True) -> tuple[int, str, bytes]:
        prefix = self.url.path.rstrip("/")
        headers = {"Authorization": self.authorization} if authenticated else {}
        connection_type = (
            client.HTTPSConnection if self.url.scheme == "https" else client.HTTPConnection
        )
        connection = connection_type(self.url.netloc, timeout=10)
        # Use the configured origin directly so redirects cannot forward credentials.
        try:
            connection.request("GET", f"{prefix}{path}", headers=headers)
            with connection.getresponse() as response:
                return response.status, response.headers.get_content_type(), response.read()
        finally:
            connection.close()


@pytest.fixture(scope="module")
def deployment() -> Deployment:
    raw_url = os.environ.get("TCACHE_URL")
    credentials = os.environ.get("TCACHE_BASIC_AUTH")
    if not raw_url or not credentials:
        pytest.skip("Set both TCACHE_URL and TCACHE_BASIC_AUTH to test a deployment")

    url = urlsplit(raw_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.query
        or url.fragment
    ):
        pytest.fail("TCACHE_URL must be an HTTP(S) URL without credentials, a query, or a fragment")
    if ":" not in credentials:
        pytest.fail("TCACHE_BASIC_AUTH must have the form username:password")
    token = base64.b64encode(credentials.encode()).decode("ascii")
    return Deployment(url, f"Basic {token}")


@pytest.fixture(scope="module")
def unused_testrun() -> str:
    return f"deployment-smoke-{uuid4().hex}"


def test_results_require_auth(deployment: Deployment, unused_testrun: str) -> None:
    status, _, _ = deployment.get(f"/results/{unused_testrun}/pypassed", authenticated=False)
    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("view", ["passed", "pypassed", "rerun", "pyrerun"])
def test_results(deployment: Deployment, unused_testrun: str, view: str) -> None:
    status, content_type, body = deployment.get(f"/results/{unused_testrun}/{view}")
    assert status == HTTPStatus.OK
    assert content_type == "text/plain"
    assert body == b""


def test_history(deployment: Deployment, unused_testrun: str) -> None:
    status, content_type, body = deployment.get(f"/history/{unused_testrun}?days=1")
    assert status == HTTPStatus.OK
    assert content_type == "application/json"
    assert json.loads(body) == []


def test_stats_totals(deployment: Deployment) -> None:
    status, content_type, body = deployment.get("/stats?days=1")
    assert status == HTTPStatus.OK
    assert content_type == "application/json"

    totals = json.loads(body)
    assert totals["days"] == 1
    assert isinstance(totals["runs"], int)
    assert totals["runs"] >= 0


def test_stats_runs(deployment: Deployment) -> None:
    status, content_type, body = deployment.get("/stats/runs?days=1&limit=1")
    assert status == HTTPStatus.OK
    assert content_type == "application/json"

    runs = json.loads(body)
    assert isinstance(runs, list)
    assert len(runs) <= 1


def test_sync_results(deployment: Deployment) -> None:
    status, content_type, body = deployment.get("/sync-results")
    assert status == HTTPStatus.OK
    assert content_type == "application/json"
    assert isinstance(json.loads(body), list)
