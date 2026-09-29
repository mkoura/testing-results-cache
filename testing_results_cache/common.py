import re
from datetime import datetime
from typing import List
from typing import NamedTuple
from typing import NoReturn

import flask

# The only upload format /results and /history accept.
ALLOWED_EXTENSIONS = frozenset({".xml"})

# The only upload format the sync-results endpoint accepts.
ALLOWED_SYNC_RESULTS_EXTENSIONS = frozenset({".zip"})

MAX_PATH_SEGMENT_LENGTH = 200
# Dots are allowed so real-world testrun names like "node-8.5.0" work, but a
# segment of dots only ("..", ".") is rejected in `valid_path_segment`.
_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")


def abort_json(status_code: int, message: str, headers: dict | None = None) -> NoReturn:
    """Abort the request with a JSON error body."""
    response = flask.jsonify(message=message)
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    flask.abort(response)


def valid_path_segment(value: str) -> bool:
    # fullmatch, not match: `$` in a pattern would still accept a trailing
    # newline ("job1%0A" in the URL), fullmatch requires the whole string.
    return (
        len(value) <= MAX_PATH_SEGMENT_LENGTH
        and _SAFE_SEGMENT_RE.fullmatch(value) is not None
        and value.strip(".") != ""
    )


def reject_invalid_segments(*values: str) -> None:
    """Refuse any URL segment that would be interpolated into a file path.

    Every route that builds a path from user input has to call this. A
    percent-encoded `..` survives the router, so without it an upload lands
    outside the folder meant to hold it.
    """
    for value in values:
        if not valid_path_segment(value):
            # Logged as well as refused: the access log cannot tell this
            # route's several 400s apart, and a traversal attempt should leave
            # a trace an operator can find.
            # Both values are repr'd. The path is caller-controlled too, and a
            # percent-encoded newline in it reaches here decoded, so writing it
            # raw lets an authenticated caller add whatever lines they like to
            # the log around this warning.
            flask.current_app.logger.warning(
                f"Rejected invalid path segment {value!r} on {flask.request.path!r}"
            )
            abort_json(
                400,
                f"Invalid path segment {value!r}: only [A-Za-z0-9_.-] "
                f"(not dots alone), max {MAX_PATH_SEGMENT_LENGTH} chars",
            )


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


class SyncResultsEntry(NamedTuple):
    """Record that a sync-results zip exists for a node version, and when it landed.

    Unlike HistoryEntry, there is only ever one row per version: a new
    upload replaces the old one instead of being rejected as a duplicate.
    `timestamp` is always tz-aware UTC, attached by
    `sync_results_cache._parse_timestamp` when rows are read back.
    """

    version: str
    timestamp: datetime


class TestrunStatsEntry(NamedTuple):
    """Counts for one test run, as uploaded by the client.

    The five identity fields together are unique. `cases` is the total after
    the client grouped its result files per test, and `passed`/`failed`/
    `broken`/`skipped` are allure statuses - anything else the client saw is
    `cases` minus those four, so it is derived rather than stored.

    `never_run` is a subset of `skipped`, not a sibling bucket: a test the
    registration pass registered and the real run never reached carries
    status "skipped". Above zero it means the run was interrupted, so every
    count here is a floor rather than a total.

    Attributes:
        project: The repository the testrun belongs to.
        testrun_name: The name the testrun was reported under.
        run_id: The CI run number, or a generated id for a local run.
        step: The step name, `main` unless this is upgrade testing.
        origin: `ci` or `local`.
        timestamp: When the testrun started, tz-aware UTC. Attached by
            `stats_cache._parse_timestamp` when rows are read back.
        cases: Total tests after grouping.
        passed: Tests with allure status `passed`.
        failed: Tests with allure status `failed`.
        broken: Tests with allure status `broken`.
        skipped: Tests with allure status `skipped`.
        never_run: Registered tests that never got a real result.
        duration: Wall clock span of the testrun, in seconds.
        exit_code: pytest's own exit code.
        filtered: True when the run covered only a subset of the tests.
        payload: The uploaded JSON document, re-serialised canonically. The
            values all survive; key order and whitespace do not.
    """

    project: str
    testrun_name: str
    run_id: str
    step: str
    origin: str
    timestamp: datetime
    cases: int
    passed: int
    failed: int
    broken: int
    skipped: int
    never_run: int
    duration: float
    exit_code: int
    filtered: bool
    payload: str

    @property
    def other(self) -> int:
        """Tests whose status was none of the four allure statuses.

        Returns:
            `cases` minus the four status buckets, which is never negative
            because the API layer refuses a payload whose buckets exceed the
            total.
        """
        return self.cases - self.passed - self.failed - self.broken - self.skipped


class TestrunStatsTotals(NamedTuple):
    """Sums across a set of runs, for the "how much testing did we do" question.

    Attributes:
        runs: How many rows were summed.
        cases: Total tests across those runs.
        passed: Tests with allure status `passed`.
        failed: Tests with allure status `failed`.
        broken: Tests with allure status `broken`.
        skipped: Tests with allure status `skipped`.
        never_run: Registered tests that never got a real result. Above zero
            means interrupted runs were included, so the other totals are a
            floor rather than a total.
        duration: Summed wall clock time, in seconds.
    """

    runs: int
    cases: int
    passed: int
    failed: int
    broken: int
    skipped: int
    # Summed as well as the buckets: without it a caller of the aggregate has
    # no way to know that interrupted runs were included, and the totals would
    # read as complete when they are a floor.
    never_run: int
    duration: float

    @property
    def other(self) -> int:
        """Tests whose status was none of the four allure statuses.

        Returns:
            `cases` minus the four status buckets, which is never negative
            because the API layer refuses a payload whose buckets exceed the
            total.
        """
        return self.cases - self.passed - self.failed - self.broken - self.skipped
