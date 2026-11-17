"""The public sync analyzer: run facts, comparison graphs and zip downloads.

The analyzer page lives in the cardano-node-tests docs site. It reads
`sync_analyzer.json` from the dashboard folder for the list of stored runs
and their numbers, shows graphs from `/sync-analyzer/graphs/...`, and links
to `/sync-analyzer/runs/<id>/zip`. All three are public: the routes take no
login, and the web server must forward `/sync-analyzer/*` to the service.

The graphs are drawn by `sync_static_graphs.py` from cardano-sync-tests, the
same script that draws the published reports. `SYNC_GRAPHS_COMMAND` names
how to run it. One pick of up to four runs is rendered on first request and
kept under `SYNC_GRAPHS_FOLDER`, so each pick costs the script once. The
folder is named by the pick and the time of its newest upload, so a new
upload of a run leads to a new folder and the old one ages out.
"""

import fcntl
import json
import math
import pathlib as pl
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import typing as tp
import zipfile
from datetime import UTC
from datetime import datetime

import click
import flask

from testing_results_cache import common
from testing_results_cache import flask_db
from testing_results_cache import sync_results_cache

SCHEMA = 1
INDEX_FILE = "sync_analyzer.json"
LOCK_FILE = ".sync_analyzer.lock"
RESULTS_MEMBER = "node_sync_results.json"
MAX_PICK = 4
# One render at a time: the script reads every results file of the pick,
# and a visitor can name any pick, so this bounds the work a burst of
# requests can start. Rendered picks are kept up to this many.
RENDER_LOCK = ".render.lock"
MAX_CACHED_PICKS = 200
STAMP_FORMAT = "%Y%m%dT%H%M%S%f"
# The file names the script writes, without the extension.
GRAPHS = frozenset(
    {
        "nodesync_progress",
        "nodesync_time_per_era",
        "nodesync_duration_per_epoch",
        "nodesync_ram_consumption",
        "nodesync_rss_consumption",
        "nodesync_cpu_consumption",
        "nodesync_cpu_consumption_smoothed",
    }
)
ERAS = ("byron", "shelley", "allegra", "mary", "alonzo", "babbage", "conway")
# An epoch is slow when it took this long and this many times its neighbours.
SLOW_EPOCH_SECONDS = 600
SLOW_EPOCH_RATIO = 2
NEIGHBOURS = 3
# RSS "at the end" is the median of this many final samples, not the last one.
END_SAMPLES = 200
CPU_PERCENTILE = 0.95
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_COMMIT = 8
# Nine digits per id: the pick comes from the address bar, and int() on a
# number of thousands of digits raises.
PICK_RE = re.compile(r"^[0-9]{1,9}(-[0-9]{1,9})*$")
FOLDER_RE = re.compile(r"^[0-9]+(-[0-9]+)*@[0-9]{8}T[0-9]{12}$")

analyzer = flask.Blueprint("sync_analyzer", __name__)


def results_file(run_id: int) -> pl.Path:
    """Return where the zip of a run is stored."""
    folder = pl.Path(flask.current_app.config["SYNC_RESULTS_FOLDER"])
    return folder / f"{run_id}.zip"


def label(version: str) -> str:
    """Return the version as the graphs name it: a commit hash is shortened."""
    return version[:SHORT_COMMIT] if COMMIT_RE.match(version) else version


def _cli_version(raw: tp.Optional[str]) -> tp.Optional[str]:
    """Return the version number from a `cardano-cli x.y.z - ...` string."""
    if not raw:
        return None
    return raw.split(" - ", 1)[0].removeprefix("cardano-cli ").strip()


