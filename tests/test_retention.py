"""Tests for history retention.

This is the only code in the service that deletes, so the tests are about
what must survive rather than what goes.
"""

import datetime
import os
import sqlite3
import time
from pathlib import Path

import flask
import pytest

from testing_results_cache import flask_db
from testing_results_cache import history_cache
from testing_results_cache import retention

DAYS_KEPT = 30
BOTH_ENTRIES = 2
SECOND_STAT = 2


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
    return _age_file(path)


def _age_file(path: Path) -> Path:
    """Move a file's mtime outside the orphan grace period.

    `find_orphans` skips recent files on purpose, so a test that writes one and
    expects it reported has to age it first.
    """
    old = time.time() - retention.ORPHAN_GRACE_SECONDS - 60
    os.utime(path, (old, old))
    return path


def _format(when: datetime.datetime) -> str:
    return history_cache._format_timestamp(when)


def _cutoff_offset(seconds: int) -> datetime.datetime:
    """Return a moment `seconds` from the cutoff a DAYS_KEPT prune computes."""
    now = datetime.datetime.now(tz=datetime.UTC)
    return now - datetime.timedelta(days=DAYS_KEPT) + datetime.timedelta(seconds=seconds)


def _seed_at(
    conn: sqlite3.Connection,
    history_folder: Path,
    name: str,
    job_id: str,
    when: datetime.datetime,
) -> Path:
    """Write a history row at an exact instant, and its file."""
    conn.execute(
        "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?)",
        (name, job_id, 1, _format(when)),
    )
    conn.commit()
    path = history_folder / name / f"{job_id}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<testsuites/>")
    return _age_file(path)


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

            removed, stranded = retention.prune(
                conn=conn, history_folder=history_folder, days=DAYS_KEPT
            )

            assert stranded == []
            assert [(e.testrun_name, e.job_id) for e in removed] == [("nightly", "1")]
            assert not old.exists()
            assert recent.exists()
            kept = [r["job_id"] for r in conn.execute("SELECT job_id FROM history")]
            assert kept == ["2"]

    def test_the_row_and_the_file_go_together(self, app: flask.Flask, history_folder: Path) -> None:
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

            listed, stranded = retention.prune(
                conn=conn, history_folder=history_folder, days=DAYS_KEPT, dry_run=True
            )
            assert stranded == []

            assert [e.job_id for e in listed] == ["1"]
            assert old.exists()
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 1

    def test_nothing_to_do_is_not_an_error(self, app: flask.Flask, history_folder: Path) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=1)
            assert retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT) == (
                [],
                [],
            )

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

            removed, stranded = retention.prune(
                conn=conn, history_folder=history_folder, days=DAYS_KEPT
            )

            assert stranded == []
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

            def refuse(self: Path, missing_ok: bool = False) -> None:
                if self == stuck:
                    raise OSError(13, "Permission denied")
                real_unlink(self, missing_ok)

            monkeypatch.setattr(Path, "unlink", refuse)

            removed, stranded = retention.prune(
                conn=conn, history_folder=history_folder, days=DAYS_KEPT
            )

            assert len(removed) == BOTH_ENTRIES
            assert [e.path for e in stranded] == [stuck]
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0
            assert stuck.exists()

        monkeypatch.undo()
        with app.app_context():
            conn = flask_db.get_db()
            assert retention.find_orphans(conn=conn, history_folder=history_folder) == [stuck]


