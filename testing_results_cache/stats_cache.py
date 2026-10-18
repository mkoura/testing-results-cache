"""Helper functions for the testrun_stats table.

A re-upload for the same run replaces the row, so the uploader is safe to
retry.
"""

import logging
import math
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from testing_results_cache import common

LOGGER = logging.getLogger(__name__)

# Same format as history_cache.py. See history.timestamp in schema.sql.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

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

    The timestamp comes from the client. `strftime("%Y")` does not zero-pad
    a year below 1000, and `strptime("%Y")` cannot read it back.

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


def _window_clause(
    project: tp.Optional[str], days: tp.Optional[int], origin: tp.Optional[str] = None
) -> tp.Tuple[str, tp.List]:
    """Build the shared WHERE clause for the two read paths.

    Args:
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.
        origin: Limit to one origin, `ci` or `local`, or None for both.

    Returns:
        Tuple of (WHERE clause including its leading space, bind parameters).
    """
    clauses = []
    params: tp.List = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if origin:
        clauses.append("origin = ?")
        params.append(origin)
    if days is not None:
        # Chronological as text: the format is fixed-width, and
        # timestamp_round_trips refuses a year below 1000.
        cutoff = datetime.now(UTC) - timedelta(days=days)
        clauses.append("timestamp >= ?")
        params.append(_format_timestamp(cutoff))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def get_totals(
    conn: sqlite3.Connection,
    project: tp.Optional[str] = None,
    days: tp.Optional[int] = None,
    origin: tp.Optional[str] = None,
) -> common.TestrunStatsTotals:
    """Sum the counts across every matching run.

    Args:
        conn: An open database connection.
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.
        origin: Limit to one origin, `ci` or `local`, or None for both.

    Returns:
        The summed counts, with `runs` holding how many rows were summed.
    """
    where, params = _window_clause(project, days, origin)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(cases),0), COALESCE(SUM(passed),0), COALESCE(SUM(failed),0), "
        "COALESCE(SUM(broken),0), COALESCE(SUM(skipped),0), COALESCE(SUM(never_run),0), "
        "SUM(duration) "
        f"FROM testrun_stats{where}",
        params,
    )
    row = cur.fetchone()
    duration = row[7]
    if duration is None:
        # SQLite 3.45 returns an overflowed float sum as NULL, newer versions as inf.
        duration = math.nan if row[0] else 0.0
    return common.TestrunStatsTotals(
        runs=row[0],
        cases=row[1],
        passed=row[2],
        failed=row[3],
        broken=row[4],
        skipped=row[5],
        never_run=row[6],
        duration=duration,
    )


def list_testrun_stats(
    conn: sqlite3.Connection,
    project: tp.Optional[str] = None,
    days: tp.Optional[int] = None,
    limit: int = MAX_LIST_ROWS,
    origin: tp.Optional[str] = None,
) -> tp.List[common.TestrunStatsEntry]:
    """List matching runs, newest first, skipping a row whose timestamp cannot be parsed.

    Args:
        conn: An open database connection.
        project: Limit to one project, or None for every project.
        days: Limit to runs this recent, or None for no time limit.
        limit: Maximum rows to return, clamped to MAX_LIST_ROWS.
        origin: Limit to one origin, `ci` or `local`, or None for both.

    Returns:
        Up to `limit` runs, newest first, minus any row whose stored
        timestamp could not be parsed.
    """
    where, params = _window_clause(project, days, origin)
    capped = max(1, min(limit, MAX_LIST_ROWS))
    # The limit is applied after bad rows are skipped, so they cannot shorten
    # the answer.
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