def _slow_epochs(epochs: tp.Dict[int, float]) -> tp.List[dict]:
    """Return the epochs that took far longer than the epochs around them."""
    ordered = sorted(epochs)
    slow = []
    for pos, epoch in enumerate(ordered):
        around = ordered[max(0, pos - NEIGHBOURS) : pos] + ordered[pos + 1 : pos + 1 + NEIGHBOURS]
        if not around:
            continue
        typical = statistics.median(epochs[other] for other in around)
        seconds = epochs[epoch]
        if seconds > SLOW_EPOCH_SECONDS and seconds > SLOW_EPOCH_RATIO * typical:
            slow.append({"epoch": epoch, "seconds": seconds, "neighbours": typical})
    return slow


def _percentile(values: tp.List[float], fraction: float) -> tp.Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def facts_from_results(doc: dict) -> dict:
    """Return the numbers the analyzer shows for one node sync results document.

    Args:
        doc: The parsed `node_sync_results.json`.

    Returns:
        The facts, with None for every number the document does not have.
    """
    log_values = doc.get("log_values") or {}
    samples = [value for _, value in sorted(log_values.items()) if isinstance(value, dict)]
    rss = [float(value["rss_ram"]) for value in samples if value.get("rss_ram")]
    cpu = [float(value["cpu"]) for value in samples if value.get("cpu") is not None]
    epochs = {
        int(epoch): float(seconds)
        for epoch, seconds in (doc.get("sync_duration_per_epoch") or {}).items()
    }
    eras = {
        era: {
            "seconds": doc.get(f"{era}_sync_duration_secs"),
            "slots_per_second": doc.get(f"{era}_sync_speed_sps"),
        }
        for era in ERAS
        if doc.get(f"{era}_sync_duration_secs") is not None
    }
    return {
        "node": doc.get("tag_no1"),
        "cli": _cli_version(doc.get("cli_version1")),
        "started": doc.get("start_sync_time1"),
        "ended": doc.get("end_sync_time1"),
        "seconds": doc.get("sync_time_seconds1"),
        "last_slot": doc.get("last_slot_no1"),
        "last_epoch": max(epochs) if epochs else None,
        "epochs": len(epochs),
        "chain_bytes": doc.get("chain_size_bytes"),
        "cores": doc.get("no_of_cpu_cores"),
        "ram_gb": doc.get("total_ram_in_GB"),
        "kernel": doc.get("platform_release"),
        "eras": eras,
        "rss_peak_bytes": max(rss) if rss else None,
        "rss_end_bytes": statistics.median(rss[-END_SAMPLES:]) if rss else None,
        "cpu_median": statistics.median(cpu) if cpu else None,
        "cpu_p95": _percentile(cpu, CPU_PERCENTILE),
        "slow_epochs": _slow_epochs(epochs),
    }


def read_facts(zip_path: pl.Path) -> tp.Optional[dict]:
    """Return the facts of the node results file inside a stored zip.

    Args:
        zip_path: The stored zip.

    Returns:
        The facts, or None when the zip has no readable node results file.
    """
    with zipfile.ZipFile(zip_path) as zf:
        if RESULTS_MEMBER not in zf.namelist():
            flask.current_app.logger.info(
                f"{zip_path.name} holds no {RESULTS_MEMBER}; no facts for the analyzer"
            )
            return None
        try:
            doc = json.loads(zf.read(RESULTS_MEMBER))
        except ValueError:
            flask.current_app.logger.warning(
                f"{RESULTS_MEMBER} in {zip_path.name} is not valid JSON"
            )
            return None
    if not isinstance(doc, dict):
        flask.current_app.logger.warning(
            f"{RESULTS_MEMBER} in {zip_path.name} is not a JSON object"
        )
        return None
    return facts_from_results(doc)


def build_index(conn: sqlite3.Connection) -> dict:
    """Build the analyzer's run list from the database.

    Args:
        conn: An open database connection.

    Returns:
        The document the analyzer page reads: every stored run, newest
        start first, with its facts and the size of its zip.
    """
    runs = []
    for entry in sync_results_cache.list_sync_results(conn):
        path = results_file(entry.id)
        runs.append(
            {
                "id": entry.id,
                "environment": entry.environment,
                "version": entry.version,
                "label": label(entry.version),
                "started": entry.started.isoformat(),
                "uploaded": entry.timestamp.isoformat(),
                "zip_bytes": path.stat().st_size if path.is_file() else None,
                "facts": entry.facts,
            }
        )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "max_pick": MAX_PICK,
        "runs": runs,
    }


