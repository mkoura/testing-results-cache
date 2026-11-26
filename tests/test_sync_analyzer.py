"""Tests for the public sync analyzer (sync_analyzer.py)."""

import fcntl
import http
import io
import json
import pathlib as pl
import sys
import zipfile

import click
import flask
import flask.testing
import pytest
from click.testing import CliRunner
from click.testing import Result

from testing_results_cache import flask_db
from testing_results_cache import sync_analyzer

FAKE_SCRIPT = pl.Path(__file__).with_name("fake_graphs.py")
STARTED = "2026-10-28T12:38:51+00:00"
# Four samples; the last one has no RSS reading, so three RSS samples.
RESULTS = {
    "env": "mainnet",
    "tag_no1": "11.2.0",
    "cli_version1": "cardano-cli 11.6.0.0 - linux-x86_64 - ghc-9.6",
    "start_sync_time1": "2026-10-28T12:38:51+00:00",
    "end_sync_time1": "2026-10-30T01:22:26+00:00",
    "sync_time_seconds1": 132111,
    "last_slot_no1": 170000000,
    "chain_size_bytes": 239700000000,
    "no_of_cpu_cores": 32,
    "total_ram_in_GB": 134,
    "platform_release": "6.18.34",
    "babbage_sync_duration_secs": 61644,
    "babbage_sync_speed_sps": 995,
    "conway_sync_duration_secs": 20000,
    "conway_sync_speed_sps": 3412,
    "log_values": {
        "2026-10-28 12:39:36+00:00": {"tip": 60, "heap_ram": 1.0, "rss_ram": 1000.0, "cpu": 10.0},
        "2026-10-28 12:39:46+00:00": {"tip": 80, "heap_ram": 1.0, "rss_ram": 4000.0, "cpu": 160.0},
        "2026-10-28 12:39:56+00:00": {"tip": 90, "heap_ram": 1.0, "rss_ram": 3000.0, "cpu": 150.0},
        "2026-10-28 12:40:06+00:00": {"tip": 95, "heap_ram": 1.0, "rss_ram": 0.0, "cpu": 0.0},
    },
    # Epoch 493 takes 3,066 s against neighbours of about 300 s.
    "sync_duration_per_epoch": {
        "490": 300,
        "491": 310,
        "492": 320,
        "493": 3066,
        "494": 330,
        "495": 340,
        "496": 350,
    },
}
SLOW_EPOCH = 493
EPOCHS = 7
MEDIAN_CPU = 80.0
P95_CPU = 160.0
RSS_END = 3000.0
RSS_PEAK = 4000.0


def _zip(results: object = RESULTS, member: str = "node_sync_results.json") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(member, json.dumps(results) if not isinstance(results, bytes) else results)
    return buffer.getvalue()


