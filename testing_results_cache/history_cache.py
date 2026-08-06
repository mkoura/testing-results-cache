"""Helper functions for the nightly-history table.

Unlike `results_cache.py`, this never parses the JUnit XML it stores - the
caller (e.g. the AI failure-analysis step) decides what a failure is, this
module only records that a dump exists and when, so a time-range or
job_id lookup doesn't need to stat every file on disk.
"""

import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List

from testing_results_cache import common

# See the comment on history.timestamp in schema.sql - never rely on
# sqlite3's own datetime adapter/converter for this column.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _format_timestamp(value: datetime) -> str:
    return value.strftime(_TIMESTAMP_FORMAT)


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def save_history_entry(
    conn: sqlite3.Connection, testrun_name: str, job_id: str, user_id: int
) -> bool:
    """Record a new history entry. Returns False if one already exists."""
    try:
        conn.execute(
            "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?)",
            (testrun_name, job_id, user_id, _format_timestamp(datetime.now(timezone.utc))),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def history_entry_exists(conn: sqlite3.Connection, testrun_name: str, job_id: str) -> bool:
    """Check whether a history entry exists for this testrun+job.

    Any logged-in user may read any testrun's history - job_id/testrun_name
    is the identity that matters here, not who uploaded it.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM history WHERE testrun_name = ? AND job_id = ? LIMIT 1",
        (testrun_name, job_id),
    )
    return cur.fetchone() is not None


def get_history_entries(
    conn: sqlite3.Connection, testrun_name: str, days: int
) -> List[common.HistoryEntry]:
    """Get history entries for a testrun within the last `days` days, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id, timestamp FROM history WHERE testrun_name = ? AND timestamp >= ? "
        "ORDER BY timestamp DESC",
        (testrun_name, _format_timestamp(cutoff)),
    )
    rows = cur.fetchall()
    return [
        common.HistoryEntry(job_id=job_id, timestamp=_parse_timestamp(timestamp))
        for job_id, timestamp in rows
    ]