def rebuild_index(conn: sqlite3.Connection, folder: pl.Path) -> None:
    """Rebuild the index file, one rebuild at a time.

    Args:
        conn: An open database connection.
        folder: The dashboard folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK_FILE).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        common.replace_json(folder / INDEX_FILE, build_index(conn))


def rebuild_index_for_app(conn: sqlite3.Connection) -> bool:
    """Rebuild the index file using the app's configuration.

    Args:
        conn: An open database connection.

    Returns:
        False when no DASHBOARD_FOLDER is configured, so nothing was done.
    """
    configured = flask.current_app.config.get("DASHBOARD_FOLDER")
    if not configured:
        return False
    rebuild_index(conn, pl.Path(configured))
    return True


def _graphs_folder() -> pl.Path:
    return pl.Path(flask.current_app.config["SYNC_GRAPHS_FOLDER"])


def _prune(base: pl.Path) -> None:
    """Drop the oldest rendered picks beyond the cap."""
    folders = [f for f in base.iterdir() if f.is_dir() and FOLDER_RE.match(f.name)]
    folders.sort(key=lambda f: f.stat().st_mtime)
    for folder in folders[: max(0, len(folders) - MAX_CACHED_PICKS)]:
        shutil.rmtree(folder, ignore_errors=True)


def facts_for_upload(zip_path: pl.Path) -> tp.Optional[dict]:
    """Return the facts of a zip about to be stored, or None when they cannot be read.

    Args:
        zip_path: The validated upload, not yet renamed into place.
    """
    try:
        facts = read_facts(zip_path)
    except Exception:
        flask.current_app.logger.exception(f"Failed to read the facts of {zip_path.name}")
        return None
    if facts is not None and not _finite(facts):
        flask.current_app.logger.warning(f"{RESULTS_MEMBER} in {zip_path.name} holds inf or NaN")
        return None
    return facts


def _finite(value: object) -> bool:
    """Return whether a document holds only numbers JSON can carry."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


def after_upload(conn: sqlite3.Connection) -> None:
    """Rewrite the index after an upload.

    The upload is already stored and answered, so a failure here is logged
    and does not change the upload's outcome.

    Args:
        conn: The connection the upload was stored with.
    """
    try:
        rebuild_index_for_app(conn)
    except Exception:
        flask.current_app.logger.exception("Failed to rebuild the sync analyzer index")


def _pick(raw: str) -> tp.List[int]:
    """Return the run ids of a pick, or abort when the pick is not canonical.

    A pick is up to four ids in ascending order joined by `-`, so one set of
    runs has one name and one rendered folder.
    """
    if not PICK_RE.match(raw):
        common.abort_json(400, "Invalid pick: expected run ids joined by '-'")
    ids = [int(part) for part in raw.split("-")]
    if len(ids) > MAX_PICK:
        common.abort_json(400, f"Invalid pick: at most {MAX_PICK} runs")
    if ids != sorted(set(ids)):
        common.abort_json(400, "Invalid pick: run ids must be unique and ascending")
    return ids


def _entries(conn: sqlite3.Connection, ids: tp.List[int]) -> tp.List[common.SyncResultsEntry]:
    entries = []
    for run_id in ids:
        try:
            entry = sync_results_cache.get_sync_results(conn, run_id)
        except sqlite3.Error:
            flask.current_app.logger.exception(f"Failed to read sync run {run_id}")
            common.abort_json(500, "Failed to read sync results")
        if entry is None:
            common.abort_json(404, f"No sync results found for run {run_id}")
        entries.append(entry)
    if len({e.environment for e in entries}) > 1:
        # The era boundaries on the progress graph are those of one chain.
        common.abort_json(400, "Invalid pick: the runs must be on one network")
    # Drawn oldest run first, which puts the releases in order in every legend.
    return sorted(entries, key=lambda e: (e.started, e.id))


