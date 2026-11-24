"""Helper functions for the sync-results table.

Like history_cache.py, this records which runs have a zip stored and when it
landed. A run is one network, one cardano-node version and one start time,
and a new upload for the same run replaces the old one instead of being
rejected as a duplicate. The `facts` column is filled in after the upload
by sync_analyzer.py, which is the one reader of the zip's contents.
"""

import json
import logging
import sqlite3
from datetime import UTC
from datetime import datetime
from typing import List
from typing import Optional

from testing_results_cache import common

LOGGER = logging.getLogger(__name__)

# See the comment on sync_results.timestamp in schema.sql - never rely on
# sqlite3's own datetime adapter/converter for this column. Same format as
# history_cache.py, for the same reason.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def save_sync_results_entry(
    conn: sqlite3.Connection,
    environment: str,
    version: str,
    started: datetime,
    user_id: int,
    facts: Optional[dict],
) -> int:
    """Upsert the row for this run and return its id. Does not commit.

    The caller owns the transaction: commit only after the zip itself is
    in place, same ordering discipline as history_cache.save_history_entry.
    A replaced run keeps its id, so the zip on disk keeps its name.
    """
    cur = conn.cursor()
    key = (environment, version, _format_timestamp(started))
    cur.execute(
        "INSERT INTO sync_results(environment, version, started, user_id, timestamp, facts) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(environment, version, started) DO UPDATE SET "
        "user_id = excluded.user_id, timestamp = excluded.timestamp, facts = excluded.facts",
        (*key, user_id, _format_timestamp(datetime.now(UTC)), _facts_text(facts)),
    )
    cur.execute(
        "SELECT id FROM sync_results WHERE environment = ? AND version = ? AND started = ?",
        key,
    )
    run_id: int = cur.fetchone()[0]
    return run_id


def _facts_text(facts: Optional[dict]) -> Optional[str]:
    return None if facts is None else json.dumps(facts, separators=(",", ":"), allow_nan=False)


def save_facts(conn: sqlite3.Connection, run_id: int, facts: Optional[dict]) -> None:
    """Store the analyzer's numbers for a run. Does not commit."""
    conn.execute("UPDATE sync_results SET facts = ? WHERE id = ?", (_facts_text(facts), run_id))


def _entry(row: tuple) -> Optional[common.SyncResultsEntry]:
    run_id, environment, version, started, timestamp, facts = row
    try:
        parsed_started = _parse_timestamp(started)
        parsed_timestamp = _parse_timestamp(timestamp)
    except ValueError:
        LOGGER.warning(f"Skipping malformed timestamps on sync-results row {run_id}")
        return None
    try:
        parsed_facts = None if facts is None else json.loads(facts)
    except ValueError:
        LOGGER.warning(f"Ignoring malformed facts on sync-results row {run_id}")
        parsed_facts = None
    return common.SyncResultsEntry(
        id=run_id,
        environment=environment,
        version=version,
        started=parsed_started,
        timestamp=parsed_timestamp,
        facts=parsed_facts,
    )


def get_sync_results(conn: sqlite3.Connection, run_id: int) -> Optional[common.SyncResultsEntry]:
    """Return the entry for one run, or None when there is no such run."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, environment, version, started, timestamp, facts FROM sync_results WHERE id = ?",
        (run_id,),
    )
    row = cur.fetchone()
    return None if row is None else _entry(row)


def list_sync_results(conn: sqlite3.Connection) -> List[common.SyncResultsEntry]:
    """List every stored run, newest start first.

    A row with an unparseable timestamp is skipped rather than failing the
    whole listing - one corrupted row should not also hide every other
    run's entry from a caller who has nothing to do with it. It is still
    logged by id, so an operator doesn't need a table scan to find it.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, environment, version, started, timestamp, facts FROM sync_results "
        "ORDER BY started DESC, id DESC"
    )
    entries = [_entry(row) for row in cur.fetchall()]
    return [entry for entry in entries if entry is not None]
