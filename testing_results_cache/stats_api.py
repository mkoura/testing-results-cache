"""Endpoints for per-run test counts.

Deliberately separate from /import and /history. Martin's reason, from the
2026-09-22 1:1: JUnit is a standardised format and cannot carry the metadata
these counts need (software versions, CLI command coverage) without breaking
its schema. /import parses JUnit and stores verdicts; /history stores raw
JUnit and parses nothing; this endpoint stores numbers the client computed
and parses no test report at all.

The client does the counting because the source is allure, not JUnit.
`cardano-node-tests` runs pytest twice into one results dir - a `--skipall`
registration pass, then the real run - so result files have to be grouped by
allure `historyId` before anything is counted. That logic already exists in
`scripts/count_test_results.py` in that repo and is not duplicated here.

Mounted at `/stats/...`, its own top-level prefix, so a project name can
never collide with a route under `/results/`, `/history/` or
`/sync-results/`.

Auth is the service's existing HTTP basic auth, same as every other route.
The "token" is the password half of the credentials pair: create a user with
`flask --app testing_results_cache add-user stats <token>`, put the pair in
CI secrets, and export it locally. Nothing here needs a second auth path.
"""

import json
import math
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime

import flask
from werkzeug.exceptions import HTTPException

from testing_results_cache import common
from testing_results_cache import flask_auth
from testing_results_cache import flask_db
from testing_results_cache import stats_cache

# The only payload version this service understands. An unknown value is
# refused rather than guessed at: a client that changed shape and a client
# that is simply newer look identical here, and silently storing a document
# we cannot read back is worse than refusing it.
SUPPORTED_SCHEMA = 1

# A real payload is about 600 bytes. This bounds the JSON body well below the
# app-wide 16MB MAX_CONTENT_LENGTH, which exists for zip and XML uploads and
# is far too generous for a metrics document.
MAX_PAYLOAD_BYTES = 64 * 1000

# `step` defaults to this rather than to an empty string, so every identity
# field is a usable URL segment for the read routes.
DEFAULT_STEP = "main"

# Guards the day window on the read routes. A year of history is more than
# any dashboard panel asks for, and it keeps the scan bounded.
MAX_DAYS = 366

# SQLite stores integers as signed 64-bit. A larger value raises OverflowError
# inside the driver, which would surface as a 500 for what is a client error.
MAX_SQLITE_INT = 2**63 - 1

# Counts are capped far below MAX_SQLITE_INT, because they are SUMmed across
# rows by `get_totals` and a per-field bound does not stop the total
# overflowing. Two rows of 2**62 are each individually legal and make every
# `GET /stats` variant fail with `integer overflow` for good - there is no
# delete route, so recovery means manual SQL on the live database. Ten million
# tests in one run is about 4600x the largest real run measured (2145), so
# this cannot reject a genuine upload, and 2**63 / 10**7 is 9.2e11 rows.
MAX_COUNT = 10_000_000

