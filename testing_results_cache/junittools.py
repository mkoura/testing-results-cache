"""Helper functions for handling data in pytest JUnit format."""
from datetime import datetime
from pathlib import Path
from typing import List

from lxml import etree

from testing_results_cache import common


def _sanitize_xml(xml_str: str) -> str:
    """Sanitize XML string to make it valid for XML."""
    # there is a bug in pytest junit output that leads to some invalid characters in XML
    xml_str = xml_str.replace("\033", "#x1B")
    return xml_str


def _get_xml_root(junit_file: Path) -> etree._Element:
    # sanitize XML to make it valid
    _xml_str = junit_file.read_text()
    xml_str = _sanitize_xml(_xml_str)
    try:
        root = etree.fromstring(bytes(xml_str, encoding="utf-8"))
    except Exception as err:
        raise ValueError("Failed to parse JUnit XML file '{junit_file}'") from err

    return root


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
        raise ValueError("Expecting single testsuite in JUnit XML file")

    testsuite = testsuites[0]
    testcases_data = _get_testcases_data(testsuite=testsuite)
    timestamp_str = testsuite.get("timestamp", "1970-01-01T00:00:00.000000")
    timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%f")
    testsuite_data = common.TestsuiteData(timestamp=timestamp, tests_verdicts=testcases_data)

    return testsuite_data
