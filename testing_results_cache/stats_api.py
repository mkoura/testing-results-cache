"""Endpoints for per-run test counts.

The client computes the counts from its allure results and uploads them as
JSON. No test report is parsed here, and the metadata that JUnit cannot
carry (software versions, CLI coverage) is stored with the counts.
"""

import json
import math
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from pathlib import Path

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import dashboard
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import stats_cache

SUPPORTED_SCHEMA = 1

# Well below the app-wide MAX_CONTENT_LENGTH, which is sized for zip uploads.
MAX_PAYLOAD_BYTES = 64 * 1000

DEFAULT_STEP = "main"
VALID_ORIGINS = frozenset({"ci", "local"})
MAX_DAYS = 366

# SQLite stores integers as signed 64-bit.
MAX_SQLITE_INT = 2**63 - 1

# Counts and duration are summed across rows, so they are bounded well below
# the point where the sum overflows (integer) or becomes infinite (float).
MAX_COUNT = 10_000_000
MAX_DURATION = 10**9

_COUNT_FIELDS = ("total", "passed", "failed", "broken", "skipped")

stats = flask.Blueprint("stats", __name__)


def _rollback(conn: sqlite3.Connection, run_id: str) -> None:
    """Undo an uncommitted write, without masking the error that caused it.

    Args:
        conn: The connection whose transaction should be rolled back.
        run_id: The run being uploaded, for the log line.
    """
    try:
        conn.rollback()
    except sqlite3.Error:
        flask.current_app.logger.warning(
            f"Rollback failed for stats upload {run_id}", exc_info=True
        )


def _abort_storage_failure(run_id: str) -> tp.NoReturn:
    """Log a failed write and answer 500 in JSON.

    Args:
        run_id: The run being uploaded.

    Raises:
        HTTPException: Always, carrying a JSON 500 body.
    """
    flask.current_app.logger.exception(f"Failed to store testrun stats for run {run_id}")
    common.abort_json(500, "Failed to store testrun stats")


def _abort_read_failure(context: str) -> tp.NoReturn:
    """Log a failed read and answer 500 in JSON.

    Args:
        context: Which read failed, for the log line.

    Raises:
        HTTPException: Always, carrying a JSON 500 body.
    """
    flask.current_app.logger.exception(f"Failed to read testrun stats ({context})")
    common.abort_json(500, "Failed to read testrun stats")


def _reject_json_constant(name: str) -> tp.NoReturn:
    """Refuse NaN and Infinity, which json.loads accepts but JSON does not.

    Args:
        name: The literal the parser found.

    Raises:
        ValueError: Always. `_body` turns it into a 400.
    """
    err = f"{name} is not valid JSON"
    raise ValueError(err)


def _finite_float(raw: str) -> float:
    """Refuse a numeric literal that overflows to infinity, such as `1e400`.

    Args:
        raw: The numeric literal as it appeared in the document.

    Returns:
        The parsed value, when it is finite.

    Raises:
        ValueError: When the literal overflows. `_body` turns it into a 400.
    """
    value = float(raw)
    if not math.isfinite(value):
        err = f"{raw} is not a finite number"
        raise ValueError(err)
    return value


def _body() -> dict:
    """Return the request body as a JSON object, or refuse it.

    Returns:
        The parsed document, with a `schema` this service understands.

    Raises:
        HTTPException: 413 when the body is over MAX_PAYLOAD_BYTES, or 400
            when it is not a JSON object or carries an unknown `schema`.
    """
    raw = flask.request.get_data(cache=True)
    if len(raw) > MAX_PAYLOAD_BYTES:
        common.abort_json(413, f"Payload larger than {MAX_PAYLOAD_BYTES} bytes")

    try:
        # `parse_constant` catches `NaN` and `Infinity`, `parse_float` catches
        # a literal such as `1e400` that overflows to infinity.
        payload = json.loads(raw, parse_constant=_reject_json_constant, parse_float=_finite_float)
    except (ValueError, UnicodeDecodeError):
        common.abort_json(400, "Body is not valid JSON")
    if not isinstance(payload, dict):
        common.abort_json(400, "Body must be a JSON object")

    schema = payload.get("schema")
    if schema != SUPPORTED_SCHEMA:
        common.abort_json(400, f"Unsupported schema {schema!r}, expected {SUPPORTED_SCHEMA}")

    return payload