# `duration` needs a bound for the same reason `cases` does, and it was
# missed when MAX_COUNT was added: `get_totals` sums this column too. A float
# sum does not raise on overflow the way an integer one does - it quietly
# becomes `inf`, and `flask.jsonify` then emits the bare token `Infinity`,
# which is not valid JSON. That is the exact failure the parse hooks in
# `_body` exist to prevent on the way in. A billion seconds is about 31
# years, against a real run of 4527 seconds, and it leaves room for 1.8e299
# rows before a sum could reach infinity.
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
    # Only DB errors land here. Without it an unrun migration would surface
    # as an unhandled exception and break the JSON-error contract the rest
    # of this service keeps - the same gap that was fixed on the
    # sync-results GET routes.
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
    # Read the body once and parse that same buffer. `get_data(cache=False)`
    # followed by `get_json()` would hand the parser an already-consumed
    # stream; `json.loads` here also avoids depending on the request's
    # content type being set correctly by the uploader.
    raw = flask.request.get_data(cache=True)
    if len(raw) > MAX_PAYLOAD_BYTES:
        common.abort_json(413, f"Payload larger than {MAX_PAYLOAD_BYTES} bytes")

    try:
        # Two hooks, because there are two ways to get a non-finite float in.
        # `parse_constant` fires for the `NaN`, `Infinity` and `-Infinity`
        # literals, which json.loads accepts by default and json.dumps emits
        # back. `parse_float` catches the other route: `1e400` is an ordinary
        # numeric literal that overflows to `inf` without the constant hook
        # ever running. Either one is not valid JSON, and either one in the
        # stored document breaks the promise that `payload` always holds
        # readable JSON - which is the whole reason that column exists.
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

    The read routes address a run by these values, so anything that would
    not survive a path segment is refused at write time rather than becoming
    a row nothing can reach.

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
    # `None` is treated as absent, not as a bad value: a client that
    # serialises an unset optional field as `null` means the same thing as
    # leaving it out, and `step` is documented as defaulting when absent.
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
    # `isinstance(True, int)` is True, so bools are excluded explicitly -
    # otherwise `"passed": true` would silently store 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        common.abort_json(400, f"Field {where!r} must be a non-negative integer")
    if value > maximum:
        common.abort_json(400, f"Field {where!r} is larger than {maximum}")
    return value


def _counts(payload: dict) -> dict:
    """Validate the count block and check it is internally consistent.

    The four status buckets must not exceed the total. They may sum to less:
    anything the client saw with another status is the remainder, exposed as
    `other` on the entry rather than stored in its own column.

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

    A registration-pass result carries status "skipped", so a `never_run`
    above `skipped` means the client counted something inconsistently and
    the row would misreport how complete the run was.

    Args:
        payload: The uploaded document.
        skipped: The already-validated `counts.skipped`.

    Returns:
        The number of tests that never produced a real result.

    Raises:
        HTTPException: 400 when `quality` is missing or not an object, or
            when the value exceeds `skipped`.
    """
    # Not `payload.get("quality") or {}`: that turns a falsy non-dict such as
    # `[]` into `{}` and the type error below is never reported, so the caller
    # gets a confusing "quality.never_run" message about a field they did not
    # send. `quality` is required rather than defaulted - defaulting
    # `never_run` to 0 would silently claim an interrupted run was complete,
    # which is the one thing this column exists to prevent.
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
    """Read the run's wall-clock duration in seconds.

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
    # `math.isfinite` as well as the sign test: `float("inf") < 0` is False, so
    # the check above passes it, and `float("nan")` compares False against
    # everything. Either one stored here poisons SUM(duration) for every
    # caller, and NaN reaches sqlite as NULL against a NOT NULL column.
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
    # MAX_SQLITE_INT, not MAX_COUNT: this column is stored, never summed.
    if not -MAX_SQLITE_INT <= value <= MAX_SQLITE_INT:
        common.abort_json(400, "Field 'exit_code' is out of range")
    return value


def _timestamp(payload: dict) -> datetime:
    """Parse the client's ISO-8601 timestamp into tz-aware UTC.

    A naive timestamp is treated as UTC rather than refused: the stored
    column is UTC by definition, and rejecting a run over a missing offset
    would lose real data for a formatting detail.

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
        # OverflowError, not just ValueError: fromisoformat accepts offsets up
        # to +/-24h, so a near-limit year converts out of datetime's range
        # here. Unhandled it escapes as Werkzeug's HTML 500 and breaks the
        # JSON-error contract every other response keeps.
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        common.abort_json(400, f"Field 'timestamp' is not a usable ISO-8601 value: {value!r}")

    # This is the first table whose timestamps come from the client rather
    # than from `datetime.now(UTC)`, so the storage format has to be checked
    # rather than assumed. `strftime("%Y")` does not zero-pad, and
    # `strptime("%Y")` demands four digits, so a year below 1000 writes a row
    # that `list_testrun_stats` can never read back - while `get_totals`,
    # which never parses the column, still counts it. The two routes would
    # then disagree forever with no way to reach the row through the API.
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
    # Refused rather than silently clamped: there is no cursor or offset on
    # this route, so a caller who asked for more than the cap and got exactly
    # the cap back has no way to tell that rows were dropped.
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
    # `str(...)`: werkzeug types `args.get` as returning Any, and returning it
    # straight fails mypy's no-any-return.
    project = str(value)
    common.reject_invalid_segments(project)
    return project


