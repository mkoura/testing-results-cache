"""Tests for the migration runner.

The runner is the one piece of this service that writes to a live database
outside a request, so the properties that matter are: it is idempotent, it
never half-applies, and it refuses to guess when the database is ahead of the
code.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from testing_results_cache import migrations_runner

# The two migrations this repo ships.
SHIPPED_MIGRATIONS = 2
SEVENTH = 7


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "test.db")


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "001_first.sql").write_text("CREATE TABLE IF NOT EXISTS a (id INTEGER);")
    (folder / "002_second.sql").write_text("CREATE TABLE IF NOT EXISTS b (id INTEGER);")
    return folder


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestApply:
    def test_applies_every_migration_in_order(
        self, conn: sqlite3.Connection, migrations: Path
    ) -> None:
        applied = migrations_runner.apply(conn=conn, migrations_dir=migrations)
        assert [m.version for m in applied] == [1, 2]
        assert {"a", "b"} <= _tables(conn)
        assert migrations_runner.current_version(conn) == SHIPPED_MIGRATIONS

    def test_is_idempotent(self, conn: sqlite3.Connection, migrations: Path) -> None:
        migrations_runner.apply(conn=conn, migrations_dir=migrations)
        assert migrations_runner.apply(conn=conn, migrations_dir=migrations) == []
        assert migrations_runner.current_version(conn) == SHIPPED_MIGRATIONS

    def test_applies_only_what_is_pending(self, conn: sqlite3.Connection, migrations: Path) -> None:
        migrations_runner.apply(conn=conn, migrations_dir=migrations)
        (migrations / "003_third.sql").write_text("CREATE TABLE IF NOT EXISTS c (id INTEGER);")
        applied = migrations_runner.apply(conn=conn, migrations_dir=migrations)
        assert [m.version for m in applied] == [3]

    def test_a_fresh_database_reports_version_zero(self, conn: sqlite3.Connection) -> None:
        assert migrations_runner.current_version(conn) == 0


class TestFailureIsAtomic:
    def test_a_broken_migration_leaves_nothing_behind(
        self, conn: sqlite3.Connection, migrations: Path
    ) -> None:
        """The version row and the SQL land together, or neither does."""
        (migrations / "003_broken.sql").write_text("CREATE TABLE c (id INTEGER); NOT SQL;")

        broken_msg = re.escape("003_broken.sql failed")
        with pytest.raises(migrations_runner.MigrationError, match=broken_msg):
            migrations_runner.apply(conn=conn, migrations_dir=migrations)

        # The two good ones stand, the broken one did not record itself.
        assert migrations_runner.current_version(conn) == SHIPPED_MIGRATIONS
        assert "c" not in _tables(conn)

    def test_a_retry_after_a_fix_succeeds(self, conn: sqlite3.Connection, migrations: Path) -> None:
        broken = migrations / "003_broken.sql"
        broken.write_text("CREATE TABLE c (id INTEGER); NOT SQL;")
        with pytest.raises(migrations_runner.MigrationError):
            migrations_runner.apply(conn=conn, migrations_dir=migrations)

        broken.write_text("CREATE TABLE IF NOT EXISTS c (id INTEGER);")
        applied = migrations_runner.apply(conn=conn, migrations_dir=migrations)
        assert [m.version for m in applied] == [3]
        assert "c" in _tables(conn)


class TestRefusesToGuess:
    def test_refuses_a_database_newer_than_the_code(
        self, conn: sqlite3.Connection, migrations: Path
    ) -> None:
        """An older release pointed at a newer database must do nothing."""
        migrations_runner.apply(conn=conn, migrations_dir=migrations)
        conn.execute("INSERT INTO schema_version(version, name) VALUES (99, 'from-the-future')")
        conn.commit()

        with pytest.raises(migrations_runner.MigrationError, match="newer release"):
            migrations_runner.pending(conn=conn, migrations_dir=migrations)

    def test_refuses_a_database_exactly_one_version_ahead(
        self, conn: sqlite3.Connection, migrations: Path
    ) -> None:
        """The realistic distance is one, not ninety-seven.

        One release rolled back leaves the database a single migration ahead.
        A guard tested only at a large gap proves the guard exists, not where
        it sits.
        """
        migrations_runner.apply(conn=conn, migrations_dir=migrations)
        conn.execute("INSERT INTO schema_version(version, name) VALUES (3, 'next_release')")
        conn.commit()

        with pytest.raises(migrations_runner.MigrationError, match="newer release"):
            migrations_runner.pending(conn=conn, migrations_dir=migrations)

    def test_rejects_a_badly_named_file(self, tmp_path: Path) -> None:
        folder = tmp_path / "m"
        folder.mkdir()
        (folder / "no-number.sql").write_text("SELECT 1;")
        bad_name_msg = re.escape("not <number>_<name>.sql")
        with pytest.raises(migrations_runner.MigrationError, match=bad_name_msg):
            migrations_runner.available(folder)

    def test_rejects_a_duplicate_version(self, tmp_path: Path) -> None:
        folder = tmp_path / "m"
        folder.mkdir()
        (folder / "001_a.sql").write_text("SELECT 1;")
        (folder / "001_b.sql").write_text("SELECT 1;")
        with pytest.raises(migrations_runner.MigrationError, match="Duplicate migration version"):
            migrations_runner.available(folder)


class TestTheRealMigrations:
    """The shipped migrations, against the deployment they will actually meet."""

    def test_a_pre_history_database_migrates_without_losing_data(
        self, conn: sqlite3.Connection
    ) -> None:
        """Martin's live database predates /history and /sync-results."""
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL);"
            "CREATE TABLE testrun (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL);"
            "CREATE TABLE results (id INTEGER PRIMARY KEY, test_name TEXT NOT NULL,"
            " verdict TEXT NOT NULL, testrun_id INTEGER NOT NULL, user_id INTEGER);"
        )
        conn.execute("INSERT INTO testrun(name) VALUES ('legacy')")
        conn.execute(
            "INSERT INTO results(test_name, verdict, testrun_id, user_id)"
            " VALUES ('test_a','passed',1,1)"
        )
        conn.commit()
        before = conn.execute("SELECT test_name, verdict FROM results").fetchall()

        migrations_runner.apply(conn=conn)

        assert conn.execute("SELECT test_name, verdict FROM results").fetchall() == before
        assert conn.execute("SELECT name FROM testrun").fetchall() == [("legacy",)]
        assert {"history", "sync_results"} <= _tables(conn)

    def test_running_twice_changes_nothing(self, conn: sqlite3.Connection) -> None:
        migrations_runner.apply(conn=conn)
        version = migrations_runner.current_version(conn)
        assert migrations_runner.apply(conn=conn) == []
        assert migrations_runner.current_version(conn) == version

    def test_the_indexes_are_created(self, conn: sqlite3.Connection) -> None:
        migrations_runner.apply(conn=conn)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_results_testrun_user" in indexes

    def test_the_index_changes_the_query_plan(self, conn: sqlite3.Connection) -> None:
        """The reason the index exists, rather than just that it is present."""
        migrations_runner.apply(conn=conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT test_name, verdict FROM results"
            " WHERE testrun_id = ? AND user_id = ?",
            (1, 1),
        ).fetchone()[-1]
        assert "idx_results_testrun_user" in plan

    def test_schema_sql_and_the_migrations_agree(self, tmp_path: Path) -> None:
        """A fresh install and a migrated one must end up the same shape."""
        from testing_results_cache import flask_db  # noqa: PLC0415

        fresh = sqlite3.connect(tmp_path / "fresh.db")
        schema = Path(flask_db.__file__).parent / "schema.sql"
        fresh.executescript(schema.read_text())

        migrated = sqlite3.connect(tmp_path / "migrated.db")
        migrations_runner.apply(conn=migrated)

        def objects(conn: sqlite3.Connection) -> set:
            return {
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT type, name FROM sqlite_master"
                    " WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_version'"
                )
            }

        def columns(conn: sqlite3.Connection, table: str) -> list:
            # name, type, notnull, primary key. Comparing only object names
            # lets a column difference through, and the two lineages did
            # differ on `sync_results` while this test was passing.
            return [
                (r[1], r[2].upper(), r[3], r[5])
                for r in conn.execute(f"PRAGMA table_info({table})")
            ]

        assert objects(fresh) == objects(migrated)
        for kind, name in sorted(objects(fresh)):
            if kind == "table":
                assert columns(fresh, name) == columns(migrated, name), (
                    f"{name} differs between a fresh install and a migrated one"
                )


