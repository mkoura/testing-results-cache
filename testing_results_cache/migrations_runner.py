"""Apply numbered SQL migrations to the database, in order, once each.

Deliberately small. This service has one sqlite file and a handful of schema
changes a year, so Alembic would be more machinery than the problem needs.

Every migration runs inside a single transaction with its `schema_version`
row, so a failure leaves the database exactly as it was. The runner refuses
to act at all if the database is ahead of the code, because that means the
deployment is running an older release than the file it is pointed at.
"""

import re
import sqlite3
from pathlib import Path
from typing import List
from typing import NamedTuple
from typing import Set

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# "001_baseline.sql" -> version 1, name "baseline"
_MIGRATION_RE = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")


class Migration(NamedTuple):
    version: int
    name: str
    path: Path


class MigrationError(Exception):
    """A migration could not be applied, or the database is in a state we refuse to touch."""


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )


def available(migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Return every migration on disk, lowest version first."""
    if not migrations_dir.is_dir():
        # `glob` on a missing directory returns nothing rather than raising, so
        # without this a deployment that lost its package data reports
        # "Up to date at version 0" from the very command run to confirm it.
        msg = f"No migrations directory at {migrations_dir}"
        raise MigrationError(msg)

    found = []
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            msg = f"Migration filename {path.name!r} is not <number>_<name>.sql"
            raise MigrationError(msg)
        found.append(Migration(version=int(match.group(1)), name=match.group(2), path=path))

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        msg = f"Duplicate migration version in {migrations_dir}"
        raise MigrationError(msg)

    # By version, not by filename. The filename pattern allows an unpadded
    # number, and sorting those as strings puts 10 before 3.
    found.sort()
    return found


def _applied_versions(conn: sqlite3.Connection) -> Set[int]:
    """Return every migration version recorded as applied.

    A pure read. Creating the table here would make `migrate --dry-run` change
    the database it is only meant to describe. A missing table and an empty one
    mean the same thing: nothing has been applied.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return set()
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_version")}


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest migration applied, or 0 if none ever were."""
    return max(_applied_versions(conn), default=0)


def pending(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Return the migrations not yet applied.

    Raises if the database is ahead of the code.
    """
    applied = _applied_versions(conn)
    found = available(migrations_dir)
    highest = max((m.version for m in found), default=0)

    if max(applied, default=0) > highest:
        # The deployment is older than the database it points at. Applying
        # anything now would be guesswork, so do nothing and say why.
        msg = (
            f"Database is at version {max(applied, default=0)}, but this release only knows about "
            f"{highest}. Refusing to migrate a database written by a newer release."
        )
        raise MigrationError(msg)

    # Membership, not "greater than the highest". A migration that failed while
    # a higher-numbered one succeeded must still be retried, not skipped for
    # good because the maximum moved past it.
    return [m for m in found if m.version not in applied]


def stamp_as_current(
    conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> List[Migration]:
    """Record every available migration as applied, without running any.

    For `init-db`, which builds the finished schema directly from schema.sql.
    The statements would be redundant; the bookkeeping still has to be right.
    """
    found = available(migrations_dir)
    _ensure_version_table(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO schema_version(version, name) VALUES (?, ?)",
        [(m.version, m.name) for m in found],
    )
    return found


def apply(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Apply every pending migration in order. Returns the ones applied."""
    todo = pending(conn=conn, migrations_dir=migrations_dir)
    if todo:
        # Here rather than in the read path, so a dry run stays a dry run.
        _ensure_version_table(conn)

    applied = []
    for migration in todo:
        # The version row goes INSIDE the script's transaction, not before it.
        # `executescript` commits any transaction that is already open, so an
        # INSERT issued beforehand would survive a script that then fails, and
        # the migration would be recorded as applied without having run.
        #
        # `name` is safe to inline: `_MIGRATION_RE` restricts it to
        # [a-z0-9_], and executescript takes no parameters.
        try:
            # Inside the try: read_text raises OSError or UnicodeDecodeError,
            # neither of which is a sqlite3.Error, and the CLI catches only
            # MigrationError. Outside, an unreadable file is a raw traceback.
            script = (
                "BEGIN;\n"
                f"{migration.path.read_text()}\n"
                "INSERT INTO schema_version(version, name) VALUES "
                f"({migration.version}, '{migration.name}');\n"
                "COMMIT;"
            )
            conn.executescript(script)
        except (sqlite3.Error, OSError, UnicodeDecodeError) as err:
            conn.rollback()
            msg = f"Migration {migration.path.name} failed: {err}"
            raise MigrationError(msg) from err

        applied.append(migration)

    return applied
