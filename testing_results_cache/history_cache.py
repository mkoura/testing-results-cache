"""Helper functions for the nightly-history table.

Unlike `results_cache.py`, this never parses the JUnit XML it stores - the
caller (e.g. the AI failure-analysis step) decides what a failure is, this
module only records that a dump exists and when, so a time-range or
job_id lookup doesn't need to stat every file on disk.
"""

import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import List

from testing_results_cache import common

# See the comment on history.timestamp in schema.sql - never rely on
# sqlite3's own datetime adapter/converter for this column. This fixed-width,
# zero-padded, offset-free format also makes the lexicographic comparison in
# the `timestamp >= ?` SQL below match chronological order.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _format_timestamp(value: datetime) -> str:
    return value.strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def save_history_entry(
    conn: sqlite3.Connection, testrun_name: str, job_id: str, user_id: int
) -> bool:
    """Insert a new history entry without committing. Returns False if one already exists.

    The caller owns the transaction: commit only after any state the row
    promises (e.g. the XML file) is in place.
    """
    # ON CONFLICT ... DO NOTHING (vs INSERT OR IGNORE) means NOT NULL and
    # CHECK violations still raise - bugs that must surface, not get
    # reported to the client as an existing entry. The explicit conflict
    # target additionally keeps any future second UNIQUE constraint from
    # being misread as "duplicate".
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?) "
        "ON CONFLICT(testrun_name, job_id) DO NOTHING",
        (testrun_name, job_id, user_id, _format_timestamp(datetime.now(UTC))),
    )
    return cur.rowcount == 1


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
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id, timestamp FROM history WHERE testrun_name = ? AND timestamp >= ? "
        "ORDER BY timestamp DESC",
        (testrun_name, _format_timestamp(cutoff)),
    )
    rows = cur.fetchall()
    entries = []
    for job_id, timestamp in rows:
        try:
            parsed = _parse_timestamp(timestamp)
        except ValueError as exc:
            # One bad row 500s the whole listing - name it, so the operator
            # doesn't need a table scan to find it.
            msg = f"Malformed timestamp {timestamp!r} for {testrun_name}/{job_id}"
            raise ValueError(msg) from exc
        entries.append(common.HistoryEntry(job_id=job_id, timestamp=parsed))
    return entries
