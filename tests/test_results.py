"""Tests for the results import/query endpoints (/results/...).

This is the oldest endpoint and the one `cardano-node-tests` calls today. It
parses the uploaded XML into per-test verdicts, and unlike /history it accepts
repeated uploads for the same testrun and job on purpose, because a job can be
re-run.
"""

import base64
import http
import io
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import List

import flask
import flask.testing
import pytest

# `types-werkzeug` still ships stubs for an older werkzeug API and doesn't know
# about this class, even though it's real at runtime.
from werkzeug.test import TestResponse  # type: ignore[attr-defined]

from testing_results_cache import common
from testing_results_cache import flask_db
from testing_results_cache import results_api
from testing_results_cache import results_cache

SAMPLE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" \
time="1.23" timestamp="2026-08-05T01:00:00.000000">
<testcase classname="cardano_node_tests.tests.test_mint.TestMint" name="test_pass" time="0.1"/>
<testcase classname="cardano_node_tests.tests.test_mint.TestMint" name="test_fail" time="0.2">\
<failure message="boom">Traceback...</failure></testcase>
<testcase classname="cardano_node_tests.tests.test_mint.TestMint" name="test_skip" time="0.0">\
<skipped type="pytest.skip">no reason</skipped></testcase>
<testcase classname="cardano_node_tests.tests.test_mint.TestMint" name="test_xfail" time="0.3">\
<skipped type="pytest.xfail">expected</skipped></testcase>
</testsuite>
</testsuites>
"""

# The escape character pytest writes when a test's output was coloured. It is
# not valid XML, and `junittools` sanitizes it, so an upload carrying one has
# to be accepted.
ESCAPE_CHAR_XML = SAMPLE_XML.replace(b"Traceback...", b"\x1b[31mTraceback...\x1b[0m")

OTHER_XML = SAMPLE_XML.replace(b'name="test_pass"', b'name="test_other"')


def _import(
    client: flask.testing.FlaskClient,
    headers: dict,
    testrun_name: str = "nightly-dbsync",
    job_id: str = "417",
    content: bytes = SAMPLE_XML,
    filename: str = "testrun-report.xml",
) -> TestResponse:
    return client.put(
        f"/results/{testrun_name}/{job_id}/import",
        headers=headers,
        data={"junitxml": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def _lines(response: TestResponse) -> List[str]:
    return [line for line in response.get_data(as_text=True).splitlines() if line]


def _uploads(app: flask.Flask) -> List[Path]:
    return sorted(Path(app.config["UPLOAD_FOLDER"]).rglob("*.xml"))


class TestImport:
    def test_import_and_query(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        body = resp.get_json()
        assert body["testrun_id"] > 0
        assert body["junitxml"].endswith(".xml")
        assert len(_uploads(app)) == 1

        with client.get("/results/nightly-dbsync/passed", headers=auth_headers) as passed:
            # xfail counts as passed, so that a re-run does not repeat it.
            assert _lines(passed) == [
                "cardano_node_tests.tests.test_mint.TestMint::test_pass",
                "cardano_node_tests.tests.test_mint.TestMint::test_xfail",
            ]

        with client.get("/results/nightly-dbsync/rerun", headers=auth_headers) as rerun:
            assert _lines(rerun) == [
                "cardano_node_tests.tests.test_mint.TestMint::test_fail",
                "cardano_node_tests.tests.test_mint.TestMint::test_skip",
            ]

    def test_pytest_nodeid_format(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _import(client, auth_headers)
        with client.get("/results/nightly-dbsync/pypassed", headers=auth_headers) as resp:
            assert _lines(resp) == [
                "cardano_node_tests/tests/test_mint.py::TestMint::test_pass",
                "cardano_node_tests/tests/test_mint.py::TestMint::test_xfail",
            ]
        with client.get("/results/nightly-dbsync/pyrerun", headers=auth_headers) as resp:
            assert _lines(resp) == [
                "cardano_node_tests/tests/test_mint.py::TestMint::test_fail",
                "cardano_node_tests/tests/test_mint.py::TestMint::test_skip",
            ]

    def test_reimporting_the_same_job_is_allowed(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Unlike /history, a job can be re-run, so a second upload is expected."""
        assert _import(client, auth_headers).status_code == http.HTTPStatus.OK
        assert _import(client, auth_headers, content=OTHER_XML).status_code == http.HTTPStatus.OK
        expected_uploads = 2
        assert len(_uploads(app)) == expected_uploads

    def test_rejects_a_byte_identical_reupload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The stored name is the content checksum, so the same bytes collide."""
        assert _import(client, auth_headers).status_code == http.HTTPStatus.OK
        resp = _import(client, auth_headers)
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "File was already uploaded"

    def test_post_import(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        resp = client.post(
            "/results/nightly/1/import",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(SAMPLE_XML), "r.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.OK

    def test_requires_auth(self, client: flask.testing.FlaskClient) -> None:
        assert _import(client, {}).status_code == http.HTTPStatus.UNAUTHORIZED
        assert client.get("/results/nightly/passed").status_code == http.HTTPStatus.UNAUTHORIZED

    def test_rejects_missing_file_part(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.put(
            "/results/nightly/1/import",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "No file part"

    def test_rejects_an_empty_filename(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, filename="")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "No selected file"

    def test_rejects_wrong_extension(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, filename="report.txt")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Unexpected file type"
        assert _uploads(app) == []

    def test_rejects_unparseable_xml(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = _import(client, auth_headers, content=b"not xml at all")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Failed to import testrun"

    def test_a_failed_import_leaves_no_file(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        _import(client, auth_headers, content=b"not xml at all")
        assert _uploads(app) == []

    def test_accepts_a_report_with_pytest_escape_characters(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        # A plain parse would refuse a real report whose output was coloured.
        assert (
            _import(client, auth_headers, content=ESCAPE_CHAR_XML).status_code == http.HTTPStatus.OK
        )


class TestPathValidation:
    """A URL segment reaches the upload path, so it has to be validated.

    A percent-encoded `..` survives the router. Before these guards it was
    accepted, and the upload landed outside `UPLOAD_FOLDER`.
    """

    @pytest.mark.parametrize("segment", ["%2e%2e", "%2e", "%2e%2e%2e"])
    def test_rejects_traversal_in_testrun_name(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, segment: str
    ) -> None:
        resp = client.put(
            f"/results/{segment}/1/import",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(SAMPLE_XML), "r.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Invalid path segment" in resp.get_json()["message"]
        assert _uploads(app) == []

    @pytest.mark.parametrize("segment", ["%2e%2e", "%2e"])
    def test_rejects_traversal_in_job_id(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, segment: str
    ) -> None:
        resp = client.put(
            f"/results/nightly/{segment}/import",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(SAMPLE_XML), "r.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert _uploads(app) == []

    def test_nothing_is_written_outside_the_upload_folder(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The assertion the status code alone does not make.

        Scanned two levels up, not one: each segment is a separate `..`, and
        both are interpolated, so the escape reaches the grandparent.
        """
        upload_folder = Path(app.config["UPLOAD_FOLDER"])
        scanned = upload_folder.parent.parent
        before = set(scanned.rglob("*.xml"))

        for path in ("/results/%2e%2e/%2e%2e/import", "/results/%2e%2e/1/import"):
            client.put(
                path,
                headers=auth_headers,
                data={"junitxml": (io.BytesIO(SAMPLE_XML), "r.xml")},
                content_type="multipart/form-data",
            )

        assert set(scanned.rglob("*.xml")) == before

    def test_rejects_overly_long_segment(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _import(client, auth_headers, testrun_name="a" * 201).status_code == (
            http.HTTPStatus.BAD_REQUEST
        )
        assert _import(client, auth_headers, testrun_name="a" * 200).status_code == (
            http.HTTPStatus.OK
        )

    def test_rejects_trailing_newline(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        resp = client.put(
            "/results/nightly%0A/1/import",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(SAMPLE_XML), "r.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST

    def test_accepts_a_real_testrun_name(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        # `cardano-node-tests` strips its testrun name to [a-zA-Z0-9_-] before
        # calling, so a name with dots never actually arrives. Dots are allowed
        # anyway, matching `history_api`, for names like "node-8.5.0".
        for name in ("nightly", "nightly-cli", "11-1-0-conway11_disk", "node-8.5.0"):
            assert _import(client, auth_headers, testrun_name=name).status_code == (
                http.HTTPStatus.OK
            )

    @pytest.mark.parametrize("route", ["passed", "pypassed", "rerun", "pyrerun"])
    def test_read_routes_are_not_validated(
        self, client: flask.testing.FlaskClient, auth_headers: dict, route: str
    ) -> None:
        """The read routes stay open on purpose, so old names keep working.

        They run only parameterised SQL and never build a path, so there is
        nothing to protect. Validating them would make any name already
        stored outside [A-Za-z0-9_.-] unreadable.
        """
        resp = client.get(f"/results/%2e%2e/{route}", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        assert _lines(resp) == []

    def test_a_legacy_name_stays_readable(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A row written before the import guard existed must still be queryable."""
        with app.app_context():
            conn = flask_db.get_db()
            results_cache.save_testrun(
                conn=conn,
                testrun_name="legacy name 1.0",
                user_id=1,
                testsuite_data=common.TestsuiteData(
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    tests_verdicts=[
                        common.TestVerdict(testid="pkg.test_m.C::test_a", verdict="passed")
                    ],
                ),
            )
            conn.commit()

        resp = client.get("/results/legacy%20name%201.0/passed", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        assert _lines(resp) == ["pkg.test_m.C::test_a"]


class TestAuth:
    """`verify_password` is the only auth check in the app.

    All three blueprints share it, and deleting the hash comparison left the
    whole suite green, so it is pinned here.
    """

    def test_rejects_a_wrong_password(self, client: flask.testing.FlaskClient) -> None:
        creds = base64.b64encode(b"tester:wrong-password").decode()
        headers = {"Authorization": f"Basic {creds}"}
        assert _import(client, headers).status_code == http.HTTPStatus.UNAUTHORIZED
        assert (
            client.get("/results/nightly/passed", headers=headers).status_code
            == http.HTTPStatus.UNAUTHORIZED
        )

    def test_rejects_an_unknown_user(self, client: flask.testing.FlaskClient) -> None:
        creds = base64.b64encode(b"nobody:secret").decode()
        assert (
            _import(client, {"Authorization": f"Basic {creds}"}).status_code
            == http.HTTPStatus.UNAUTHORIZED
        )

    def test_rejects_an_empty_password(self, client: flask.testing.FlaskClient) -> None:
        creds = base64.b64encode(b"tester:").decode()
        assert (
            _import(client, {"Authorization": f"Basic {creds}"}).status_code
            == http.HTTPStatus.UNAUTHORIZED
        )

    def test_accepts_the_right_password(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _import(client, auth_headers).status_code == http.HTTPStatus.OK


class TestUnknownTestrun:
    @pytest.mark.parametrize("route", ["passed", "pypassed", "rerun", "pyrerun"])
    def test_unknown_testrun_is_empty_not_an_error(
        self, client: flask.testing.FlaskClient, auth_headers: dict, route: str
    ) -> None:
        """An empty list is the first-run case, and callers rely on it."""
        resp = client.get(f"/results/never-uploaded/{route}", headers=auth_headers)
        assert resp.status_code == http.HTTPStatus.OK
        assert _lines(resp) == []


class TestPerUserIsolation:
    def test_another_user_does_not_see_these_results(
        self, client: flask.testing.FlaskClient, auth_headers: dict, other_auth_headers: dict
    ) -> None:
        """Unlike /history, results are read back per user."""
        _import(client, auth_headers)
        with client.get("/results/nightly-dbsync/passed", headers=auth_headers) as mine:
            assert _lines(mine)
        with client.get("/results/nightly-dbsync/passed", headers=other_auth_headers) as theirs:
            assert _lines(theirs) == []


class TestPytestify:
    """`_pytestify` drops names it cannot convert rather than failing the call."""

    def test_drops_a_name_with_no_test_file_part(self, app: flask.Flask) -> None:
        with app.app_context():
            assert results_api._pytestify(["some.module.Class::test_a"]) == []

    def test_drops_an_overly_deep_name(self, app: flask.Flask) -> None:
        with app.app_context():
            assert results_api._pytestify(["a.test_mod.Class.Nested::test_a"]) == []

    def test_converts_a_name_with_no_class(self, app: flask.Flask) -> None:
        with app.app_context():
            assert results_api._pytestify(["pkg.test_mod::test_a"]) == ["pkg/test_mod.py::test_a"]

    def test_converts_a_name_with_no_package(self, app: flask.Flask) -> None:
        with app.app_context():
            assert results_api._pytestify(["test_mod::test_a"]) == ["test_mod.py::test_a"]

    @pytest.mark.parametrize(
        "testid",
        [
            "pkg.test_mod.Cls::test_p[a::b]",
            "pkg.test_mod.Cls::test_a::b",
            "pkg.test_mod.Cls::a::b::c",
            "a::b::test_x",
        ],
    )
    def test_drops_a_name_containing_a_double_colon(self, app: flask.Flask, testid: str) -> None:
        """`parametrize("x", ["a::b"])` produces one of these. It used to raise."""
        with app.app_context():
            assert results_api._pytestify([testid]) == []


class TestPoisonedTestrun:
    """A test id the converter cannot handle must not wedge the testrun.

    The row stays in the database and there is no delete route, so an
    exception here would make /pypassed and /pyrerun fail for that testrun
    permanently, while /passed kept returning 200.
    """

    POISON = SAMPLE_XML.replace(b'name="test_pass"', b'name="test_p[a::b]"')

    def test_a_double_colon_name_does_not_break_the_query_routes(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _import(client, auth_headers, content=self.POISON).status_code == (
            http.HTTPStatus.OK
        )
        for route in ("passed", "pypassed", "rerun", "pyrerun"):
            resp = client.get(f"/results/nightly-dbsync/{route}", headers=auth_headers)
            assert resp.status_code == http.HTTPStatus.OK, route

    def test_the_other_tests_are_still_returned(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Only the unconvertible name is dropped, not the whole listing."""
        _import(client, auth_headers, content=self.POISON)
        with client.get("/results/nightly-dbsync/pypassed", headers=auth_headers) as resp:
            assert _lines(resp) == ["cardano_node_tests/tests/test_mint.py::TestMint::test_xfail"]


class TestRejectionIsLogged:
    def test_an_invalid_segment_is_logged(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A traversal attempt should leave a trace an operator can find."""
        with caplog.at_level("WARNING"):
            _import(client, auth_headers, testrun_name="%2e%2e")
        assert any("Rejected invalid path segment" in r.message for r in caplog.records)

    def test_the_rejection_cannot_forge_extra_log_lines(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A newline in the URL must not become a newline in the log.

        The path is caller-controlled and reaches the handler with `%0a`
        already decoded. Written raw, one warning becomes two lines, and the
        second is whatever the caller chose, timestamp and all.
        """
        forged = "evil%0a2026-01-01 00:00:00 WARNING: nothing to see here"
        with caplog.at_level("WARNING"):
            _import(client, auth_headers, testrun_name=forged)

        rejections = [r for r in caplog.records if "Rejected invalid path segment" in r.message]
        assert len(rejections) == 1
        assert "\n" not in rejections[0].getMessage()


class TestVerdictHelpers:
    def test_passed_includes_xfailed(self) -> None:
        verdicts = [
            results_api.common.TestVerdict(testid="a", verdict="passed"),
            results_api.common.TestVerdict(testid="b", verdict="xfailed"),
            results_api.common.TestVerdict(testid="c", verdict="failed"),
            results_api.common.TestVerdict(testid="d", verdict="skipped"),
        ]
        assert results_api.get_passed(tests_verdicts=verdicts) == {"a", "b"}
        assert results_api.get_nonpassed(tests_verdicts=verdicts) == {"c", "d"}