def _labels(entries: tp.List[common.SyncResultsEntry]) -> tp.List[str]:
    """Return one legend label per run.

    The script refuses two runs with one label, so a repeated version gets
    its start date, and a repeated date the run id as well.
    """
    names = [label(e.version) for e in entries]
    dated = [
        f"{name} ({entry.started.date().isoformat()})" if names.count(name) > 1 else name
        for name, entry in zip(names, entries, strict=True)
    ]
    return [
        f"{name[:-1]}, run {entry.id})" if dated.count(name) > 1 else name
        for name, entry in zip(dated, entries, strict=True)
    ]


def _extract_results(entry: common.SyncResultsEntry, target: pl.Path) -> None:
    try:
        with zipfile.ZipFile(results_file(entry.id)) as zf:
            if RESULTS_MEMBER not in zf.namelist():
                common.abort_json(404, f"Run {entry.id} has no {RESULTS_MEMBER} to draw")
            target.write_bytes(zf.read(RESULTS_MEMBER))
    except (OSError, zipfile.BadZipFile):
        # DB row without a readable zip: see the note in send_zip.
        flask.current_app.logger.exception(f"The zip of sync run {entry.id} cannot be read")
        common.abort_json(404, f"No sync results found for run {entry.id}")


def _run_script(entries: tp.List[common.SyncResultsEntry], workdir: pl.Path) -> pl.Path:
    """Draw the graphs of the runs into a new folder under `workdir`."""
    config = flask.current_app.config
    inputs = []
    for entry in entries:
        target = workdir / f"{entry.id}.json"
        _extract_results(entry, target)
        inputs.append(str(target))
    labels = _labels(entries)
    out = workdir / "out"
    command = [
        *config["SYNC_GRAPHS_COMMAND"],
        "--mode",
        "node",
        "-i",
        *inputs,
        "--labels",
        *labels,
        "-o",
        str(out),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=config["SYNC_GRAPHS_TIMEOUT"],
        )
    except subprocess.CalledProcessError as exc:
        flask.current_app.logger.exception(
            f"Graph script failed with code {exc.returncode}: {exc.stderr.decode(errors='replace')}"
        )
        common.abort_json(500, "Failed to draw the graphs")
    except (subprocess.TimeoutExpired, OSError):
        flask.current_app.logger.exception("Graph script did not run")
        common.abort_json(500, "Failed to draw the graphs")
    if not out.is_dir() or not any(out.glob("*.png")):
        # The script logs a refused input and exits 0 with no graph written.
        flask.current_app.logger.error(f"Graph script wrote nothing for {labels}")
        common.abort_json(500, "Failed to draw the graphs")
    return out


