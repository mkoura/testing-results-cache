"""Tests for the operator-facing commands.

`migrate` and `prune-history` are run by hand on a live database, or by cron.
Nothing else in this service is invoked that way, so the properties that matter
here are the ones an operator relies on: --dry-run really changes nothing, a
failure is a clean error rather than a traceback, and a command that could not
do its job does not exit zero.
"""

import datetime
import os
import sqlite3
import time
from pathlib import Path

import flask
import pytest
from click.testing import CliRunner

from testing_results_cache import flask_db
from testing_results_cache import history_cache
from testing_results_cache import migrations_runner
from testing_results_cache import retention

DAYS_KEPT = 30
# The two migrations this repo ships.
SHIPPED_MIGRATIONS = 2


def _run(app: flask.Flask, command: object, *args: str) -> object:
    """Invoke a click command inside the app context it expects."""
    with app.app_context():
        return CliRunner().invoke(command, list(args), obj=app)


def _seed_history(app: flask.Flask, name: str, job_id: str, age_days: int) -> Path:
    when = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=age_days)
    with app.app_context():
        conn = flask_db.get_db()
        conn.execute(
            "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?)",
            (name, job_id, 1, history_cache._format_timestamp(when)),
        )
        conn.commit()
    path = Path(app.config["HISTORY_FOLDER"]) / name / f"{job_id}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<testsuites/>")
    old = time.time() - retention.ORPHAN_GRACE_SECONDS - 60
    os.utime(path, (old, old))
    return path


def _tables(app: flask.Flask) -> set:
    conn = sqlite3.connect(app.config["DATABASE"])
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class TestMigrateCommand:
    def test_dry_run_reports_work_and_writes_nothing(self, app: flask.Flask) -> None:
        """The flag an operator reaches for before touching production."""
        with app.app_context():
            conn = flask_db.get_db()
            conn.executescript("DROP TABLE IF EXISTS schema_version")
            conn.commit()
        before = _tables(app)

        result = _run(app, flask_db.migrate_command, "--dry-run")

        assert result.exit_code == 0, result.output
        assert "Would apply 001_baseline.sql" in result.output
        assert _tables(app) == before
        assert "schema_version" not in _tables(app)

    def test_applies_and_is_then_up_to_date(self, app: flask.Flask) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            conn.executescript("DROP TABLE IF EXISTS schema_version")
            conn.commit()

        applied = _run(app, flask_db.migrate_command)
        assert applied.exit_code == 0, applied.output
        assert "Applied 001_baseline.sql" in applied.output

        again = _run(app, flask_db.migrate_command)
        assert again.exit_code == 0, again.output
        assert "Up to date at version 2." in again.output
        assert "Applied" not in again.output

    def test_a_database_ahead_of_the_code_is_a_clean_error(self, app: flask.Flask) -> None:
        """Not a traceback: this is the message the operator has to act on."""
        with app.app_context():
            conn = flask_db.get_db()
            migrations_runner.stamp_as_current(conn)
            conn.execute("INSERT INTO schema_version(version, name) VALUES (99, 'future')")
            conn.commit()

        result = _run(app, flask_db.migrate_command)

        assert result.exit_code != 0
        assert "Refusing to migrate" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestPruneHistoryCommand:
    def test_dry_run_lists_and_deletes_nothing(self, app: flask.Flask) -> None:
        old = _seed_history(app, "nightly", "1", age_days=90)
        fresh = _seed_history(app, "nightly", "2", age_days=1)

        result = _run(app, flask_db.prune_history_command, "--days", str(DAYS_KEPT), "--dry-run")

        assert result.exit_code == 0, result.output
        assert "Would remove nightly/1" in result.output
        assert old.exists()
        assert fresh.exists()

    def test_removes_the_aged_entries_and_says_how_many(self, app: flask.Flask) -> None:
        old = _seed_history(app, "nightly", "1", age_days=90)
        fresh = _seed_history(app, "nightly", "2", age_days=1)

        result = _run(app, flask_db.prune_history_command, "--days", str(DAYS_KEPT))

        assert result.exit_code == 0, result.output
        assert "Removed 1 entries older than 30 days." in result.output
        assert not old.exists()
        assert fresh.exists()

    def test_a_meaningless_window_is_refused(self, app: flask.Flask) -> None:
        kept = _seed_history(app, "nightly", "1", age_days=90)

        result = _run(app, flask_db.prune_history_command, "--days", "0")

        assert result.exit_code != 0
        assert "days must be at least 1" in result.output
        assert kept.exists()

    def test_a_file_that_will_not_delete_fails_the_run(
        self, app: flask.Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cron job that deleted no files must not report success.

        The rows are already committed by then, so a later run will not retry
        these. Exiting zero would hide that permanently.
        """
        stuck = _seed_history(app, "nightly", "1", age_days=90)
        real_unlink = Path.unlink

        def refuse(self: Path, missing_ok: bool = False) -> None:
            if self == stuck:
                raise OSError(13, "Permission denied")
            real_unlink(self, missing_ok)

        monkeypatch.setattr(Path, "unlink", refuse)
        result = _run(app, flask_db.prune_history_command, "--days", str(DAYS_KEPT))
        monkeypatch.undo()

        assert result.exit_code != 0
        assert "could not be removed" in result.output
        assert stuck.exists()

    def test_reports_a_file_with_no_row(self, app: flask.Flask) -> None:
        stray = Path(app.config["HISTORY_FOLDER"]) / "nightly" / "999.xml"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"<testsuites/>")
        old = time.time() - retention.ORPHAN_GRACE_SECONDS - 60
        os.utime(stray, (old, old))

        result = _run(app, flask_db.prune_history_command, "--days", str(DAYS_KEPT))

        assert result.exit_code == 0, result.output
        assert "999.xml" in result.output
        assert "have no matching row" in result.output


class TestInitDbAgreesWithTheMigrations:
    def test_a_fresh_install_has_nothing_pending(self, app: flask.Flask) -> None:
        """schema.sql already builds what the migrations would.

        Left unstamped, a brand-new database reports version 0 and `migrate`
        offers to re-apply the baseline. That is harmless only while every
        migration is written IF NOT EXISTS, which nothing enforces, so the
        first ALTER TABLE would fail on every fresh install.
        """
        result = _run(app, flask_db.init_db_command)
        assert result.exit_code == 0, result.output

        with app.app_context():
            conn = flask_db.get_db()
            assert migrations_runner.pending(conn=conn) == []
            assert migrations_runner.current_version(conn) == SHIPPED_MIGRATIONS

    def test_migrate_after_init_db_is_a_no_op(self, app: flask.Flask) -> None:
        assert _run(app, flask_db.init_db_command).exit_code == 0

        result = _run(app, flask_db.migrate_command)

        assert result.exit_code == 0, result.output
        assert "Up to date at version 2." in result.output
