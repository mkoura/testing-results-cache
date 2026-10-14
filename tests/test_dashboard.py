"""Tests for the public dashboard data (dashboard.py) and its upload hook."""

import copy
import fcntl
import http
import json
import threading
import time
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import click
import flask
import flask.testing
import pytest
from click.testing import CliRunner
from click.testing import Result

from testing_results_cache import dashboard
from testing_results_cache import flask_db

NOW = datetime.now(UTC)

BASE: dict = {
    "schema": 1,
    "project": "cardano-node-tests",
    "testrun_name": "nightly",
    "run_id": "1",
    "step": "main",
    "origin": "ci",
    "timestamp": NOW.isoformat(),
    "duration": 10.0,
    "exit_code": 0,
    "filtered": False,
    "counts": {"total": 100, "passed": 90, "failed": 0, "broken": 0, "skipped": 10},
    "quality": {"never_run": 0, "no_history_id": 0, "read_errors": 0},
    "versions": {"cardano_node": "10.5.0", "cardano_cli": None, "db_sync": None},
    "commands": {"count": 1000, "coverage_pct": 30.0},
}

# Every key a document may carry. Anything else reaching the file is a leak.
TOP_KEYS = {
    "schema",
    "generated_at",
    "window_days",
    "all_time",
    "test_runs",
    "daily",
    "latest",
    "coverage",
    "releases",
}
TEST_RUN_KEYS = {"name", "source", "type", "release", "runs", "tests", "seconds", "first", "last"}
DAILY_KEYS = {"date", "name", "source", "runs", "tests", "seconds"}
LATEST_KEYS = {"started", "name", "source", "steps", "tests", "seconds", "node"}
RELEASE_KEYS = {"name", "version", "runs", "tests", "last"}
UPGRADE_STEPS = 3
OLD_DAYS = dashboard.WINDOW_DAYS + 5
WAITING_TESTS = 7
HOOK_TESTS = 33
SKIP_TOTAL = 100
SKIP_SKIPPED = 25


def _doc(**overrides: object) -> dict:
    """Return an upload document with the given top-level fields replaced."""
    out = copy.deepcopy(BASE)
    out.update(overrides)
    return out


def _counts(total: int) -> dict:
    """Return a count block whose buckets fit the total."""
    return {"total": total, "passed": total, "failed": 0, "broken": 0, "skipped": 0}


def _upload(client: flask.testing.FlaskClient, headers: dict, doc: dict) -> None:
    """Upload a document through the real route and require it to be stored."""
    response = client.put("/stats", data=json.dumps(doc), headers=headers)
    assert response.status_code == http.HTTPStatus.OK, response.data


def _ago(days: float) -> str:
    """Return an ISO-8601 timestamp the given number of days before NOW."""
    return (NOW - timedelta(days=days)).isoformat()


def _run(app: flask.Flask, command: click.Command) -> Result:
    """Invoke a click command inside the app context it expects."""
    with app.app_context():
        return CliRunner().invoke(command, [], obj=app)


def _build(app: flask.Flask) -> dict:
    """Build the dashboard document from the test database."""
    with app.app_context():
        return dashboard.build(flask_db.get_db(), now=NOW)


