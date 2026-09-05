"""Helper functions for the sync-results table.

Like history_cache.py, this never inspects what's inside the zip it
stores - it only records which version's result is currently cached and
when it landed. Unlike history, there is only ever one row per version: a
new upload replaces the old one instead of being rejected as a duplicate.
"""

import sqlite3
from datetime import datetime
from datetime import timezone
from typing import List

from testing_results_cache import common

# See the comment on sync_results.timestamp in schema.sql - never rely on
# sqlite3's own datetime adapter/converter for this column. Same format as
# history_cache.py, for the same reason.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _format_timestamp(value: datetime) -> str:
    return value.strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def save_sync_results_entry(conn: sqlite3.Connection, version: str, user_id: int) -> None:
    """Upsert the row for this version. Does not commit.

    The caller owns the transaction: commit only after the zip itself is
    in place, same ordering discipline as history_cache.save_history_entry.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sync_results(version, user_id, timestamp) VALUES (?,?,?) "
        "ON CONFLICT(version) DO UPDATE SET "
        "user_id = excluded.user_id, timestamp = excluded.timestamp",
        (version, user_id, _format_timestamp(datetime.now(timezone.utc))),
    )


def sync_results_exists(conn: sqlite3.Connection, version: str) -> bool:
    """Check whether a sync-results entry exists for this version."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sync_results WHERE version = ? LIMIT 1", (version,))
    return cur.fetchone() is not None


def list_sync_results(conn: sqlite3.Connection) -> List[common.SyncResultsEntry]:
    """List every stored version's sync-results entry, newest first."""
    cur = conn.cursor()
    cur.execute("SELECT version, timestamp FROM sync_results ORDER BY timestamp DESC")
    rows = cur.fetchall()
    entries = []
    for version, timestamp in rows:
        try:
            parsed = _parse_timestamp(timestamp)
        except ValueError as exc:
            # One bad row 500s the whole listing - name it, so the operator
            # doesn't need a table scan to find it.
            msg = f"Malformed timestamp {timestamp!r} for version {version}"
            raise ValueError(msg) from exc
        entries.append(common.SyncResultsEntry(version=version, timestamp=parsed))
    return entries
