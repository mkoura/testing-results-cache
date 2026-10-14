"""Build the public dashboard data from the testrun_stats table.

The dashboard is a folder of static files that the web server serves to the
public. After each stats upload, `rebuild` writes `dashboard.json` into that
folder. The file is built from the fields listed here, so nothing else from
the database can reach it. Every run is marked `ci` or `local`.

"Tests" always means tests that ran: the total minus the skipped ones.
"""

import fcntl
import json
import math
import os
import shutil
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import click
import flask

from testing_results_cache import flask_db
from testing_results_cache import stats_cache

SCHEMA = 1
WINDOW_DAYS = 90
LATEST_RUNS = 50
CI = "ci"
LOCAL = "local"
MAIN_STEP = "main"
DATA_FILE = "dashboard.json"
TMP_FILE = ".dashboard.json.tmp"
LOCK_FILE = ".dashboard.lock"
MAX_VERSION_LENGTH = 64
MAX_PERCENT = 100
EXECUTED_SQL = "cases - skipped"
# Any origin other than "ci" is shown as "local", so no other value reaches the page.
SOURCE_SQL = "CASE WHEN origin = 'ci' THEN 'ci' ELSE 'local' END"
ASSETS_DIR = Path(__file__).parent / "dashboard_static"
ASSET_FILES = ("index.html", "dashboard.css", "dashboard.js")


