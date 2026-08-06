from datetime import datetime
from typing import List
from typing import NamedTuple


class VerdictValues:
    """Verdict values."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    XFAILED = "xfailed"


class TestVerdict(NamedTuple):
    testid: str
    verdict: str


class TestsuiteData(NamedTuple):
    """Data about the testsuite."""

    timestamp: datetime
    tests_verdicts: List[TestVerdict]


class HistoryEntry(NamedTuple):
    """A dumped JUnit XML for a testrun+job - no verdict info, just when it landed."""

    job_id: str
    timestamp: datetime