class TestOrdering:
    def test_migrations_run_in_numeric_order_not_filename_order(self, tmp_path: Path) -> None:
        """Sorting filenames as strings puts 10 before 3.

        The filename pattern accepts an unpadded number, so the first person to
        write `3_x.sql` next to `010_y.sql` gets them applied backwards.
        """
        folder = tmp_path / "migrations"
        folder.mkdir()
        (folder / "3_third.sql").write_text("CREATE TABLE IF NOT EXISTS c (id INTEGER);")
        (folder / "010_tenth.sql").write_text("CREATE TABLE IF NOT EXISTS j (id INTEGER);")

        found = migrations_runner.available(folder)

        assert [m.version for m in found] == [3, 10]

    def test_a_migration_that_failed_is_retried_later(self, tmp_path: Path) -> None:
        """Pending is membership, not "above the highest applied".

        If a lower-numbered migration fails while a higher one has already
        succeeded, comparing against the maximum skips the failed one for good
        and every later run reports success.
        """
        folder = tmp_path / "migrations"
        folder.mkdir()
        (folder / "001_broken.sql").write_text("CREATE TABLE a (id INTEGER); SELECT nope;")
        (folder / "002_fine.sql").write_text("CREATE TABLE IF NOT EXISTS b (id INTEGER);")
        conn = sqlite3.connect(tmp_path / "t.db")

        # Apply 002 on its own first, so the maximum is ahead of the broken one.
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO schema_version(version, name) VALUES (2, 'fine');"
        )

        still_todo = migrations_runner.pending(conn=conn, migrations_dir=folder)

        assert [m.version for m in still_todo] == [1]

    def test_current_version_is_the_highest_not_the_count(self, conn: sqlite3.Connection) -> None:
        """They agree only while the applied versions are contiguous."""
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO schema_version(version, name) VALUES (7, 'seventh');"
        )
        assert migrations_runner.current_version(conn) == SEVENTH


