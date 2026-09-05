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