class TestTheCutoffItself:
    """The boundary, not a day either side of it.

    An off-by-one here deletes a day more or keeps a day longer than asked,
    and either way nothing complains.
    """

    def test_an_entry_just_inside_the_window_is_kept(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            kept = _seed_at(conn, history_folder, "nightly", "1", _cutoff_offset(seconds=+2))
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            assert kept.exists()

    def test_an_entry_just_outside_the_window_goes(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            gone = _seed_at(conn, history_folder, "nightly", "1", _cutoff_offset(seconds=-2))
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            assert not gone.exists()

    def test_the_window_is_exactly_days_wide(self, app: flask.Flask, history_folder: Path) -> None:
        """Pins the width, not just the direction.

        Seeded a day and a bit past the cutoff: a window one day too wide keeps
        this, a correct one removes it.
        """
        with app.app_context():
            conn = flask_db.get_db()
            gone = _seed(conn, history_folder, "nightly", "1", age_days=DAYS_KEPT + 1)
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            assert not gone.exists()


class TestTheRowGoesFirst:
    def test_an_interrupted_prune_leaves_a_file_with_no_row(
        self, app: flask.Flask, history_folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering the whole design rests on.

        A file with no row is a stray that `find_orphans` names. A row with no
        file is unreachable through the API and cannot be re-uploaded either,
        because the UNIQUE constraint still holds the name. So the row must be
        committed before the file is touched, never after.
        """
        with app.app_context():
            conn = flask_db.get_db()
            path = _seed(conn, history_folder, "nightly", "1", age_days=90)

            def die(*_args: object, **_kwargs: object) -> None:
                raise KeyboardInterrupt

            monkeypatch.setattr(Path, "unlink", die)
            with pytest.raises(KeyboardInterrupt):
                retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            monkeypatch.undo()

            assert path.exists()

        # A new connection, so this is the committed state a later run sees.
        with app.app_context():
            conn = flask_db.get_db()
            assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0

    def test_an_upload_during_the_prune_keeps_its_file(
        self, app: flask.Flask, history_folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window the commit opens.

        Committing the delete frees the testrun+job for reuse while the unlink
        loop is still running. An upload landing there writes a new row and a
        new file at the same path. Unlinking by path alone would delete that
        fresh file and leave its row behind, which is the state the test above
        exists to avoid, arrived at from the other side.

        The upload is injected at the `stat` that starts each unlink, which is
        the first thing `prune` does to a path after committing. It closes the
        window between the commit and that stat, not the far smaller one
        between the stat and the unlink itself.
        """
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=90)
            path = history_folder / "nightly" / "1.xml"
            real_stat = Path.stat
            seen: list = []
            done: list = []

            def upload_first(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                # Stand in for a concurrent upload: re-insert the row and
                # replace the file, the way upload_history does it.
                #
                # On the second stat of this path, not the first. The first is
                # `find_expired` recording the inode, which runs before the
                # delete is committed; an upload there is simply included in
                # the prune. The second is the unlink loop, after the commit,
                # which is the window this test is about.
                if self == path:
                    seen.append(True)
                if self == path and len(seen) == SECOND_STAT and not done:
                    done.append(True)
                    other = sqlite3.connect(app.config["DATABASE"])
                    history_cache.save_history_entry(
                        conn=other, testrun_name="nightly", job_id="1", user_id=1
                    )
                    replacement = path.with_suffix(".new")
                    replacement.write_bytes(b"<brand new report/>")
                    replacement.rename(path)
                    other.commit()
                    other.close()
                return real_stat(self, follow_symlinks=follow_symlinks)

            monkeypatch.setattr(Path, "stat", upload_first)
            retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)
            monkeypatch.undo()

            assert done, "the stand-in upload never ran, so nothing was tested"

        with app.app_context():
            conn = flask_db.get_db()
            rows = conn.execute("SELECT count(*) FROM history").fetchone()[0]
            assert rows == 1, "the concurrent upload's row should still be there"
            assert path.exists(), "the concurrent upload's file was deleted by the prune"
            assert path.read_bytes() == b"<brand new report/>"


class TestUnusableRows:
    def test_a_row_whose_name_escapes_the_folder_is_skipped(
        self, app: flask.Flask, history_folder: Path
    ) -> None:
        """The one path built from stored values instead of from a request.

        No route can write such a row, but the README tells operators to open
        this database with sqlite3, and this is the code that deletes.
        """
        with app.app_context():
            conn = flask_db.get_db()
            outside = history_folder.parent / "important.xml"
            outside.write_bytes(b"<not history/>")
            when = _format(datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=90))
            conn.execute(
                "INSERT INTO history(testrun_name, job_id, user_id, timestamp) VALUES (?,?,?,?)",
                ("..", "important", 1, when),
            )
            conn.commit()

            removed, _ = retention.prune(conn=conn, history_folder=history_folder, days=DAYS_KEPT)

            assert removed == []
            assert outside.exists()


class TestOrphans:
    def test_finds_a_file_with_no_row(self, app: flask.Flask, history_folder: Path) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            stray = history_folder / "nightly" / "999.xml"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b"<testsuites/>")
            _age_file(stray)

            assert retention.find_orphans(conn=conn, history_folder=history_folder) == [stray]

    def test_a_stored_entry_is_not_an_orphan(self, app: flask.Flask, history_folder: Path) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            _seed(conn, history_folder, "nightly", "1", age_days=1)
            assert retention.find_orphans(conn=conn, history_folder=history_folder) == []

    def test_no_history_folder_is_not_an_error(self, app: flask.Flask) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            missing = Path(app.config["HISTORY_FOLDER"]) / "does-not-exist"
            assert retention.find_orphans(conn=conn, history_folder=missing) == []