def _entry_dict(entry: common.TestrunStatsEntry) -> dict:
    """Render one run for the listing route.

    The stored document is deliberately left out, so a summary page built on
    this route cannot leak test names or failure text.

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


@stats.route("/stats", methods=["PUT", "POST"])
@flask_auth.auth.login_required
def upload_stats() -> dict:
    """Store the counts for one run, replacing any earlier upload for it.

    The whole identity comes from the body rather than the URL. Splitting a
    five-part identity between path segments and JSON would let the two
    disagree, and there is no sensible answer for which one wins.

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
        # Re-serialised from the parsed object, not stored byte for byte: the
        # values all survive, but key order, whitespace and any duplicate
        # keys do not. That is deliberate - what lands in the column is then
        # always valid, canonical JSON.
        #
        # `ensure_ascii=False` matters for the size, not for looks. The
        # default escapes every non-ASCII character to `\uXXXX`, which
        # measures 3.0x for 4-byte emoji and 2.0x for 3-byte CJK - so a body
        # inside MAX_PAYLOAD_BYTES could still store three times that. Left
        # as UTF-8, the stored form is never larger than the request that
        # carried it.
        payload=json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False),
    )

    conn = flask_db.get_db()
    user_id = flask_auth.auth.current_user()["user_id"]
    try:
        stats_cache.save_testrun_stats(conn=conn, entry=entry, user_id=user_id)
        conn.commit()
    except HTTPException:
        # Never swallow an intentional abort into the generic 500 below. The
        # try block holds only the save and the commit today, so nothing
        # aborts inside it - this keeps that true if anything moves in.
        raise
    except sqlite3.OperationalError as exc:
        _rollback(conn, run_id)
        # Masked to the low byte so a WAL busy (SQLITE_BUSY_SNAPSHOT) is not
        # missed by an equality test - same reasoning as sync_results_api.
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY:
            flask.current_app.logger.warning(f"Stats upload for run {run_id} hit database lock")
            common.abort_json(503, "Server busy, try again", headers={"Retry-After": "5"})
        _abort_storage_failure(run_id)
    except Exception:
        _rollback(conn, run_id)
        _abort_storage_failure(run_id)

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
    """Sum the counts across runs, optionally narrowed by project and day window.

    Returns:
        The summed counts, echoing back the filters that were applied.

    Raises:
        HTTPException: 400 when a query parameter is invalid, or 500 when the
            read fails.
    """
    project = _parse_project_arg()
    days = _parse_days()

    conn = flask_db.get_db()
    try:
        totals = stats_cache.get_totals(conn=conn, project=project, days=days)
    except sqlite3.Error:
        _abort_read_failure("totals")

    # MAX_DURATION makes this unreachable through this API, but a row written
    # by any other means could still push the sum to infinity, and emitting it
    # would return a body no strict JSON reader can parse. An integer overflow
    # already surfaces here as a JSON 500 from the sqlite3.Error above; this
    # gives the float column the same behaviour instead of silent corruption.
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

    conn = flask_db.get_db()
    try:
        entries = stats_cache.list_testrun_stats(conn=conn, project=project, days=days, limit=limit)
    except sqlite3.Error:
        _abort_read_failure("listing")

    return [_entry_dict(e) for e in entries]
