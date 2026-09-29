"""Helper functions for handling data in pytest JUnit format."""

from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import List

from lxml import etree

from testing_results_cache import common


def _sanitize_xml(xml_str: str) -> str:
    """Sanitize XML string to make it valid for XML."""
    # there is a bug in pytest junit output that leads to some invalid characters in XML
    xml_fixed = xml_str.replace("\033", "#x1B")
    return xml_fixed


def _get_xml_root(junit_file: Path) -> etree._Element:
    # sanitize XML to make it valid
    _xml_str = junit_file.read_text()
    xml_str = _sanitize_xml(_xml_str)
    try:
        root = etree.fromstring(bytes(xml_str, encoding="utf-8"))
    except Exception as err:
        msg = f"Failed to parse JUnit XML file '{junit_file}'"
        raise ValueError(msg) from err

    return root


def _parse_junit_timestamp(value: str) -> datetime:
    """Parse a JUnit `<testsuite timestamp=...>` value into tz-aware UTC.

    `datetime.fromisoformat`, not a fixed `strptime` format. The previous
    fixed format accepted exactly one shape, `...%f+00:00`, and raised on
    two that pytest really emits:

    * a non-UTC offset, e.g. `+01:00`, which is what every run on a machine
      not set to UTC produces. CI runners are UTC, so this never surfaced
      while CI was the only caller.
    * a whole-second timestamp, which pytest writes without the `.%f` part
      because it calls `datetime.isoformat()`.

    Both reached `import_results` as a ValueError and were reported to the
    client as "Failed to import testrun".

    A value with no offset at all is read as UTC, which is what the old code
    did after stripping `+00:00`.

    Args:
        value: The `timestamp` attribute of the `<testsuite>` element.

    Returns:
        The timestamp as tz-aware UTC.

    Raises:
        ValueError: When the value is not a usable ISO-8601 timestamp.
    """
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        # `fromisoformat` accepts offsets up to +/-24h, so a year at either
        # end of the range converts out of `datetime`'s range in
        # `astimezone`. Re-raised as ValueError because that is what the
        # import route catches to answer 400; left as OverflowError it
        # escapes as an unhandled 500 with an HTML body. The old fixed
        # format raised ValueError for the same input, so this keeps the
        # caller's contract unchanged.
        err = f"Timestamp out of range: {value!r}"
        raise ValueError(err) from exc


def _get_verdict(testcase_record: etree._Element) -> str:
    """Parse testcase record and return it's info."""
    verdict = None
    for element in testcase_record:
        if element.tag == "error":
            verdict = common.VerdictValues.FAILED
            # continue to see if there's more telling verdict for this record
        elif element.tag == "failure":
            verdict = common.VerdictValues.FAILED
            break
        elif element.tag == "skipped":
            skip_type = element.get("type") or ""
            if "xfail" in skip_type:
                verdict = common.VerdictValues.XFAILED
            else:
                verdict = common.VerdictValues.SKIPPED
            break

    if not verdict:
        verdict = common.VerdictValues.PASSED

    return verdict


def _get_testcases_data(testsuite: etree._Element) -> List[common.TestVerdict]:
    testcases: List[etree._Element] = testsuite.xpath(".//testcase") or []  # type: ignore

    results = []
    for test_data in testcases:
        verdict = _get_verdict(test_data)

        title = test_data.get("name") or ""
        classname = test_data.get("classname") or ""

        data = common.TestVerdict(testid=f"{classname}::{title}", verdict=verdict)

        results.append(data)

    return results


def get_testsuite_data(junit_file: Path) -> common.TestsuiteData:
    """Read the content of the junit-results file produced by pytest and return imported data."""
    xml_root = _get_xml_root(junit_file)

    testsuites: List[etree._Element] = xml_root.xpath(".//testsuite") or []  # type: ignore
    if len(testsuites) != 1:
        msg = "Expecting single testsuite in JUnit XML file"
        raise ValueError(msg)

    testsuite = testsuites[0]
    testcases_data = _get_testcases_data(testsuite=testsuite)
    timestamp = _parse_junit_timestamp(testsuite.get("timestamp", "1970-01-01T00:00:00.000000"))
    testsuite_data = common.TestsuiteData(timestamp=timestamp, tests_verdicts=testcases_data)

    return testsuite_data
