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
import pathlib as pl
import shutil
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import click
import flask

from testing_results_cache import common
from testing_results_cache import flask_db
from testing_results_cache import impact as impact_mod
from testing_results_cache import stats_cache

SCHEMA = 1
WINDOW_DAYS = 90
LATEST_RUNS = 50
CI = "ci"
LOCAL = "local"
MAIN_STEP = "main"
DATA_FILE = "dashboard.json"
LOCK_FILE = ".dashboard.lock"
MAX_VERSION_LENGTH = 64
MAX_CLI_COUNT = 10**9
EXECUTED_SQL = "cases - skipped"
# Pre-filters rows in SQL; `_is_release` then checks the flag in the parsed document.
RELEASE_FLAG = '"release":true'
# Any origin other than "ci" is shown as "local", so no other value reaches the page.
SOURCE_SQL = "CASE WHEN origin = 'ci' THEN 'ci' ELSE 'local' END"
ASSETS_DIR = pl.Path(__file__).parent / "dashboard_static"
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


def _is_release(doc: dict, source: str) -> bool:
    """Tell whether an upload is a CI release run.

    Args:
        doc: The parsed upload document.
        source: `ci` or `local`.

    Returns:
        True when a CI run carries `"release": true` at the top level.
    """
    return source == CI and doc.get("release") is True


def _count(value: object) -> int:
    """Read a CLI count from an upload document.

    Args:
        value: The stored value.

    Returns:
        The count, or 0 when it is not a plausible non-negative integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= MAX_CLI_COUNT else 0


def _cli(doc: dict) -> tp.Tuple[int, int]:
    """Read the CLI invocation and submitted transaction counts from an upload.

    Args:
        doc: The parsed upload document.

    Returns:
        Tuple of (CLI invocations, transactions submitted), 0 when missing.
    """
    commands = doc.get("commands")
    if not isinstance(commands, dict):
        return 0, 0
    return _count(commands.get("count")), _count(commands.get("submits"))


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
        f"COALESCE(SUM({EXECUTED_SQL}),0), SUM(duration), "
        "MIN(timestamp), MAX(timestamp), MAX(step != ?) "
        "FROM testrun_stats GROUP BY testrun_name, source ORDER BY testrun_name, source",
        (MAIN_STEP,),
    )
    names = []
    for name, source, runs, tests, seconds, first, last, upgrade in cur.fetchall():
        names.append(
            {
                "name": name,
                "source": source,
                "type": "upgrade" if upgrade else "regression",
                "release": False,
                "runs": runs,
                "tests": tests,
                # SQLite 3.45 returns an overflowed float sum as NULL, newer versions as inf.
                "seconds": math.nan if seconds is None else round(seconds, 1),
                "first": _timestamp(first).isoformat(),
                "last": _timestamp(last).isoformat(),
            }
        )

    by_key = {(n["name"], n["source"]): n for n in names}
    for n in names:
        n["invocations"] = n["submits"] = 0
    cur = conn.execute(
        f"SELECT testrun_name, {SOURCE_SQL}, payload FROM testrun_stats "
        f"WHERE instr(payload, '\"commands\":{{') > 0 OR instr(payload, '{RELEASE_FLAG}') > 0"
    )
    for name, source, raw in cur.fetchall():
        entry = by_key.get((name, source))
        if entry is None:
            # Stored after the totals were read; the next rebuild counts it.
            continue
        doc = _payload(raw)
        invocations, submits = _cli(doc)
        entry["invocations"] += invocations
        entry["submits"] += submits
        entry["release"] = entry["release"] or _is_release(doc, source)

    def total(entries: tp.List[dict]) -> dict:
        """Sum the counts of the given entries.

        Args:
            entries: Entries from the per testrun list above.

        Returns:
            The summed runs, tests, seconds, invocations and submits.
        """
        return {
            "runs": sum(n["runs"] for n in entries),
            "tests": sum(n["tests"] for n in entries),
            "seconds": round(sum(n["seconds"] for n in entries), 1),
            "invocations": sum(n["invocations"] for n in entries),
            "submits": sum(n["submits"] for n in entries),
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
        version.
    """
    cur = conn.execute(
        f"SELECT project, testrun_name, run_id, origin, {SOURCE_SQL}, step, timestamp, "
        f"{EXECUTED_SQL}, duration, payload FROM testrun_stats WHERE timestamp >= ? "
        "ORDER BY timestamp",
        (since.strftime(stats_cache.TIMESTAMP_FORMAT),),
    )
    runs: tp.Dict[tp.Tuple[str, str, str, str], dict] = {}
    for row in cur.fetchall():
        project, name, run_id, origin, source, _step, stamp, executed, duration, raw = row
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
                "invocations": 0,
                "submits": 0,
            }
        run["steps"] += 1
        run["tests"] += executed
        run["seconds"] += duration
        # Rows come oldest first, so the last step's values win.
        run["node"] = _version(doc) or run["node"]
        invocations, submits = _cli(doc)
        run["invocations"] += invocations
        run["submits"] += submits
    return list(runs.values())


