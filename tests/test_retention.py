"""Tests for history retention.

This is the only code in the service that deletes, so the tests are about
what must survive rather than what goes.
"""

import datetime
import sqlite3
from pathlib import Path

import flask
import pytest

from testing_results_cache import flask_db
from testing_results_cache import history_cache
from testing_results_cache import retention

DAYS_KEPT = 30
BOTH_ENTRIES = 2


def _seed(
    conn: sqlite3.Connection, history_folder: Path, name: str, job_id: str, age_days: int
) -> Path:
    """Write a history row and its file, aged by `age_days`."""
    when = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=age_days)
    conn.execute(
        "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?)",
        (name, job_id, 1, history_cache._format_timestamp(when)),
    )
    conn.commit()
    path = history_folder / name / f"{job_id}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<testsuites/>")
    return path


@pytest.fixture
def history_folder(app: flask.Flask) -> Path:
    folder = Path(app.config["HISTORY_FOLDER"])
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class TestPrune:
    def test_removes_old_entries_and_keeps_recent_ones(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            old = _seed(conn, history_folder, "nightly", "1", age_days=90)
            recent = _seed(conn, history_folder, "nightly", "2", age_days=1)

            removed = retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)

            assert [(e.testrun_name, e.job_id) for e in removed] == [("nightly", "1")]
            assert not old.exists()
            assert recent.exists()
            kept = [r["job_id"] for r in conn.execute("SELECT job_id FROM history")]
            assert kept == ["2"]

    def test_the_row_and_the_file_go_together(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        """Either state left alone is one the upload path works to avoid."""
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=90)

            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)

            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0
            assert retention.find_orphans(conn=conn, history_folder=history_folder) == []

    def test_dry_run_changes_nothing(self, app: flask.Flask, history_folder: Path) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            old = _seed(conn, history_folder, "nightly", "1", age_days=90)

            listed = retention.prune(
                conn=conn, history_folder=history_folder, days=DAYS_KEPT, dry_run=True
            )

            assert [e.job_id for e in listed] == ["1"]
            assert old.exists()
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 1

    def test_nothing_to_do_is_not_an_error(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=1)
            assert retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT) == []

    def test_an_entry_exactly_at_the_cutoff_is_kept(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        """Off-by-one here silently deletes a day more than asked."""
        with app.app_context():
            conn = flask_db.get_db()
            kept = _seed(conn, history_folder, "nightly", "1", age_days=29)
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            assert kept.exists()

    def test_only_the_named_testrun_files_are_touched(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=90)
            other = _seed(conn, history_folder, "nightly-cli", "1", age_days=1)
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            assert other.exists()

    def test_a_missing_file_does_not_stop_the_prune(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        """The row still has to go, or it points at nothing for good."""
        with app.app_context():
            conn = flask_db.get_db()
            path = _seed(conn, history_folder, "nightly", "1", age_days=90)
            path.unlink()

            removed = retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)

            assert len(removed) == 1
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0

    @pytest.mark.parametrize("days", [0, -1, -30])
    def test_refuses_a_meaningless_window(
        self, app: flask.Flask, history_folder: Path, days: int
    ) -> None:
        """`--days 0` would delete everything, which is never what was meant."""
        with app.app_context():
            conn = flask_db.get_db()
            kept = _seed(conn, history_folder, "nightly", "1", age_days=90)
            with pytest.raises(ValueError, match="days must be at least 1"):
                retention.prune(conn=conn, history_folder=history_folder, days=days)
            assert kept.exists()

    def test_a_file_that_will_not_unlink_becomes_a_reported_orphan(
        self, app: flask.Flask, history_folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row is committed first, so a failed unlink must not undo the run.

        Aborting here would lose both the removal report and the orphan report
        that follows it, leaving the operator with no record of either.
        """
        with app.app_context():
            conn = flask_db.get_db()
            stuck = _seed(conn, history_folder, "nightly", "1", age_days=90)
            _seed(conn, history_folder, "nightly", "2", age_days=90)

            real_unlink = Path.unlink

            def refuse(self: Path, *args: object, **kwargs: object) -> None:
                if self == stuck:
                    raise OSError(13, "Permission denied")
                real_unlink(self, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", refuse)

            removed = retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)

            assert len(removed) == BOTH_ENTRIES
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0
            assert stuck.exists()

        monkeypatch.undo()
        with app.app_context():
            conn = flask_db.get_db()
            assert retention.find_orphans(conn=conn, history_folder=history_folder) == [stuck]


class TestOrphans:
    def test_finds_a_file_with_no_row(self, app: flask.Flask, history_folder: Path) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            stray = history_folder / "nightly" / "999.xml"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b"<testsuites/>")

            assert retention.find_orphans(conn=conn, history_folder=history_folder) == [stray]

    def test_a_stored_entry_is_not_an_orphan(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=1)
            assert retention.find_orphans(conn=conn, history_folder=history_folder) == []

    def test_no_history_folder_is_not_an_error(self, app: flask.Flask) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            missing = Path(app.config["HISTORY_FOLDER"]) / "does-not-exist"
            assert retention.find_orphans(conn=conn, history_folder=missing) == []