def _required_segment(payload: dict, name: str, default: tp.Optional[str] = None) -> str:
    """Read one identity field and check it is usable as a URL segment.

    Args:
        payload: The uploaded document.
        name: The field to read.
        default: Used when the field is absent or explicitly null.

    Returns:
        The field's value.

    Raises:
        HTTPException: 400 when the field is missing, not a string, or not a
            usable path segment.
    """
    value = payload.get(name)
    if value is None:
        value = default
    if value is None or not isinstance(value, str) or not value:
        common.abort_json(400, f"Missing or non-string field {name!r}")
    common.reject_invalid_segments(value)
    return value


def _non_negative_int(
    payload: dict, name: str, container: str = "", maximum: int = MAX_COUNT
) -> int:
    """Read one count and check it is a bounded, non-negative integer.

    Args:
        payload: The object holding the field.
        name: The field to read.
        container: The parent field's name, for the error message.
        maximum: The largest value accepted.

    Returns:
        The field's value.

    Raises:
        HTTPException: 400 when the value is not an integer, is negative, or
            exceeds `maximum`.
    """
    value = payload.get(name)
    where = f"{container}.{name}" if container else name
    # Exclude bools: True is an int and would store 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        common.abort_json(400, f"Field {where!r} must be a non-negative integer")
    if value > maximum:
        common.abort_json(400, f"Field {where!r} is larger than {maximum}")
    return value


def _counts(payload: dict) -> dict:
    """Validate the count block. The four status buckets may not exceed the total.

    Args:
        payload: The uploaded document.

    Returns:
        The five counts, keyed by name.

    Raises:
        HTTPException: 400 when the block is missing, a count is invalid, or
            the buckets exceed the total.
    """
    block = payload.get("counts")
    if not isinstance(block, dict):
        common.abort_json(400, "Missing or non-object field 'counts'")

    values = {name: _non_negative_int(block, name, "counts") for name in _COUNT_FIELDS}
    bucketed = sum(v for k, v in values.items() if k != "total")
    if bucketed > values["total"]:
        common.abort_json(
            400,
            f"counts: passed+failed+broken+skipped ({bucketed}) exceeds total ({values['total']})",
        )
    return values


def _never_run(payload: dict, skipped: int) -> int:
    """Validate `never_run`, which is a subset of `skipped`.

    Args:
        payload: The uploaded document.
        skipped: The already-validated `counts.skipped`.

    Returns:
        The number of tests that never produced a real result.

    Raises:
        HTTPException: 400 when `quality` is missing or not an object, or
            when the value exceeds `skipped`.
    """
    quality = payload.get("quality")
    if not isinstance(quality, dict):
        common.abort_json(400, "Missing or non-object field 'quality'")
    value = _non_negative_int(quality, "never_run", "quality")
    if value > skipped:
        common.abort_json(
            400, f"quality.never_run ({value}) cannot exceed counts.skipped ({skipped})"
        )
    return value


def _duration(payload: dict) -> float:
    """Read the run's wall clock duration in seconds.

    Args:
        payload: The uploaded document.

    Returns:
        The duration, guaranteed finite and non-negative.

    Raises:
        HTTPException: 400 when the value is not a finite, non-negative
            number, or is above MAX_DURATION.
    """
    value = payload.get("duration")
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        common.abort_json(400, "Field 'duration' must be a non-negative number")
    if not math.isfinite(value):
        common.abort_json(400, "Field 'duration' must be a finite number")
    if value > MAX_DURATION:
        common.abort_json(400, f"Field 'duration' is larger than {MAX_DURATION}")
    return float(value)


def _exit_code(payload: dict) -> int:
    """Read pytest's own exit code for the run.

    Args:
        payload: The uploaded document.

    Returns:
        The exit code.

    Raises:
        HTTPException: 400 when it is not an integer, or is out of range.
    """
    value = payload.get("exit_code")
    if isinstance(value, bool) or not isinstance(value, int):
        common.abort_json(400, "Field 'exit_code' must be an integer")
    if not -MAX_SQLITE_INT <= value <= MAX_SQLITE_INT:
        common.abort_json(400, "Field 'exit_code' is out of range")
    return value