def _releases(conn: sqlite3.Connection) -> tp.List[dict]:
    """List every release tested, newest first.

    Args:
        conn: An open database connection.

    Returns:
        One entry per release testrun name.
    """
    cur = conn.execute(
        f"SELECT testrun_name, run_id, timestamp, {EXECUTED_SQL}, payload FROM testrun_stats "
        f"WHERE origin = ? AND instr(payload, '{RELEASE_FLAG}') > 0 ORDER BY timestamp",
        (CI,),
    )
    releases: tp.Dict[str, dict] = {}
    for name, run_id, stamp, executed, raw in cur.fetchall():
        doc = _payload(raw)
        if not _is_release(doc, CI):
            continue
        entry = releases.setdefault(
            name, {"name": name, "version": None, "run_ids": set(), "tests": 0, "last": None}
        )
        entry["run_ids"].add(run_id)
        entry["tests"] += executed
        entry["last"] = _timestamp(stamp).isoformat()
        entry["version"] = _version(doc) or entry["version"]
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


def build(
    conn: sqlite3.Connection,
    now: tp.Optional[datetime] = None,
    impact: tp.Optional[dict] = None,
) -> dict:
    """Build the dashboard document from the database.

    Args:
        conn: An open database connection.
        now: The current time, tz-aware UTC. Defaults to the real clock.
        impact: The impact section from `impact.load_section`, or None.

    Returns:
        The document, holding only the fields the dashboard page draws.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=WINDOW_DAYS)
    totals, names = _all_time(conn)
    runs = _window_runs(conn, since)

    daily: tp.Dict[tp.Tuple[str, str, str], dict] = {}
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
                "invocations": 0,
                "submits": 0,
            },
        )
        for key in ("runs", "tests", "seconds", "invocations", "submits"):
            entry[key] += 1 if key == "runs" else run[key]

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

    doc = {
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
        "releases": _releases(conn),
    }
    if impact is not None:
        doc["impact"] = impact
    return doc


def write(folder: pl.Path, data: dict) -> None:
    """Replace the dashboard file in one step.

    The new file is written next to the old one and renamed over it, so a
    reader gets either the whole old file or the whole new one.

    Args:
        folder: The dashboard folder.
        data: The document to write.
    """
    common.replace_json(folder / DATA_FILE, data)


def rebuild(conn: sqlite3.Connection, folder: pl.Path, impact: tp.Optional[dict] = None) -> None:
    """Rebuild the dashboard file, one rebuild at a time.

    The lock makes a second rebuild wait and then read the database again,
    so the last file written always includes every stored run.

    Args:
        conn: An open database connection.
        folder: The dashboard folder.
        impact: The impact section from `impact.load_section`, or None.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK_FILE).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write(folder, build(conn, impact=impact))


def rebuild_for_app(conn: sqlite3.Connection) -> bool:
    """Rebuild the dashboard file using the app's configuration.

    Args:
        conn: An open database connection.

    Returns:
        False when no DASHBOARD_FOLDER is configured, so nothing was done.
    """
    config = flask.current_app.config
    if not config.get("DASHBOARD_FOLDER"):
        return False
    impact = (
        impact_mod.load_section(pl.Path(config["IMPACT_FILE"]))
        if config.get("IMPACT_AUTHORS")
        else None
    )
    rebuild(conn, pl.Path(config["DASHBOARD_FOLDER"]), impact)
    return True


def install_assets(folder: pl.Path) -> None:
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
    install_assets(pl.Path(configured))
    rebuild_for_app(flask_db.get_db())
    click.echo(f"Dashboard written to {configured}.")


@click.command("update-impact")
def update_impact_command() -> None:
    """Fetch the issues found by our testing from GitHub, then rebuild the dashboard."""
    config = flask.current_app.config
    authors = config.get("IMPACT_AUTHORS") or []
    if not authors:
        msg = "IMPACT_AUTHORS is not set in the instance config."
        raise click.ClickException(msg)
    try:
        issues = impact_mod.fetch(
            authors=authors,
            owner=config["IMPACT_OWNER"],
            exclude=config["IMPACT_EXCLUDE_REPOS"],
            labelled=config["IMPACT_LABELLED"],
            token=config.get("IMPACT_GITHUB_TOKEN"),
        )
    except (OSError, ValueError) as err:
        # The previous impact file stays as it is.
        msg = f"Could not fetch issues from GitHub: {err}"
        raise click.ClickException(msg) from err
    impact_mod.save(pl.Path(config["IMPACT_FILE"]), config["IMPACT_OWNER"], issues)
    click.echo(f"Stored {len(issues)} issues.")
    if rebuild_for_app(flask_db.get_db()):
        click.echo("Dashboard rebuilt.")