@pytest.fixture
def seeded(client: flask.testing.FlaskClient, auth_headers: dict) -> dict:
    """Store a known mix of runs and return the expected totals, computed here."""
    runs = [
        _doc(run_id="1", timestamp=_ago(2), counts=_counts(100), duration=10.0),
        _doc(run_id="2", timestamp=_ago(1), counts=_counts(120), duration=12.0),
        _doc(run_id="3", timestamp=_ago(OLD_DAYS), counts=_counts(80), duration=8.0),
        _doc(
            testrun_name="node-1050",
            run_id="50",
            timestamp=_ago(1),
            counts=_counts(200),
            duration=20.0,
            release=True,
            versions={"cardano_node": "10.5.0"},
        ),
        _doc(
            testrun_name="nightly", run_id="99", origin="local", counts=_counts(999), release=True
        ),
    ]
    for doc in runs:
        _upload(client, auth_headers, doc)
    # One upgrade run: three steps under one run id, node version changing.
    for i, (cases, node) in enumerate(((10, "10.4.0"), (20, "10.4.0"), (30, "10.5.0")), start=1):
        _upload(
            client,
            auth_headers,
            _doc(
                testrun_name="nightly-upgrade",
                run_id="7",
                step=f"step{i}",
                timestamp=_ago(1 - i / 100),
                counts=_counts(cases),
                duration=float(cases),
                versions={"cardano_node": node},
            ),
        )
    ci_tests = 100 + 120 + 80 + 200 + 10 + 20 + 30
    return {
        "ci_runs": 3 + 1 + 1,
        "ci_tests": ci_tests,
        "local_runs": 1,
        "local_tests": 999,
        "all_time_runs": 3 + 1 + 1 + 1,
        "all_time_tests": ci_tests + 999,
        "window_nightly_runs": 2,
        "window_nightly_tests": 220,
        "upgrade_tests": 60,
    }


