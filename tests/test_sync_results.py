"""Tests for the sync-results cache endpoints (/sync-results/...).

Deliberately separate from /history: this endpoint keeps at most one entry
per cardano-node version, and a new upload for a version replaces whatever
was stored before, instead of being rejected as a duplicate.
"""

import http
import io
import sqlite3
import zipfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import List

import flask
import flask.testing

# `types-werkzeug` (pulled in transitively by `types-flask`) still ships stubs for an
# older werkzeug API and doesn't know about this class, even though it's real at
# runtime (werkzeug.test.TestResponse, a Response subclass) - pre-existing stub/
# runtime-version mismatch, not something introduced here.
from werkzeug.test import TestResponse  # type: ignore[attr-defined]

from testing_results_cache import sync_results_cache


def _make_zip(*, node_sync_results: bytes = b'{"tag_no1": "11.1.0"}') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("node_sync_results.json", node_sync_results)
        zf.writestr("graphs/nodesync_cpu_consumption.png", b"not-a-real-png-but-thats-fine")
    return buffer.getvalue()


SAMPLE_ZIP = _make_zip()
OTHER_ZIP = _make_zip(node_sync_results=b'{"tag_no1": "11.1.0", "note": "rerun"}')


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
        assert abs(datetime.now(timezone.utc) - timestamp) < timedelta(minutes=1)

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

    def test_bad_content_does_not_overwrite_existing_upload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        first = _upload(client, auth_headers, "11.1.0")
        assert first.status_code == http.HTTPStatus.OK

        bad = _upload(client, auth_headers, "11.1.0", content=b"garbage")
        assert bad.status_code == http.HTTPStatus.BAD_REQUEST

        with client.get("/sync-results/11.1.0/zip", headers=auth_headers) as zip_resp:
            assert zip_resp.data == SAMPLE_ZIP

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
        _set_timestamp(
            app.config["DATABASE"], "11.0.1", datetime.now(timezone.utc) - timedelta(days=2)
        )

        resp = client.get("/sync-results", headers=auth_headers)
        versions = [entry["version"] for entry in resp.get_json()]
        assert versions == ["11.1.0", "11.0.1"]

    def test_replacing_a_version_updates_its_position(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _upload(client, auth_headers, "11.0.1")
        _upload(client, auth_headers, "11.1.0")
        _set_timestamp(
            app.config["DATABASE"], "11.0.1", datetime.now(timezone.utc) - timedelta(days=2)
        )

        # Re-upload the older version - it should now sort as the newest.
        _upload(client, auth_headers, "11.0.1", content=OTHER_ZIP)

        resp = client.get("/sync-results", headers=auth_headers)
        versions = [entry["version"] for entry in resp.get_json()]
        assert versions == ["11.0.1", "11.1.0"]
