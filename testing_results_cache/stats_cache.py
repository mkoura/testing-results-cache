"""Helper functions for the testrun_stats table.

Unlike history_cache.py, which records only that a file landed, this stores
the numbers themselves - the client has already done the counting. The
server never parses a test report here.

The timestamps in this table come from the client, not from `datetime.now`,
so the storage format is not self-enforcing the way it is in the other two
cache modules. `timestamp_round_trips` exists for that, and the API layer
calls it before any row is written.

Like sync_results_cache.py, a re-upload for the same run replaces the row
instead of being rejected as a duplicate: the uploader runs at the end of a
test script and may legitimately be retried.
"""

import logging
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from testing_results_cache import common

LOGGER = logging.getLogger(__name__)

# See the comment on testrun_stats.timestamp in schema.sql - never rely on
# sqlite3's own datetime adapter/converter for this column. Same format as
# history_cache.py and sync_results_cache.py, for the same reason.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# Bound on how many rows a single listing returns, so one request cannot ask
# the service to build an unbounded JSON document. The read routes also
# accept a smaller `limit`.
MAX_LIST_ROWS = 1000


def _format_timestamp(value: datetime) -> str:
    """Render a datetime in the column's storage format.

    Args:
        value: A tz-aware UTC datetime.

    Returns:
        The value in TIMESTAMP_FORMAT.
    """
    return value.strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(value: str) -> datetime:
    """Read a stored timestamp back as tz-aware UTC.

    Args:
        value: A string in TIMESTAMP_FORMAT.

    Returns:
        The parsed datetime, with UTC attached.

    Raises:
        ValueError: When the value is not in TIMESTAMP_FORMAT.
    """
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def timestamp_round_trips(value: datetime) -> bool:
    """Check a timestamp survives being written and read back.

    Unlike the other two tables, the timestamps here come from the client, so
    the storage format cannot be assumed to fit. `strftime("%Y")` does not
    zero-pad while `strptime("%Y")` requires four digits, so a year below 1000
    formats to something that can never be parsed again. Checked by doing the
    round trip rather than by testing the year, so this stays correct if
    TIMESTAMP_FORMAT changes.

    Args:
        value: The tz-aware UTC timestamp a client sent.

    Returns:
        True when the value survives being formatted and parsed again.
    """
    try:
        return _parse_timestamp(_format_timestamp(value)) == value
    except (ValueError, OverflowError):
        return False


