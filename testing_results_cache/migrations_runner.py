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
    found = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            msg = f"Migration filename {path.name!r} is not <number>_<name>.sql"
            raise MigrationError(msg)
        found.append(Migration(version=int(match.group(1)), name=match.group(2), path=path))

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        msg = f"Duplicate migration version in {migrations_dir}"
        raise MigrationError(msg)

    return found


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest migration applied, or 0 if none ever were."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def pending(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Return the migrations not yet applied.

    Raises if the database is ahead of the code.
    """
    applied = current_version(conn)
    found = available(migrations_dir)
    highest = max((m.version for m in found), default=0)

    if applied > highest:
        # The deployment is older than the database it points at. Applying
        # anything now would be guesswork, so do nothing and say why.
        msg = (
            f"Database is at version {applied}, but this release only knows about "
            f"{highest}. Refusing to migrate a database written by a newer release."
        )
        raise MigrationError(msg)

    return [m for m in found if m.version > applied]


def apply(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Apply every pending migration in order. Returns the ones applied."""
    todo = pending(conn=conn, migrations_dir=migrations_dir)

    applied = []
    for migration in todo:
        # The version row goes INSIDE the script's transaction, not before it.
        # `executescript` commits any transaction that is already open, so an
        # INSERT issued beforehand would survive a script that then fails, and
        # the migration would be recorded as applied without having run.
        #
        # `name` is safe to inline: `_MIGRATION_RE` restricts it to
        # [a-z0-9_], and executescript takes no parameters.
        script = (
            "BEGIN;\n"
            f"{migration.path.read_text()}\n"
            "INSERT INTO schema_version(version, name) VALUES "
            f"({migration.version}, '{migration.name}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as err:
            conn.rollback()
            msg = f"Migration {migration.path.name} failed: {err}"
            raise MigrationError(msg) from err

        applied.append(migration)

    return applied