class TestBuild:
    """Tests for the numbers and shape of the dashboard document."""

    def test_all_time_totals_count_ci_and_local_runs(self, app: flask.Flask, seeded: dict) -> None:
        """Count every run ever stored, split by source."""
        totals = _build(app)["all_time"]
        assert totals["runs"] == seeded["all_time_runs"]
        assert totals["tests"] == seeded["all_time_tests"]
        assert totals["by_source"]["ci"]["runs"] == seeded["ci_runs"]
        assert totals["by_source"]["ci"]["tests"] == seeded["ci_tests"]
        assert totals["by_source"]["local"]["runs"] == seeded["local_runs"]
        assert totals["by_source"]["local"]["tests"] == seeded["local_tests"]

    def test_a_local_run_is_kept_apart_from_ci(self, app: flask.Flask, seeded: dict) -> None:
        """Keep a local run under its own source, never as a release."""
        assert seeded
        doc = _build(app)
        local = [t for t in doc["test_runs"] if t["source"] == "local"]
        assert [(t["name"], t["runs"], t["release"]) for t in local] == [("nightly", 1, False)]
        assert {r["source"] for r in doc["latest"]} == {"ci", "local"}

    def test_an_unknown_origin_is_shown_as_local(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Never let an origin other than ci or local reach the page."""
        _upload(client, auth_headers, _doc(origin="staging-box-7"))
        doc = _build(app)
        assert "staging-box-7" not in json.dumps(doc)
        assert {t["source"] for t in doc["test_runs"]} == {"local"}

    def test_coverage_comes_from_ci_only(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Ignore the coverage of a local run, which is often a subset."""
        _upload(
            client, auth_headers, _doc(origin="local", commands={"count": 1, "coverage_pct": 90.0})
        )
        assert _build(app)["coverage"] == []

    def test_an_upgrade_counts_as_one_run(self, app: flask.Flask, seeded: dict) -> None:
        """Sum the three steps of an upgrade into one run."""
        doc = _build(app)
        upgrade = next(t for t in doc["test_runs"] if t["name"] == "nightly-upgrade")
        assert upgrade["type"] == "upgrade"
        assert upgrade["runs"] == 1
        assert upgrade["tests"] == seeded["upgrade_tests"]
        latest = next(r for r in doc["latest"] if r["name"] == "nightly-upgrade")
        assert latest["steps"] == UPGRADE_STEPS
        assert latest["tests"] == seeded["upgrade_tests"]

    def test_the_last_step_gives_the_node_version(self, app: flask.Flask, seeded: dict) -> None:
        """Report the version of the upgrade's final step."""
        assert seeded
        latest = next(r for r in _build(app)["latest"] if r["name"] == "nightly-upgrade")
        assert latest["node"] == "10.5.0"

    def test_daily_entries_cover_the_window_only(self, app: flask.Flask, seeded: dict) -> None:
        """Leave a run older than the window out of the daily totals."""
        doc = _build(app)
        nightly = [d for d in doc["daily"] if d["name"] == "nightly" and d["source"] == "ci"]
        assert sum(d["runs"] for d in nightly) == seeded["window_nightly_runs"]
        assert sum(d["tests"] for d in nightly) == seeded["window_nightly_tests"]

    def test_the_window_includes_recent_runs(self, app: flask.Flask, seeded: dict) -> None:
        """Include every recent CI run in the latest runs."""
        assert seeded
        names = {r["name"] for r in _build(app)["latest"]}
        assert names == {"nightly", "node-1050", "nightly-upgrade"}

    def test_a_release_is_listed_with_its_version(self, app: flask.Flask, seeded: dict) -> None:
        """List a run uploaded with release true under releases tested."""
        assert seeded
        doc = _build(app)
        assert doc["releases"] == [
            {
                "name": "node-1050",
                "version": "10.5.0",
                "runs": 1,
                "tests": 200,
                "last": doc["releases"][0]["last"],
            }
        ]
        flags = {(t["name"], t["source"]): t["release"] for t in doc["test_runs"]}
        assert flags == {
            ("nightly", "ci"): False,
            ("nightly", "local"): False,
            ("node-1050", "ci"): True,
            ("nightly-upgrade", "ci"): False,
        }

    def test_only_allowed_fields_appear(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Never copy anything from the stored document beyond the listed fields."""
        marker = "NOT-FOR-THE-PUBLIC"
        _upload(
            client,
            auth_headers,
            _doc(
                notes=marker,
                quality={"never_run": 0, "no_history_id": 0, "read_errors": 0, "x": marker},
            ),
        )
        doc = _build(app)
        assert marker not in json.dumps(doc)
        assert set(doc) == TOP_KEYS
        assert all(set(t) == TEST_RUN_KEYS for t in doc["test_runs"])
        assert all(set(d) == DAILY_KEYS for d in doc["daily"])
        assert all(set(r) == LATEST_KEYS for r in doc["latest"])
        assert all(set(r) == RELEASE_KEYS for r in doc["releases"])

    def test_an_unusable_version_is_dropped(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Drop a version that is not a short string rather than publish it."""
        _upload(client, auth_headers, _doc(versions={"cardano_node": "x" * 500}))
        assert _build(app)["latest"][0]["node"] is None

    def test_coverage_takes_the_highest_value_of_the_day(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Report the best coverage of each day, ignoring out-of-range values."""
        for run_id, pct in (("1", 30.0), ("2", 31.5), ("3", 250.0)):
            _upload(
                client,
                auth_headers,
                _doc(run_id=run_id, commands={"count": 1, "coverage_pct": pct}),
            )
        assert _build(app)["coverage"] == [{"date": NOW.date().isoformat(), "pct": 31.5}]

    def test_skipped_tests_are_not_counted(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Count only the tests that ran, in every total the page shows."""
        counts = {
            "total": SKIP_TOTAL,
            "passed": SKIP_TOTAL - SKIP_SKIPPED,
            "failed": 0,
            "broken": 0,
            "skipped": SKIP_SKIPPED,
        }
        _upload(client, auth_headers, _doc(counts=counts, release=True))
        doc = _build(app)
        ran = SKIP_TOTAL - SKIP_SKIPPED
        assert doc["all_time"]["tests"] == ran
        assert [d["tests"] for d in doc["daily"]] == [ran]
        assert [r["tests"] for r in doc["latest"]] == [ran]
        assert [r["tests"] for r in doc["releases"]] == [ran]

    def test_an_empty_database_builds_an_empty_document(self, app: flask.Flask) -> None:
        """Build a valid document before any run is stored."""
        doc = _build(app)
        zero = {"runs": 0, "tests": 0, "seconds": 0}
        assert doc["all_time"] == {**zero, "by_source": {"ci": zero, "local": zero}}
        assert doc["daily"] == doc["latest"] == doc["releases"] == []


class TestWrite:
    """Tests for writing and replacing the dashboard file."""

    def test_rebuild_writes_the_file(self, app: flask.Flask, seeded: dict, tmp_path: Path) -> None:
        """Write a document that matches a fresh build."""
        with app.app_context():
            dashboard.rebuild(flask_db.get_db(), tmp_path)
        written = json.loads((tmp_path / dashboard.DATA_FILE).read_text())
        assert written["all_time"]["tests"] == seeded["all_time_tests"]
        assert written == _build(app) | {"generated_at": written["generated_at"]}
        assert not (tmp_path / dashboard.TMP_FILE).exists()

    def test_the_file_is_always_whole_while_it_is_replaced(self, tmp_path: Path) -> None:
        """Never let a reader see a missing or half-written file."""
        dashboard.write(tmp_path, {"seq": 0})
        stop = threading.Event()
        bad: tp.List[str] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    json.loads((tmp_path / dashboard.DATA_FILE).read_text())
                except (OSError, ValueError) as exc:
                    bad.append(repr(exc))

        thread = threading.Thread(target=reader)
        thread.start()
        for i in range(300):
            dashboard.write(tmp_path, {"seq": i, "pad": "x" * (i * 97 % 5000)})
        stop.set()
        thread.join()
        assert bad == []

    def test_a_waiting_rebuild_reads_the_database_after_the_first(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: Path,
    ) -> None:
        """Make a rebuild that waited for the lock include rows stored meanwhile."""
        lock = (tmp_path / dashboard.LOCK_FILE).open("a")
        fcntl.flock(lock, fcntl.LOCK_EX)

        def second_rebuild() -> None:
            with app.app_context():
                dashboard.rebuild(flask_db.get_db(), tmp_path)

        thread = threading.Thread(target=second_rebuild)
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive(), "the rebuild did not wait for the lock"
        _upload(client, auth_headers, _doc(run_id="42", counts=_counts(WAITING_TESTS)))
        lock.close()
        thread.join(timeout=10)
        written = json.loads((tmp_path / dashboard.DATA_FILE).read_text())
        assert written["all_time"]["tests"] == WAITING_TESTS


class TestUploadHook:
    """Tests for the rebuild that follows a stats upload."""

    def test_an_upload_rebuilds_the_file(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: Path,
    ) -> None:
        """Rewrite the dashboard file when a folder is configured."""
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        _upload(client, auth_headers, _doc(counts=_counts(HOOK_TESTS)))
        written = json.loads((tmp_path / dashboard.DATA_FILE).read_text())
        assert written["all_time"]["tests"] == HOOK_TESTS

    def test_no_folder_means_no_file(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: Path,
    ) -> None:
        """Do nothing when no dashboard folder is configured."""
        assert app.config["DASHBOARD_FOLDER"] is None
        _upload(client, auth_headers, _doc())
        assert list(tmp_path.glob("**/" + dashboard.DATA_FILE)) == []

    def test_a_failed_rebuild_does_not_fail_the_upload(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep the upload stored and answered with 200 when the rebuild fails."""
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)

        def broken(*_args: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(dashboard, "rebuild", broken)
        _upload(client, auth_headers, _doc(run_id="5"))
        with app.app_context():
            count = flask_db.get_db().execute("SELECT COUNT(*) FROM testrun_stats").fetchone()[0]
        assert count == 1


class TestCommand:
    """Tests for the build-dashboard command."""

    def test_the_command_writes_the_page_and_the_data(
        self, app: flask.Flask, seeded: dict, tmp_path: Path
    ) -> None:
        """Copy the page files and write the data file."""
        assert seeded
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        result = _run(app, dashboard.build_dashboard_command)
        assert result.exit_code == 0, result.output
        for name in (*dashboard.ASSET_FILES, dashboard.DATA_FILE):
            assert (tmp_path / name).is_file()

    def test_the_command_needs_a_folder(self, app: flask.Flask) -> None:
        """Refuse to run when no dashboard folder is configured."""
        result = _run(app, dashboard.build_dashboard_command)
        assert result.exit_code != 0
        assert "DASHBOARD_FOLDER" in result.output
