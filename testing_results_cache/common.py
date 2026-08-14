from datetime import datetime
from typing import List
from typing import NamedTuple

# JUnit XML is the only upload format accepted anywhere in the service.
ALLOWED_EXTENSIONS = frozenset({".xml"})


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
    """Record that a JUnit XML dump exists for a job - no verdict info, just when it landed.

    `timestamp` is always tz-aware UTC - attached by `history_cache._parse_timestamp`
    when rows are read back in `get_history_entries` (currently the only place
    this tuple is constructed). `job_id` was validated at the API layer before
    insert (history_api rejects anything outside [A-Za-z0-9_.-]); rows inserted
    by other means bypass that check.
    """

    job_id: str
    timestamp: datetime