def save_testrun_stats(
    conn: sqlite3.Connection, entry: common.TestrunStatsEntry, user_id: int
) -> None:
    """Upsert the row for this run. Does not commit.

    `user_id` is passed separately rather than living on the entry, the
    same way sync_results_cache does it: it is an upload record, not part
    of the run's identity, and nothing reads it back.

    The caller owns the transaction, matching the other two cache modules.
    There is no file to put in place here, so the caller commits straight
    after, but keeping the split means the API layer decides what a failure
    rolls back.

    Args:
        conn: An open database connection.
        entry: The run's identity, counts and stored document.
        user_id: The authenticated uploader.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO testrun_stats("
        "project, testrun_name, run_id, step, origin, user_id, timestamp, "
        "cases, passed, failed, broken, skipped, never_run, duration, "
        "exit_code, filtered, payload"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project, testrun_name, run_id, step, origin) DO UPDATE SET "
        "user_id = excluded.user_id, timestamp = excluded.timestamp, "
        "cases = excluded.cases, passed = excluded.passed, failed = excluded.failed, "
        "broken = excluded.broken, skipped = excluded.skipped, "
        "never_run = excluded.never_run, duration = excluded.duration, "
        "exit_code = excluded.exit_code, filtered = excluded.filtered, "
        "payload = excluded.payload",
        (
            entry.project,
            entry.testrun_name,
            entry.run_id,
            entry.step,
            entry.origin,
            user_id,
            _format_timestamp(entry.timestamp),
            entry.cases,
            entry.passed,
            entry.failed,
            entry.broken,
            entry.skipped,
            entry.never_run,
            entry.duration,
            entry.exit_code,
            int(entry.filtered),
            entry.payload,
        ),
    )


def _window_clause(project: tp.Optional[str], days: tp.Optional[int]) -> tp.Tuple[str, tp.List]:
    """Build the shared WHERE clause for the two read paths.

    Interrupted runs (`never_run > 0`) are deliberately NOT excluded here.
    They are real runs and hiding them would understate the work done; the
    column is exposed instead so a caller can decide. See the note on
    `never_run` in common.TestrunStatsEntry.

    Args:
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.

    Returns:
        Tuple of (WHERE clause including its leading space, bind parameters).
    """
    clauses = []
    params: tp.List = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if days is not None:
        # Compared as text. That is sound only because every stored value is
        # zero-padded and fixed-width, which the API layer enforces with
        # `timestamp_round_trips` before any row is written - here the
        # timestamps come from the client, so unlike history_cache and
        # sync_results_cache the format is not self-enforcing.
        #
        # `timedelta`, not a float epoch round trip: same arithmetic as
        # history_cache's day window, without the lossy conversion.
        cutoff = datetime.now(UTC) - timedelta(days=days)
        clauses.append("timestamp >= ?")
        params.append(_format_timestamp(cutoff))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def get_totals(
    conn: sqlite3.Connection, project: tp.Optional[str] = None, days: tp.Optional[int] = None
) -> common.TestrunStatsTotals:
    """Sum the counts across every matching run.

    Args:
        conn: An open database connection.
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.

    Returns:
        The summed counts, with `runs` holding how many rows were summed.
    """
    where, params = _window_clause(project, days)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(cases),0), COALESCE(SUM(passed),0), COALESCE(SUM(failed),0), "
        "COALESCE(SUM(broken),0), COALESCE(SUM(skipped),0), COALESCE(SUM(never_run),0), "
        "COALESCE(SUM(duration),0) "
        f"FROM testrun_stats{where}",
        params,
    )
    row = cur.fetchone()
    return common.TestrunStatsTotals(
        runs=row[0],
        cases=row[1],
        passed=row[2],
        failed=row[3],
        broken=row[4],
        skipped=row[5],
        never_run=row[6],
        duration=row[7],
    )


def list_testrun_stats(
    conn: sqlite3.Connection,
    project: tp.Optional[str] = None,
    days: tp.Optional[int] = None,
    limit: int = MAX_LIST_ROWS,
) -> tp.List[common.TestrunStatsEntry]:
    """List matching runs, newest first.

    A row with an unparseable timestamp is skipped rather than failing the
    whole listing, matching sync_results_cache.list_sync_results - one bad
    row should not hide every other run from a caller with nothing to do
    with it. It is still logged so an operator can find it.

    Args:
        conn: An open database connection.
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.
        limit: Maximum rows to return. Values above MAX_LIST_ROWS are
            clamped here; the API layer refuses them outright instead.

    Returns:
        Up to `limit` runs, newest first, minus any row whose stored
        timestamp could not be parsed.
    """
    where, params = _window_clause(project, days)
    capped = max(1, min(limit, MAX_LIST_ROWS))
    # Fetched up to the hard cap and trimmed to `capped` only after the
    # unparseable rows are dropped. Applying the caller's limit in SQL would
    # let a single bad row inside the window shorten the answer - at
    # `limit=1`, one bad row returns an empty list while good rows sit right
    # behind it. The work stays bounded, because MAX_LIST_ROWS is the same
    # ceiling the SQL had before.
    cur = conn.cursor()
    cur.execute(
        "SELECT project, testrun_name, run_id, step, origin, timestamp, "
        "cases, passed, failed, broken, skipped, never_run, duration, "
        "exit_code, filtered, payload "
        f"FROM testrun_stats{where} ORDER BY timestamp DESC LIMIT ?",
        [*params, MAX_LIST_ROWS],
    )
    entries = []
    for row in cur.fetchall():
        try:
            parsed = _parse_timestamp(row[5])
        except ValueError:
            LOGGER.warning(
                f"Skipping malformed timestamp {row[5]!r} for run "
                f"{row[0]}/{row[1]}/{row[2]}/{row[3]}/{row[4]}"
            )
            continue
        entries.append(
            common.TestrunStatsEntry(
                project=row[0],
                testrun_name=row[1],
                run_id=row[2],
                step=row[3],
                origin=row[4],
                timestamp=parsed,
                cases=row[6],
                passed=row[7],
                failed=row[8],
                broken=row[9],
                skipped=row[10],
                never_run=row[11],
                duration=row[12],
                exit_code=row[13],
                filtered=bool(row[14]),
                payload=row[15],
            )
        )
        if len(entries) == capped:
            break
    return entries