def render(conn: sqlite3.Connection, ids: tp.List[int]) -> pl.Path:
    """Return the folder holding the graphs of a pick, drawing them if needed.

    Args:
        conn: An open database connection.
        ids: The run ids of the pick, ascending.

    Returns:
        The folder with one PNG per graph.
    """
    entries = _entries(conn, ids)
    base = _graphs_folder()
    newest = max(e.timestamp for e in entries).strftime(STAMP_FORMAT)
    name = "-".join(str(run_id) for run_id in ids)
    folder = base / f"{name}@{newest}"
    if folder.is_dir():
        # The prune keeps the most recently used picks.
        folder.touch()
        return folder
    if not flask.current_app.config.get("SYNC_GRAPHS_COMMAND"):
        common.abort_json(503, "Graphs are not configured on this server")

    base.mkdir(parents=True, exist_ok=True)
    # One render at a time: a request while one is in progress is told to
    # come back, and the page asks for one graph of a pick at a time, so the
    # other graphs of that pick find the folder.
    with (base / RENDER_LOCK).open("a") as render_lock:
        try:
            fcntl.flock(render_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            common.abort_json(
                503,
                "Graphs are being drawn for another pick, try again shortly",
                headers={"Retry-After": "5"},
            )
        if folder.is_dir():
            return folder
        with tempfile.TemporaryDirectory(dir=base, prefix=f".{name}-") as tmp:
            out = _run_script(entries, pl.Path(tmp))
            out.replace(folder)
        _prune(base)
    return folder


@analyzer.route("/sync-analyzer/graphs/<pick>/<name>.png", methods=["GET"])
def get_graph(pick: str, name: str) -> flask.Response:
    """Send one graph of a pick of runs, drawing the pick on its first request."""
    ids = _pick(pick)
    if name not in GRAPHS:
        common.abort_json(404, "No such graph")
    folder = render(flask_db.get_db(), ids)
    path = folder / f"{name}.png"
    if not path.is_file():
        common.abort_json(404, "The graph script did not draw this graph")
    response = flask.send_file(path, mimetype="image/png")
    # The page adds the upload time to the URL, so a browser never shows a
    # copy drawn before a new upload.
    response.cache_control.public = True
    response.cache_control.max_age = 3600
    return response


def send_zip(run_id: int) -> flask.Response:
    """Send the stored zip of a run, or abort with 404 when there is none."""
    conn = flask_db.get_db()
    try:
        entry = sync_results_cache.get_sync_results(conn=conn, run_id=run_id)
    except sqlite3.Error:
        flask.current_app.logger.exception(f"Failed to read sync results ({run_id})")
        common.abort_json(500, "Failed to read sync results")
    if entry is None:
        common.abort_json(404, "No sync results found for this run")

    filepath = results_file(run_id)
    if not filepath.is_file():
        # DB says yes, disk says no - usually server-side data loss, not a
        # client error, so log it loudly; the 404 alone would hide the
        # distinction from the operator. There is one narrow, self-healing
        # exception: an overwrite in upload_sync_results moves the old file
        # to <name>.prev, then renames the new one into place - a request
        # landing in that microsecond gap sees a real but transient miss,
        # not data loss. Check for a matching .prev file before treating
        # this log line as an incident.
        flask.current_app.logger.error(
            f"Sync-results DB row exists for run {run_id} but file {filepath} is missing"
        )
        common.abort_json(404, "No sync results found for this run")

    response = flask.send_file(filepath, mimetype="application/zip")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="sync_results-{entry.environment}-{entry.version}.zip"'
    )
    return response


@analyzer.route("/sync-analyzer/runs/<int:run_id>/zip", methods=["GET"])
def get_run_zip(run_id: int) -> flask.Response:
    """Download the stored zip of a run, for visitors of the analyzer page."""
    return send_zip(run_id)


@click.command("build-sync-analyzer")
@click.option(
    "--refresh-facts",
    is_flag=True,
    help="Read the facts of every stored run from its zip again first.",
)
def build_sync_analyzer_command(refresh_facts: bool) -> None:
    """Rebuild the sync analyzer index in DASHBOARD_FOLDER."""
    configured = flask.current_app.config.get("DASHBOARD_FOLDER")
    if not configured:
        msg = "DASHBOARD_FOLDER is not set in the instance config."
        raise click.ClickException(msg)
    conn = flask_db.get_db()
    if refresh_facts:
        for entry in sync_results_cache.list_sync_results(conn):
            path = results_file(entry.id)
            if not path.is_file():
                click.echo(f"Run {entry.id} has no zip on disk; facts left as they are.")
                continue
            sync_results_cache.save_facts(conn, entry.id, facts_for_upload(path))
            conn.commit()
    rebuild_index(conn, pl.Path(configured))
    click.echo(f"Sync analyzer data written to {configured}.")