def _timestamp(value: str) -> datetime:
    """Read a stored timestamp as tz-aware UTC.

    Args:
        value: A string in stats_cache.TIMESTAMP_FORMAT.

    Returns:
        The timestamp.
    """
    return datetime.strptime(value, stats_cache.TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _payload(raw: str) -> dict:
    """Parse a stored upload document.

    Args:
        raw: The `payload` column.

    Returns:
        The document, or an empty dict when it cannot be read.
    """
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _version(doc: dict) -> tp.Optional[str]:
    """Read the cardano-node version from an upload document.

    Args:
        doc: The parsed upload document.

    Returns:
        The version, or None when it is missing or not a short string.
    """
    versions = doc.get("versions")
    value = versions.get("cardano_node") if isinstance(versions, dict) else None
    if isinstance(value, str) and 0 < len(value) <= MAX_VERSION_LENGTH:
        return value
    return None


def _coverage(doc: dict) -> tp.Optional[float]:
    """Read the CLI coverage percentage from an upload document.

    Args:
        doc: The parsed upload document.

    Returns:
        The percentage, or None when it is missing or out of range.
    """
    commands = doc.get("commands")
    value = commands.get("coverage_pct") if isinstance(commands, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 <= value <= MAX_PERCENT:
        return None
    return round(float(value), 2)


def _all_time(conn: sqlite3.Connection) -> tp.Tuple[dict, tp.List[dict]]:
    """Sum every run ever stored, in total and per testrun name and source.

    Args:
        conn: An open database connection.

    Returns:
        Tuple of (totals, one entry per testrun name and source).
    """
    # One run is every step stored under one (project, testrun_name, run_id, origin).
    cur = conn.execute(
        f"SELECT testrun_name, {SOURCE_SQL} AS source, "
        "COUNT(DISTINCT project || '/' || run_id || '/' || origin), "
        f"COALESCE(SUM({EXECUTED_SQL}),0), COALESCE(SUM(duration),0), "
        "MIN(timestamp), MAX(timestamp), MAX(step != ?), "
        "MAX(origin = ? AND instr(payload, '\"release\":true') > 0) "
        "FROM testrun_stats GROUP BY testrun_name, source ORDER BY testrun_name, source",
        (MAIN_STEP, CI),
    )
    names = []
    for name, source, runs, tests, seconds, first, last, upgrade, release in cur.fetchall():
        names.append(
            {
                "name": name,
                "source": source,
                "type": "upgrade" if upgrade else "regression",
                "release": bool(release),
                "runs": runs,
                "tests": tests,
                "seconds": round(seconds, 1),
                "first": _timestamp(first).isoformat(),
                "last": _timestamp(last).isoformat(),
            }
        )

    def total(entries: tp.List[dict]) -> dict:
        return {
            "runs": sum(n["runs"] for n in entries),
            "tests": sum(n["tests"] for n in entries),
            "seconds": round(sum(n["seconds"] for n in entries), 1),
        }

    totals = total(names)
    totals["by_source"] = {
        src: total([n for n in names if n["source"] == src]) for src in (CI, LOCAL)
    }
    return totals, names


def _window_runs(conn: sqlite3.Connection, since: datetime) -> tp.List[dict]:
    """Group the rows since a time into runs, oldest first.

    Args:
        conn: An open database connection.
        since: The start of the window, tz-aware UTC.

    Returns:
        One entry per run, with its steps summed and the latest step's
        version and coverage.
    """
    cur = conn.execute(
        f"SELECT project, testrun_name, run_id, origin, {SOURCE_SQL}, step, timestamp, "
        f"{EXECUTED_SQL}, duration, payload FROM testrun_stats WHERE timestamp >= ? "
        "ORDER BY timestamp",
        (since.strftime(stats_cache.TIMESTAMP_FORMAT),),
    )
    runs: tp.Dict[tp.Tuple[str, str, str, str], dict] = {}
    for row in cur.fetchall():
        project, name, run_id, origin, source, step, stamp, executed, duration, raw = row
        doc = _payload(raw)
        key = (project, name, run_id, origin)
        run = runs.get(key)
        if run is None:
            run = runs[key] = {
                "started": _timestamp(stamp),
                "name": name,
                "source": source,
                "steps": 0,
                "tests": 0,
                "seconds": 0.0,
                "node": None,
                "coverage": None,
                "release": False,
            }
        run["steps"] += 1
        run["tests"] += executed
        run["seconds"] += duration
        run["release"] = run["release"] or (source == CI and doc.get("release") is True)
        # Rows come oldest first, so the last step's values win.
        run["node"] = _version(doc) or run["node"]
        if step == MAIN_STEP:
            run["coverage"] = _coverage(doc)
    return list(runs.values())


def _releases(conn: sqlite3.Connection) -> tp.List[dict]:
    """List every release tested, newest first.

    Args:
        conn: An open database connection.

    Returns:
        One entry per release testrun name.
    """
    # The stored document is canonical JSON, so the flag is always spelled this way.
    cur = conn.execute(
        f"SELECT testrun_name, run_id, timestamp, {EXECUTED_SQL}, payload FROM testrun_stats "
        "WHERE origin = ? AND instr(payload, '\"release\":true') > 0 ORDER BY timestamp",
        (CI,),
    )
    releases: tp.Dict[str, dict] = {}
    for name, run_id, stamp, executed, raw in cur.fetchall():
        entry = releases.setdefault(
            name, {"name": name, "version": None, "run_ids": set(), "tests": 0, "last": None}
        )
        entry["run_ids"].add(run_id)
        entry["tests"] += executed
        entry["last"] = _timestamp(stamp).isoformat()
        entry["version"] = _version(_payload(raw)) or entry["version"]
    out = [
        {
            "name": e["name"],
            "version": e["version"],
            "runs": len(e["run_ids"]),
            "tests": e["tests"],
            "last": e["last"],
        }
        for e in releases.values()
    ]
    return sorted(out, key=lambda e: e["last"], reverse=True)


def build(conn: sqlite3.Connection, now: tp.Optional[datetime] = None) -> dict:
    """Build the dashboard document from the database.

    Args:
        conn: An open database connection.
        now: The current time, tz-aware UTC. Defaults to the real clock.

    Returns:
        The document, holding only the fields the dashboard page draws.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=WINDOW_DAYS)
    totals, names = _all_time(conn)
    runs = _window_runs(conn, since)

    daily: tp.Dict[tp.Tuple[str, str, str], dict] = {}
    coverage: tp.Dict[str, float] = {}
    for run in runs:
        day = run["started"].date().isoformat()
        entry = daily.setdefault(
            (day, run["name"], run["source"]),
            {
                "date": day,
                "name": run["name"],
                "source": run["source"],
                "runs": 0,
                "tests": 0,
                "seconds": 0.0,
            },
        )
        entry["runs"] += 1
        entry["tests"] += run["tests"]
        entry["seconds"] += run["seconds"]
        # Coverage from CI only: a local run is often a small subset of the tests.
        if run["coverage"] is not None and run["source"] == CI and not run["release"]:
            coverage[day] = max(coverage.get(day, 0.0), run["coverage"])

    latest = [
        {
            "started": run["started"].isoformat(),
            "name": run["name"],
            "source": run["source"],
            "steps": run["steps"],
            "tests": run["tests"],
            "seconds": round(run["seconds"], 1),
            "node": run["node"],
        }
        for run in sorted(runs, key=lambda r: r["started"], reverse=True)[:LATEST_RUNS]
    ]

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "all_time": totals,
        "test_runs": names,
        "daily": [
            {**e, "seconds": round(e["seconds"], 1)}
            for e in sorted(daily.values(), key=lambda e: (e["date"], e["name"], e["source"]))
        ],
        "latest": latest,
        "coverage": [{"date": d, "pct": coverage[d]} for d in sorted(coverage)],
        "releases": _releases(conn),
    }


def write(folder: Path, data: dict) -> None:
    """Replace the dashboard file in one step.

    The new file is written next to the old one and renamed over it, so a
    reader gets either the whole old file or the whole new one.

    Args:
        folder: The dashboard folder.
        data: The document to write.
    """
    tmp = folder / TMP_FILE
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(folder / DATA_FILE)


def rebuild(conn: sqlite3.Connection, folder: Path) -> None:
    """Rebuild the dashboard file, one rebuild at a time.

    The lock makes a second rebuild wait and then read the database again,
    so the last file written always includes every stored run.

    Args:
        conn: An open database connection.
        folder: The dashboard folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK_FILE).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write(folder, build(conn))


def install_assets(folder: Path) -> None:
    """Copy the dashboard page files into the dashboard folder.

    Args:
        folder: The dashboard folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS_DIR / name, folder / name)


@click.command("build-dashboard")
def build_dashboard_command() -> None:
    """Copy the dashboard page into DASHBOARD_FOLDER and rebuild its data."""
    configured = flask.current_app.config.get("DASHBOARD_FOLDER")
    if not configured:
        msg = "DASHBOARD_FOLDER is not set in the instance config."
        raise click.ClickException(msg)
    folder = Path(configured)
    install_assets(folder)
    rebuild(flask_db.get_db(), folder)
    click.echo(f"Dashboard written to {folder}.")