def _timestamp(payload: dict) -> datetime:
    """Parse the client's ISO-8601 timestamp into tz-aware UTC. A naive value is UTC.

    Args:
        payload: The uploaded document.

    Returns:
        The timestamp as tz-aware UTC, known to survive storage.

    Raises:
        HTTPException: 400 when the value is not an ISO-8601 string, cannot
            be converted to UTC, or could not be read back once stored.
    """
    value = payload.get("timestamp")
    if not isinstance(value, str):
        common.abort_json(400, "Field 'timestamp' must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # Offsets up to +/-24h are accepted, so a year at either end of the
        # range overflows here.
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        common.abort_json(400, f"Field 'timestamp' is not a usable ISO-8601 value: {value!r}")

    if not stats_cache.timestamp_round_trips(parsed):
        common.abort_json(400, f"Field 'timestamp' is outside the storable range: {value!r}")
    return parsed


def _filtered(payload: dict) -> bool:
    """Read whether the run covered only a subset of the tests.

    Args:
        payload: The uploaded document.

    Returns:
        True when the run was restricted, defaulting to False.

    Raises:
        HTTPException: 400 when the value is not a boolean.
    """
    value = payload.get("filtered", False)
    if not isinstance(value, bool):
        common.abort_json(400, "Field 'filtered' must be a boolean")
    return value


def _parse_days() -> tp.Optional[int]:
    """Read the optional `days` query parameter.

    Returns:
        The window in days, or None when the caller did not ask for one.

    Raises:
        HTTPException: 400 when it is not an integer between 1 and MAX_DAYS.
    """
    raw = flask.request.args.get("days")
    if raw is None:
        return None
    try:
        days = int(raw)
    except ValueError:
        common.abort_json(400, f"Query parameter 'days' must be an integer, got {raw!r}")
    if not 1 <= days <= MAX_DAYS:
        common.abort_json(400, f"Query parameter 'days' must be between 1 and {MAX_DAYS}")
    return days


def _parse_limit() -> int:
    """Read the optional `limit` query parameter.

    Returns:
        The maximum rows to return, defaulting to the cap.

    Raises:
        HTTPException: 400 when it is not an integer within the cap.
    """
    raw = flask.request.args.get("limit")
    if raw is None:
        return stats_cache.MAX_LIST_ROWS
    try:
        limit = int(raw)
    except ValueError:
        common.abort_json(400, f"Query parameter 'limit' must be an integer, got {raw!r}")
    # Refused, not clamped: there is no cursor to fetch the rest.
    if not 1 <= limit <= stats_cache.MAX_LIST_ROWS:
        common.abort_json(
            400, f"Query parameter 'limit' must be between 1 and {stats_cache.MAX_LIST_ROWS}"
        )
    return limit


def _parse_project_arg() -> tp.Optional[str]:
    """Read the optional `project` query parameter.

    Returns:
        The project to filter on, or None for every project.

    Raises:
        HTTPException: 400 when the value is not a usable path segment.
    """
    value = flask.request.args.get("project")
    if value is None:
        return None
    project = str(value)
    common.reject_invalid_segments(project)
    return project


def _parse_origin_arg() -> tp.Optional[str]:
    """Read the optional `origin` query parameter.

    Returns:
        The origin to filter on, or None for every origin.

    Raises:
        HTTPException: 400 when the value is not `ci` or `local`.
    """
    value = flask.request.args.get("origin")
    if value is None:
        return None
    if value not in VALID_ORIGINS:
        allowed = ", ".join(sorted(VALID_ORIGINS))
        common.abort_json(400, f"Query parameter 'origin' must be one of: {allowed}")
    return str(value)


def _entry_dict(entry: common.TestrunStatsEntry) -> dict:
    """Render one run for the listing route, without the stored document.

    Args:
        entry: The run as read back from the database.

    Returns:
        The run's summary fields, including the derived `other`.
    """
    return {
        "project": entry.project,
        "testrun_name": entry.testrun_name,
        "run_id": entry.run_id,
        "step": entry.step,
        "origin": entry.origin,
        "timestamp": entry.timestamp.isoformat(),
        "cases": entry.cases,
        "passed": entry.passed,
        "failed": entry.failed,
        "broken": entry.broken,
        "skipped": entry.skipped,
        "other": entry.other,
        "never_run": entry.never_run,
        "duration": entry.duration,
        "exit_code": entry.exit_code,
        "filtered": entry.filtered,
    }


def _rebuild_dashboard(conn: sqlite3.Connection) -> None:
    """Rebuild the public dashboard file, when a dashboard folder is configured.

    A failure is logged and never fails the upload, which is already stored.

    Args:
        conn: The connection the upload was stored with.
    """
    folder = flask.current_app.config.get("DASHBOARD_FOLDER")
    if not folder:
        return
    try:
        dashboard.rebuild(conn, Path(folder))
    except Exception:
        flask.current_app.logger.exception("Failed to rebuild the dashboard")


@stats.route("/stats", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_stats() -> dict:
    """Store the counts for one run, replacing any earlier upload for it.

    Returns:
        The five identity fields of the run that was stored.

    Raises:
        HTTPException: 400 or 413 when the document is refused, 503 when the
            database is busy, or 500 when the write fails.
    """
    payload = _body()

    project = _required_segment(payload, "project")
    testrun_name = _required_segment(payload, "testrun_name")
    run_id = _required_segment(payload, "run_id")
    step = _required_segment(payload, "step", DEFAULT_STEP)
    origin = _required_segment(payload, "origin")

    counts = _counts(payload)
    entry = common.TestrunStatsEntry(
        project=project,
        testrun_name=testrun_name,
        run_id=run_id,
        step=step,
        origin=origin,
        timestamp=_timestamp(payload),
        cases=counts["total"],
        passed=counts["passed"],
        failed=counts["failed"],
        broken=counts["broken"],
        skipped=counts["skipped"],
        never_run=_never_run(payload, counts["skipped"]),
        duration=_duration(payload),
        exit_code=_exit_code(payload),
        filtered=_filtered(payload),
        # `ensure_ascii=False` keeps the stored form no larger than the request.
        payload=json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False),
    )

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]
    try:
        stats_cache.save_testrun_stats(conn=conn, entry=entry, user_id=user_id)
        conn.commit()
    except HTTPException:
        # Never swallow an intentional abort into the generic 500 below.
        raise
    except sqlite3.OperationalError as exc:
        _rollback(conn, run_id)
        # The low byte also matches extended codes such as SQLITE_BUSY_SNAPSHOT.
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY:
            flask.current_app.logger.warning(f"Stats upload for run {run_id} hit database lock")
            common.abort_json(503, "Server busy, try again", headers={"Retry-After": "5"})
        _abort_storage_failure(run_id)
    except Exception:
        _rollback(conn, run_id)
        _abort_storage_failure(run_id)

    _rebuild_dashboard(conn)

    return {
        "project": project,
        "testrun_name": testrun_name,
        "run_id": run_id,
        "step": step,
        "origin": origin,
    }


