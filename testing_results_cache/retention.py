"""Remove history entries older than a cutoff.

Nothing else in this service ever deletes. `?days=n` filters a query, it does
not prune, so without this every stored report is kept forever and only
someone with shell access to the host can reverse it.

Deliberately a command rather than an HTTP route. Retention runs on a
schedule, not on request, and an endpoint that deletes is a wider surface
than the problem needs.

The row and its file go together. Removing one and not the other leaves
either a row pointing at a missing file, or a file no listing mentions, which
are the two states the upload path already works to avoid.
"""

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import List
from typing import NamedTuple

from testing_results_cache import history_cache


class Prunable(NamedTuple):
    testrun_name: str
    job_id: str
    path: Path


def find_expired(conn: sqlite3.Connection, history_folder: Path, days: int) -> List[Prunable]:
    """Return the history entries older than `days`, newest cutoff first."""
    if days < 1:
        msg = "days must be at least 1"
        raise ValueError(msg)

    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=days)
    cur = conn.cursor()
    cur.execute(
        "SELECT testrun_name, job_id FROM history WHERE timestamp < ?",
        (history_cache._format_timestamp(cutoff),),
    )
    return [
        Prunable(
            testrun_name=name,
            job_id=job_id,
            path=history_folder / name / f"{job_id}.xml",
        )
        for name, job_id in cur.fetchall()
    ]


def prune(
    conn: sqlite3.Connection, history_folder: Path, days: int, dry_run: bool = False
) -> List[Prunable]:
    """Delete history entries older than `days`. Returns what was removed.

    The file is unlinked only after its row is deleted and committed, so an
    interruption leaves a file with no row rather than a row with no file. A
    later prune cannot find that file, which is why `find_orphans` exists.
    """
    expired = find_expired(conn=conn, history_folder=history_folder, days=days)
    if dry_run or not expired:
        return expired

    cur = conn.cursor()
    cur.executemany(
        "DELETE FROM history WHERE testrun_name = ? AND job_id = ?",
        [(e.testrun_name, e.job_id) for e in expired],
    )
    conn.commit()

    # A file that will not unlink must not abort the run: the rows are already
    # committed, so aborting here loses both the removal report and the orphan
    # report below it. The leftover file has no row now, so the caller's
    # `find_orphans` names it.
    for entry in expired:
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                f"Could not remove {entry.path}; it is now an orphan", exc_info=True
            )

    return expired


def find_orphans(conn: sqlite3.Connection, history_folder: Path) -> List[Path]:
    """Return stored files that no history row mentions.

    An interrupted prune, or a crash between the commit and the unlink, leaves
    these behind. Nothing else finds them.
    """
    if not history_folder.is_dir():
        return []

    known = {
        history_folder / name / f"{job_id}.xml"
        for name, job_id in conn.execute("SELECT testrun_name, job_id FROM history")
    }
    return sorted(p for p in history_folder.rglob("*.xml") if p not in known)