class TestDryRunIsADryRun:
    def test_reading_the_version_does_not_create_the_table(self, conn: sqlite3.Connection) -> None:
        """`migrate --dry-run` goes through here.

        A dry run that writes to the database it is only describing breaks the
        promise of the flag an operator reaches for when nervous.
        """
        migrations_runner.current_version(conn)
        migrations_runner.pending(conn=conn)

        assert "schema_version" not in _tables(conn)


class TestMisconfiguredDeployment:
    def test_a_missing_migrations_directory_is_an_error(self, tmp_path: Path) -> None:
        """Not a silent success.

        `glob` on a missing directory returns nothing rather than raising, so
        without a check the command run to confirm a deployment reports
        "Up to date at version 0" on a database that has had nothing applied.
        """
        with pytest.raises(migrations_runner.MigrationError, match="No migrations directory"):
            migrations_runner.available(tmp_path / "not-here")

    def test_an_unreadable_migration_is_a_migration_error(
        self, conn: sqlite3.Connection, migrations: Path
    ) -> None:
        """Not an OSError traceback: the CLI catches MigrationError."""
        (migrations / "001_first.sql").chmod(0o000)
        try:
            with pytest.raises(migrations_runner.MigrationError, match="001_first.sql"):
                migrations_runner.apply(conn=conn, migrations_dir=migrations)
        finally:
            (migrations / "001_first.sql").chmod(0o644)