def _upload(
    client: flask.testing.FlaskClient,
    headers: dict,
    version: str,
    content: bytes | None = None,
    environment: str = "mainnet",
    started: str = STARTED,
) -> int:
    """Upload a run through the real route and return its id."""
    resp = client.put(
        f"/sync-results/{version}",
        headers=headers,
        data={
            "syncresults": (io.BytesIO(content or _zip()), "sync_results.zip"),
            "environment": environment,
            "started": started,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == http.HTTPStatus.OK, resp.data
    run_id: int = resp.get_json()["id"]
    return run_id


def _run(app: flask.Flask, command: click.Command, *args: str) -> Result:
    """Invoke a click command inside the app context it expects."""
    with app.app_context():
        return CliRunner().invoke(command, list(args), obj=app)


@pytest.fixture
def graphs(app: flask.Flask, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch) -> pl.Path:
    """Point the app at the stand-in graph script and return its call log."""
    log = tmp_path / "renders.log"
    monkeypatch.setenv("FAKE_GRAPHS_LOG", str(log))
    app.config["SYNC_GRAPHS_COMMAND"] = [sys.executable, str(FAKE_SCRIPT)]
    app.config["SYNC_GRAPHS_FOLDER"] = str(tmp_path / "graphs")
    return log


def _renders(log: pl.Path) -> list:
    return log.read_text().splitlines() if log.is_file() else []


class TestFacts:
    def test_the_facts_of_a_results_file(self) -> None:
        facts = sync_analyzer.facts_from_results(RESULTS)
        assert facts == {
            "node": "11.2.0",
            "cli": "11.6.0.0",
            "started": "2026-10-28T12:38:51+00:00",
            "ended": "2026-10-30T01:22:26+00:00",
            "seconds": 132111,
            "last_slot": 170000000,
            "last_epoch": 496,
            "epochs": EPOCHS,
            "chain_bytes": 239700000000,
            "cores": 32,
            "ram_gb": 134,
            "kernel": "6.18.34",
            "eras": {
                "babbage": {"seconds": 61644, "slots_per_second": 995},
                "conway": {"seconds": 20000, "slots_per_second": 3412},
            },
            "rss_peak_bytes": RSS_PEAK,
            "rss_end_bytes": RSS_END,
            "cpu_median": MEDIAN_CPU,
            "cpu_p95": P95_CPU,
            "slow_epochs": [{"epoch": SLOW_EPOCH, "seconds": 3066.0, "neighbours": 325.0}],
        }


class TestIndex:
    """The run list the page reads, written after each upload."""

    def test_an_upload_writes_the_index_with_the_facts(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        run_id = _upload(client, auth_headers, "11.2.0")

        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert written["schema"] == sync_analyzer.SCHEMA
        assert written["max_pick"] == sync_analyzer.MAX_PICK
        [run] = written["runs"]
        assert run["id"] == run_id
        assert run["environment"] == "mainnet"
        assert run["version"] == "11.2.0"
        assert run["label"] == "11.2.0"
        assert run["started"] == STARTED
        assert run["zip_bytes"] == len(_zip())
        assert run["facts"]["seconds"] == RESULTS["sync_time_seconds1"]
        assert run["facts"]["slow_epochs"][0]["epoch"] == SLOW_EPOCH

    def test_a_zip_without_a_results_file_is_listed_without_facts(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        _upload(client, auth_headers, "13.6.0.5", content=_zip(member="db_sync_results.json"))

        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert written["runs"][0]["facts"] is None

    def test_a_results_file_with_a_number_json_cannot_carry_is_listed_without_facts(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        """Python reads 1e999 as infinity, which the index file cannot hold."""
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        _upload(
            client, auth_headers, "11.2.0", content=_zip(results=b'{"sync_time_seconds1": 1e999}')
        )
        _upload(client, auth_headers, "11.1.0")

        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert [(run["version"], run["facts"] is None) for run in written["runs"]] == [
            ("11.1.0", False),
            ("11.2.0", True),
        ]

    def test_a_results_file_that_is_not_json_is_listed_without_facts(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)
        _upload(client, auth_headers, "11.2.0", content=_zip(results=b"{not json"))

        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert written["runs"][0]["facts"] is None

    def test_no_folder_means_no_index(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skip the index cleanly when no dashboard folder is configured."""
        calls: list = []
        monkeypatch.setattr(sync_analyzer, "rebuild_index", lambda *args: calls.append(args))
        assert app.config["DASHBOARD_FOLDER"] is None
        _upload(client, auth_headers, "11.2.0")
        assert calls == []

    def test_the_command_writes_the_index(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        _upload(client, auth_headers, "11.2.0")
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)

        result = _run(app, sync_analyzer.build_sync_analyzer_command)
        assert result.exit_code == 0, result.output
        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert [run["version"] for run in written["runs"]] == ["11.2.0"]

    def test_the_command_can_read_the_facts_again(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        """A row carried over by the migration has no facts until this runs."""
        run_id = _upload(client, auth_headers, "11.2.0")
        with app.app_context():
            conn = flask_db.get_db()
            conn.execute("UPDATE sync_results SET facts = NULL WHERE id = ?", (run_id,))
            conn.commit()
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)

        result = _run(app, sync_analyzer.build_sync_analyzer_command, "--refresh-facts")
        assert result.exit_code == 0, result.output
        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert written["runs"][0]["facts"]["node"] == "11.2.0"

    def test_the_command_keeps_the_facts_of_a_zip_it_cannot_read(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        """A zip replaced by garbage on disk must not wipe the numbers already stored."""
        run_id = _upload(client, auth_headers, "11.2.0")
        pl.Path(app.config["SYNC_RESULTS_FOLDER"], f"{run_id}.zip").write_bytes(b"garbage")
        app.config["DASHBOARD_FOLDER"] = str(tmp_path)

        result = _run(app, sync_analyzer.build_sync_analyzer_command, "--refresh-facts")
        assert result.exit_code == 0, result.output
        assert f"Run {run_id}: no readable facts" in result.output
        written = json.loads((tmp_path / sync_analyzer.INDEX_FILE).read_text())
        assert written["runs"][0]["facts"]["node"] == "11.2.0"

    def test_the_command_needs_a_folder(self, app: flask.Flask) -> None:
        result = _run(app, sync_analyzer.build_sync_analyzer_command)
        assert result.exit_code != 0
        assert "DASHBOARD_FOLDER" in result.output


class TestZipDownload:
    def test_a_visitor_downloads_a_run_without_logging_in(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        archive = _zip()
        run_id = _upload(client, auth_headers, "11.2.0", content=archive)

        with client.get(f"/sync-analyzer/runs/{run_id}/zip") as resp:
            assert resp.status_code == http.HTTPStatus.OK
            assert resp.mimetype == "application/zip"
            assert resp.data == archive
            assert "sync_results-mainnet-11.2.0.zip" in resp.headers["Content-Disposition"]

    def test_an_unknown_run_is_404(self, client: flask.testing.FlaskClient) -> None:
        resp = client.get("/sync-analyzer/runs/42/zip")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert resp.get_json()["message"] == "No sync results found for this run"


class TestGraphs:
    def test_a_pick_is_drawn_once_and_served_from_then_on(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        older = _upload(client, auth_headers, "11.1.0", started="2026-08-19T18:11:28+00:00")
        newer = _upload(client, auth_headers, "11.2.0")

        first = client.get(f"/sync-analyzer/graphs/{older}-{newer}/nodesync_progress.png")
        assert first.status_code == http.HTTPStatus.OK, first.data
        assert first.mimetype == "image/png"
        assert first.data.endswith(b"11.1.0 11.2.0")

        second = client.get(f"/sync-analyzer/graphs/{older}-{newer}/nodesync_time_per_era.png")
        assert second.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["11.1.0 11.2.0"]

    def test_runs_are_drawn_oldest_first_whatever_their_ids(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        newer = _upload(client, auth_headers, "11.2.0")
        older = _upload(client, auth_headers, "11.1.0", started="2026-08-19T18:11:28+00:00")

        resp = client.get(f"/sync-analyzer/graphs/{newer}-{older}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["11.1.0 11.2.0"]

    def test_a_commit_run_is_labelled_by_its_short_hash(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        run_id = _upload(client, auth_headers, "c2ebdc87dfe07706a83e52f219e712c60d1b0a56")

        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["c2ebdc87"]

    def test_a_new_upload_of_a_run_draws_its_picks_again(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        """Graphs drawn from the old zip are never served after the run is uploaded again."""
        one = _upload(client, auth_headers, "11.1.0", started="2026-08-19T18:11:28+00:00")
        two = _upload(client, auth_headers, "11.2.0")
        for pick in (f"{one}", f"{one}-{two}"):
            resp = client.get(f"/sync-analyzer/graphs/{pick}/nodesync_progress.png")
            assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["11.1.0", "11.1.0 11.2.0"]

        _upload(client, auth_headers, "11.2.0")

        for pick in (f"{one}", f"{one}-{two}"):
            resp = client.get(f"/sync-analyzer/graphs/{pick}/nodesync_progress.png")
            assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["11.1.0", "11.1.0 11.2.0", "11.1.0 11.2.0"]

    def test_two_runs_of_one_version_on_one_day_get_their_run_ids_as_labels(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        """A rerun on the same day as the first run still needs two labels."""
        first = _upload(client, auth_headers, "11.2.0")
        second = _upload(client, auth_headers, "11.2.0", started="2026-10-28T20:00:00+00:00")

        resp = client.get(f"/sync-analyzer/graphs/{first}-{second}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == [
            f"11.2.0 (2026-10-28, run {first}) 11.2.0 (2026-10-28, run {second})"
        ]

    def test_a_script_that_writes_no_graph_is_a_json_500(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        """The real script makes its folder before it finds that an input has no data."""
        run_id = _upload(client, auth_headers, "empty")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert resp.get_json()["message"] == "Failed to draw the graphs"
        assert _renders(graphs) == []

    def test_two_runs_of_one_version_get_their_dates_as_labels(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        """The graph script refuses two runs with one label."""
        first = _upload(client, auth_headers, "11.2.0")
        second = _upload(client, auth_headers, "11.2.0", started="2026-11-03T08:00:00+00:00")

        resp = client.get(f"/sync-analyzer/graphs/{first}-{second}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.OK
        assert _renders(graphs) == ["11.2.0 (2026-10-28) 11.2.0 (2026-11-03)"]

    def test_runs_on_two_networks_cannot_share_a_graph(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        mainnet = _upload(client, auth_headers, "11.2.0")
        preview = _upload(client, auth_headers, "11.2.0", environment="preview")

        resp = client.get(f"/sync-analyzer/graphs/{mainnet}-{preview}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == "Invalid pick: the runs must be on one network"
        assert _renders(graphs) == []

    def test_a_script_that_writes_nothing_is_a_json_500(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        run_id = _upload(client, auth_headers, "silent")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert resp.get_json()["message"] == "Failed to draw the graphs"
        assert _renders(graphs) == []

    def test_a_render_in_progress_turns_another_pick_away(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        graphs: pl.Path,
    ) -> None:
        """A visitor who asks for a second pick while one draws gets a 503, not a queue."""
        one = _upload(client, auth_headers, "11.1.0", started="2026-08-19T18:11:28+00:00")
        two = _upload(client, auth_headers, "11.2.0")
        base = pl.Path(app.config["SYNC_GRAPHS_FOLDER"])
        base.mkdir(parents=True, exist_ok=True)
        with (base / sync_analyzer.RENDER_LOCK).open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            resp = client.get(f"/sync-analyzer/graphs/{one}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert resp.headers["Retry-After"] == "5"
        assert _renders(graphs) == []

        resp = client.get(f"/sync-analyzer/graphs/{two}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.OK

    def test_the_oldest_picks_are_dropped_beyond_the_cap(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        graphs: pl.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        one = _upload(client, auth_headers, "11.1.0", started="2026-08-19T18:11:28+00:00")
        two = _upload(client, auth_headers, "11.2.0")
        base = pl.Path(app.config["SYNC_GRAPHS_FOLDER"])
        monkeypatch.setattr(sync_analyzer, "MAX_CACHED_PICKS", 1)
        for pick in (f"{one}", f"{two}"):
            resp = client.get(f"/sync-analyzer/graphs/{pick}/nodesync_progress.png")
            assert resp.status_code == http.HTTPStatus.OK
        assert [f.name.split("@")[0] for f in base.iterdir() if f.is_dir()] == [f"{two}"]
        assert _renders(graphs) == ["11.1.0", "11.2.0"]

    @pytest.mark.parametrize(
        ("pick", "message"),
        [
            ("2-1", "Invalid pick: run ids must be unique and ascending"),
            ("1-2-3-4-5", "Invalid pick: at most 4 runs"),
            ("one", "Invalid pick: expected run ids joined by '-'"),
            ("1234567890", "Invalid pick: expected run ids joined by '-'"),
        ],
    )
    def test_a_pick_that_is_not_canonical_is_400(
        self, client: flask.testing.FlaskClient, pick: str, message: str
    ) -> None:
        resp = client.get(f"/sync-analyzer/graphs/{pick}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.BAD_REQUEST
        assert resp.get_json()["message"] == message

    def test_an_unknown_run_in_a_pick_is_404(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        run_id = _upload(client, auth_headers, "11.2.0")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}-99/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert resp.get_json()["message"] == "No sync results found for run 99"
        assert _renders(graphs) == []

    def test_an_unknown_graph_name_is_404(
        self, client: flask.testing.FlaskClient, auth_headers: dict, graphs: pl.Path
    ) -> None:
        run_id = _upload(client, auth_headers, "11.2.0")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_everything.png")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert _renders(graphs) == []

    @pytest.mark.usefixtures("graphs")
    def test_a_graph_the_script_did_not_draw_is_404(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        run_id = _upload(client, auth_headers, "11.2.0")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_rss_consumption.png")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert resp.get_json()["message"] == "The graph script did not draw this graph"

    def test_no_command_means_no_graphs(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert app.config["SYNC_GRAPHS_COMMAND"] is None
        run_id = _upload(client, auth_headers, "11.2.0")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert resp.get_json()["message"] == "Graphs are not configured on this server"

    @pytest.mark.usefixtures("graphs")
    def test_a_script_failure_is_a_json_500_and_leaves_no_folder(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        run_id = _upload(client, auth_headers, "fail")
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert resp.get_json()["message"] == "Failed to draw the graphs"
        assert [
            p.name
            for p in pl.Path(app.config["SYNC_GRAPHS_FOLDER"]).iterdir()
            if not p.name.startswith(".")
        ] == []

    def test_a_run_whose_zip_is_gone_cannot_be_drawn(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        graphs: pl.Path,
    ) -> None:
        """A row from before migration 004 has no `<id>.zip` until its zip is renamed."""
        run_id = _upload(client, auth_headers, "11.2.0")
        pl.Path(app.config["SYNC_RESULTS_FOLDER"], f"{run_id}.zip").unlink()
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert resp.get_json()["message"] == f"No sync results found for run {run_id}"
        assert _renders(graphs) == []

    @pytest.mark.usefixtures("graphs")
    def test_a_run_without_a_results_file_cannot_be_drawn(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        run_id = _upload(
            client, auth_headers, "13.6.0.5", content=_zip(member="db_sync_results.json")
        )
        resp = client.get(f"/sync-analyzer/graphs/{run_id}/nodesync_progress.png")
        assert resp.status_code == http.HTTPStatus.NOT_FOUND
        assert resp.get_json()["message"] == f"Run {run_id} has no node_sync_results.json to draw"
