"""Unit tests for the JUnit XML parsing helpers.

Only `/results/.../import` parses the uploaded XML; `/history` stores it raw
and `/sync-results` stores zips. That is why this module was the least covered
in the package. Two behaviours are load-bearing and easy to break by accident:
the escape-character sanitizing, and treating xfail as passed.
"""

import http
import io
from datetime import datetime
from datetime import timezone
from pathlib import Path

import flask
import flask.testing
import pytest

from testing_results_cache import common
from testing_results_cache import junittools


def _suite(testcases: str, timestamp: str = "2026-08-05T01:00:00.000000") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
        f'<testsuite name="pytest" tests="1" time="1.0" timestamp="{timestamp}">\n'
        f"{testcases}\n</testsuite>\n</testsuites>\n"
    ).encode()


def _case(name: str, inner: str = "") -> str:
    return f'<testcase classname="pkg.test_mod.Cls" name="{name}" time="0.1">{inner}</testcase>'


def _write(tmp_path: Path, content: bytes) -> Path:
    junit_file = tmp_path / "report.xml"
    junit_file.write_bytes(content)
    return junit_file


class TestVerdicts:
    @pytest.mark.parametrize(
        ("inner", "expected"),
        [
            ("", common.VerdictValues.PASSED),
            ('<failure message="boom">tb</failure>', common.VerdictValues.FAILED),
            ('<error message="boom">tb</error>', common.VerdictValues.FAILED),
            ('<skipped type="pytest.skip">why</skipped>', common.VerdictValues.SKIPPED),
            ('<skipped type="pytest.xfail">why</skipped>', common.VerdictValues.XFAILED),
        ],
    )
    def test_verdict_for_each_outcome(self, tmp_path: Path, inner: str, expected: str) -> None:
        junit_file = _write(tmp_path, _suite(_case("test_a", inner)))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.tests_verdicts == [
            common.TestVerdict(testid="pkg.test_mod.Cls::test_a", verdict=expected)
        ]

    def test_failure_wins_over_an_earlier_error(self, tmp_path: Path) -> None:
        """`error` keeps looking; `failure` stops. Both mean failed here."""
        inner = '<error message="e">tb</error><failure message="f">tb</failure>'
        junit_file = _write(tmp_path, _suite(_case("test_a", inner)))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.tests_verdicts[0].verdict == common.VerdictValues.FAILED

    def test_skipped_with_no_type_is_a_skip_not_an_xfail(self, tmp_path: Path) -> None:
        junit_file = _write(tmp_path, _suite(_case("test_a", "<skipped>why</skipped>")))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.tests_verdicts[0].verdict == common.VerdictValues.SKIPPED


class TestSanitizing:
    def test_accepts_the_raw_escape_character_pytest_writes(self, tmp_path: Path) -> None:
        """Without `_sanitize_xml` this raises, and real reports get refused."""
        inner = '<failure message="boom">\x1b[31mred\x1b[0m</failure>'
        junit_file = _write(tmp_path, _suite(_case("test_a", inner)))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.tests_verdicts[0].verdict == common.VerdictValues.FAILED

    def test_accepts_non_ascii_test_names(self, tmp_path: Path) -> None:
        junit_file = _write(tmp_path, _suite(_case("test_wystrój")))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert "test_wystrój" in data.tests_verdicts[0].testid


class TestTimestamp:
    def test_parses_a_plain_timestamp(self, tmp_path: Path) -> None:
        junit_file = _write(tmp_path, _suite(_case("test_a")))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.timestamp == datetime(2026, 8, 5, 1, 0, 0, tzinfo=timezone.utc)

    def test_strips_a_utc_offset(self, tmp_path: Path) -> None:
        junit_file = _write(
            tmp_path, _suite(_case("test_a"), timestamp="2026-08-05T01:00:00.000000+00:00")
        )
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.timestamp == datetime(2026, 8, 5, 1, 0, 0, tzinfo=timezone.utc)

    def test_a_non_utc_offset_is_not_handled(self, tmp_path: Path) -> None:
        """Known limitation, pinned so a change is deliberate.

        Only `+00:00` is stripped, so a report written in another timezone
        raises. Runners are UTC, which is why this has never bitten.
        """
        junit_file = _write(
            tmp_path, _suite(_case("test_a"), timestamp="2026-08-05T01:00:00.000000+01:00")
        )
        with pytest.raises(ValueError, match="unconverted data remains"):
            junittools.get_testsuite_data(junit_file=junit_file)

    def test_missing_timestamp_falls_back_to_the_epoch(self, tmp_path: Path) -> None:
        content = _suite(_case("test_a")).replace(b' timestamp="2026-08-05T01:00:00.000000"', b"")
        junit_file = _write(tmp_path, content)
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.timestamp == datetime(1970, 1, 1, tzinfo=timezone.utc)


class TestRejects:
    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not xml at all", id="not-xml"),
            pytest.param(b"<?xml version='1.0'?>", id="declaration-only"),
            pytest.param(b"<a/><b/>", id="two-roots"),
        ],
    )
    def test_unparseable_input_raises_value_error(self, tmp_path: Path, content: bytes) -> None:
        junit_file = _write(tmp_path, content)
        with pytest.raises(ValueError, match="Failed to parse JUnit XML file"):
            junittools.get_testsuite_data(junit_file=junit_file)

    def test_binary_input_fails_at_the_decode(self, tmp_path: Path) -> None:
        """`UnicodeDecodeError` is a `ValueError`, so callers still catch it."""
        junit_file = _write(tmp_path, b"\x00\x01\x02\xff")
        with pytest.raises(UnicodeDecodeError):
            junittools.get_testsuite_data(junit_file=junit_file)

    def test_rejects_a_document_with_no_testsuite(self, tmp_path: Path) -> None:
        junit_file = _write(tmp_path, b"<testsuites></testsuites>")
        with pytest.raises(ValueError, match="Expecting single testsuite"):
            junittools.get_testsuite_data(junit_file=junit_file)

    def test_rejects_a_document_with_two_testsuites(self, tmp_path: Path) -> None:
        one = _suite(_case("test_a")).decode()
        two = one.replace("</testsuites>", '<testsuite name="b"></testsuite></testsuites>')
        junit_file = _write(tmp_path, two.encode())
        with pytest.raises(ValueError, match="Expecting single testsuite"):
            junittools.get_testsuite_data(junit_file=junit_file)

    def test_a_missing_file_raises_oserror_not_valueerror(self, tmp_path: Path) -> None:
        """A read failure is the server's problem, so callers can tell it apart."""
        with pytest.raises(FileNotFoundError):
            junittools.get_testsuite_data(junit_file=tmp_path / "nope.xml")


class TestEmptySuite:
    def test_a_testsuite_with_no_testcases_is_valid_and_empty(self, tmp_path: Path) -> None:
        junit_file = _write(tmp_path, _suite(""))
        data = junittools.get_testsuite_data(junit_file=junit_file)
        assert data.tests_verdicts == []

    def test_an_empty_report_imports_as_an_empty_testrun(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """End to end: an empty but valid report must not 500."""
        resp = client.put(
            "/results/emptyrun/1/import",
            headers=auth_headers,
            data={"junitxml": (io.BytesIO(_suite("")), "r.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == http.HTTPStatus.OK
        with client.get("/results/emptyrun/passed", headers=auth_headers) as passed:
            assert passed.get_data(as_text=True).strip() == ""