@stats.route("/stats", methods=["GET"])
@flask_auth.auth.login_required
def get_totals() -> dict:
    """Sum the counts across runs, optionally filtered by project, origin and day window.

    Returns:
        The summed counts, echoing back the filters that were applied.

    Raises:
        HTTPException: 400 when a query parameter is invalid, or 500 when the
            read fails.
    """
    project = _parse_project_arg()
    days = _parse_days()
    origin = _parse_origin_arg()

    conn = flask_db.get_db()
    try:
        totals = stats_cache.get_totals(conn=conn, project=project, days=days, origin=origin)
    except sqlite3.Error:
        _abort_read_failure("totals")

    # An infinite float would be emitted as `Infinity`, which is not JSON.
    if not math.isfinite(totals.duration):
        flask.current_app.logger.error(
            "SUM(duration) is not finite - a stored row is out of range "
            f"(project={project!r}, days={days!r})"
        )
        common.abort_json(500, "Failed to read testrun stats")

    return {
        "runs": totals.runs,
        "cases": totals.cases,
        "passed": totals.passed,
        "failed": totals.failed,
        "broken": totals.broken,
        "skipped": totals.skipped,
        "other": totals.other,
        "never_run": totals.never_run,
        "duration": totals.duration,
        "project": project,
        "days": days,
        "origin": origin,
    }


@stats.route("/stats/runs", methods=["GET"])
@flask_auth.auth.login_required
def list_runs() -> tp.List[dict]:
    """List individual runs, newest first.

    Returns:
        One summary object per run, oldest dropped once `limit` is reached.

    Raises:
        HTTPException: 400 when a query parameter is invalid, or 500 when the
            read fails.
    """
    project = _parse_project_arg()
    days = _parse_days()
    limit = _parse_limit()
    origin = _parse_origin_arg()

    conn = flask_db.get_db()
    try:
        entries = stats_cache.list_testrun_stats(
            conn=conn, project=project, days=days, limit=limit, origin=origin
        )
    except sqlite3.Error:
        _abort_read_failure("listing")

    return [_entry_dict(e) for e in entries]
