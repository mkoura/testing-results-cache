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
import time
from pathlib import Path
from typing import List
from typing import NamedTuple
from typing import Tuple

from testing_results_cache import common
from testing_results_cache import history_cache

# Files newer than this are left out of the orphan report. An upload renames
# its XML into place and commits one line later; a prune running on another
# connection cannot see the uncommitted row, so without this grace period it
# names a live upload as an orphan, and the README says orphans are safe to
# delete.
ORPHAN_GRACE_SECONDS = 300


class Prunable(NamedTuple):
    testrun_name: str
    job_id: str
    path: Path
    # The inode recorded when the entry was selected. See `prune`.
    inode: int


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
    expired = []
    for name, job_id in cur.fetchall():
        # The only path this service builds from stored values rather than from
        # a request. `upload_history` is the sole writer to this table and
        # validates both segments, so a bad row cannot come from the app - but
        # the README tells operators to run raw sqlite3 against this database,
        # and this code deletes.
        if not (common.valid_path_segment(name) and common.valid_path_segment(job_id)):
            logging.getLogger(__name__).warning(
                f"Skipping history row with an unusable name: {name!r}/{job_id!r}"
            )
            continue
        path = history_folder / name / f"{job_id}.xml"
        try:
            inode = path.stat().st_ino
        except OSError:
            # Missing already, or unreadable. `prune` still removes the row;
            # inode 0 never matches a real file, so it never unlinks by mistake.
            inode = 0
        expired.append(Prunable(testrun_name=name, job_id=job_id, path=path, inode=inode))

    return expired


def prune(
    conn: sqlite3.Connection, history_folder: Path, days: int, dry_run: bool = False
) -> Tuple[List[Prunable], List[Prunable]]:
    """Delete history entries older than `days`. Returns what was removed.

    The file is unlinked only after its row is deleted and committed, so an
    interruption leaves a file with no row rather than a row with no file. A
    later prune cannot find that file, which is why `find_orphans` exists.

    Returns the entries whose rows were removed. `failed` on each entry says
    whether its file is still on disk.
    """
    expired = find_expired(conn=conn, history_folder=history_folder, days=days)
    if dry_run or not expired:
        return expired, []

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
    stranded = []
    for entry in expired:
        # The commit above released the write lock, which frees this
        # testrun+job for reuse. An upload landing in this loop's window
        # creates a new row and a new file at the same path, and unlinking by
        # path alone would delete that fresh file and leave its row behind -
        # the one state `upload_history` is written to avoid, unreachable
        # through the API and not repairable through it either. A new upload
        # arrives via mkstemp and rename, so it always has a different inode.
        try:
            if entry.path.stat().st_ino != entry.inode:
                logging.getLogger(__name__).info(
                    f"Leaving {entry.path} alone; it was replaced during the prune"
                )
                continue
            entry.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logging.getLogger(__name__).warning(
                f"Could not remove {entry.path}; it is now an orphan", exc_info=True
            )
            stranded.append(entry)

    return expired, stranded


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
    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    found = []
    for path in history_folder.rglob("*.xml"):
        if path in known:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found)
