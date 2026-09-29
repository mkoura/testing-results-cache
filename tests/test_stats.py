"""Tests for the per-run statistics endpoints (/stats...).

Deliberately separate from /import and /history: the server stores counts the
client computed and never parses a test report. The properties that matter
are that a payload it cannot store faithfully is refused, that a re-upload of
the same run replaces rather than duplicates, and that the aggregates count
only what was actually stored.

The counts in VALID_PAYLOAD are the real output of
`scripts/count_test_results.py` on a real allure results directory from
cardano-node-tests (2145 tests, 1892 passed, 253 skipped), so the happy path
uses numbers a real client actually produces.

Several tests here cover values that only a client can supply. This is the
first table whose timestamps and counts are not server-generated, so the
range and finiteness checks are load-bearing rather than defensive.
"""

import base64
import copy
import http
import json
import sqlite3
import typing as tp
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import flask
import flask.testing
import pytest

# See the note in test_sync_results.py: `types-werkzeug` does not know about
# this class, though it is real at runtime.
from werkzeug.test import TestResponse  # type: ignore[attr-defined]

from testing_results_cache import flask_db
from testing_results_cache import stats_api
from testing_results_cache import stats_cache

TOTAL_CASES = 2145
TOTAL_PASSED = 1892
TOTAL_SKIPPED = 253
TOTAL_DURATION = 4527.316

UPGRADE_STEPS = ("step1", "step2", "step3")
RUN_IDS = ("1", "2", "3")
# Two uploads that differ in exactly one identity field must both survive.
ROWS_FOR_TWO_IDENTITIES = 2
# A `total` above the four buckets, so the derived `other` is non-zero.
OTHER_TOTAL = 10
OTHER_PASSED = 6
OTHER_FAILED = 1
EXPECTED_OTHER = OTHER_TOTAL - OTHER_PASSED - OTHER_FAILED
LIMIT_BELOW_ROW_COUNT = 2

VALID_PAYLOAD: dict = {
    "schema": 1,
    "project": "cardano-node-tests",
    "testrun_name": "node-10.5.0",
    "run_id": "1234",
    "step": "main",
    "origin": "ci",
    "timestamp": "2026-05-31T00:39:35+01:00",
    "duration": TOTAL_DURATION,
    "exit_code": 0,
    "filtered": False,
    "counts": {
        "total": TOTAL_CASES,
        "passed": TOTAL_PASSED,
        "failed": 0,
        "broken": 0,
        "skipped": TOTAL_SKIPPED,
    },
    "quality": {"never_run": 0, "no_history_id": 0, "read_errors": 0},
    "versions": {"cardano_node": "10.5.0", "cardano_cli": None, "db_sync": None},
    "commands": {"count": 213130, "coverage_pct": 31.01},
}


def _payload(**overrides: object) -> dict:
    """Return a copy of VALID_PAYLOAD with the given fields replaced."""
    out = copy.deepcopy(VALID_PAYLOAD)
    out.update(overrides)
    return out


def _put(client: flask.testing.FlaskClient, headers: dict, payload: dict | str) -> TestResponse:
    """Upload a payload, taking a raw string when the body must be malformed."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return client.put("/stats", data=body, headers=headers)


def _ok(client: flask.testing.FlaskClient, headers: dict, payload: dict) -> None:
    """Upload a payload that must be accepted, failing loudly if it is not."""
    response = _put(client, headers, payload)
    assert response.status_code == http.HTTPStatus.OK, response.data


def _row_count(app: flask.Flask) -> int:
    """Return how many rows the stats table currently holds."""
    with app.app_context():
        conn = flask_db.get_db()
        return int(conn.execute("SELECT COUNT(*) FROM testrun_stats").fetchone()[0])


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class TestAuth:
    """Tests for authentication on the three stats routes."""

    def test_upload_requires_auth(self, client: flask.testing.FlaskClient) -> None:
        """Refuse an unauthenticated upload."""
        response = client.put("/stats", data=json.dumps(VALID_PAYLOAD))
        assert response.status_code == http.HTTPStatus.UNAUTHORIZED

    def test_totals_require_auth(self, client: flask.testing.FlaskClient) -> None:
        """Refuse an unauthenticated read of the totals."""
        assert client.get("/stats").status_code == http.HTTPStatus.UNAUTHORIZED

    def test_runs_require_auth(self, client: flask.testing.FlaskClient) -> None:
        """Refuse an unauthenticated read of the listing."""
        assert client.get("/stats/runs").status_code == http.HTTPStatus.UNAUTHORIZED

    def test_wrong_password_is_refused(self, client: flask.testing.FlaskClient) -> None:
        """Refuse a known user sending the wrong token."""
        creds = base64.b64encode(b"tester:wrong").decode()
        response = _put(client, {"Authorization": f"Basic {creds}"}, VALID_PAYLOAD)
        assert response.status_code == http.HTTPStatus.UNAUTHORIZED

    def test_the_uploading_user_is_recorded(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Martin's requirement: the write uses the same user_id as the other routes."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        with app.app_context():
            conn = flask_db.get_db()
            row = conn.execute("SELECT user_id FROM testrun_stats").fetchone()
        assert row[0] is not None

    def test_a_second_user_can_also_upload(
        self, client: flask.testing.FlaskClient, other_auth_headers: dict
    ) -> None:
        """There is no per-account ownership on this service - one team, one dataset."""
        _ok(client, other_auth_headers, VALID_PAYLOAD)


class TestUpload:
    """Tests for storing a run through PUT /stats."""

    def test_stores_a_run(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Store every count from a valid upload."""
        response = _put(client, auth_headers, VALID_PAYLOAD)

        assert response.status_code == http.HTTPStatus.OK, response.data
        assert response.json["run_id"] == "1234"

        with app.app_context():
            conn = flask_db.get_db()
            row = conn.execute(
                "SELECT cases, passed, failed, broken, skipped, never_run, duration, "
                "exit_code, filtered FROM testrun_stats"
            ).fetchone()
        # tuple(): the app sets row_factory to sqlite3.Row, which does not
        # compare equal to a plain tuple.
        assert tuple(row) == (
            TOTAL_CASES,
            TOTAL_PASSED,
            0,
            0,
            TOTAL_SKIPPED,
            0,
            pytest.approx(TOTAL_DURATION),
            0,
            0,
        )

    def test_post_is_accepted_too(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Accept POST as well as PUT, like the sibling upload routes."""
        response = client.post("/stats", data=json.dumps(VALID_PAYLOAD), headers=auth_headers)
        assert response.status_code == http.HTTPStatus.OK

    def test_payload_is_stored(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The fields with no column of their own must survive the round trip."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        with app.app_context():
            conn = flask_db.get_db()
            stored = conn.execute("SELECT payload FROM testrun_stats").fetchone()[0]

        parsed = json.loads(stored)
        assert parsed["commands"] == VALID_PAYLOAD["commands"]
        assert parsed["versions"]["cardano_node"] == "10.5.0"
        assert parsed["quality"]["read_errors"] == 0

    def test_reupload_replaces_instead_of_duplicating(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Replace the row when the same run is uploaded again."""
        _ok(client, auth_headers, VALID_PAYLOAD)
        second = _payload(
            counts={**VALID_PAYLOAD["counts"], "failed": 5, "passed": TOTAL_PASSED - 5}
        )
        _ok(client, auth_headers, second)

        with app.app_context():
            conn = flask_db.get_db()
            rows = conn.execute("SELECT failed FROM testrun_stats").fetchall()
        assert [tuple(r) for r in rows] == [(5,)]

    @pytest.mark.parametrize("field", ["project", "testrun_name", "run_id", "origin"])
    def test_a_different_identity_field_makes_a_new_row(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        field: str,
    ) -> None:
        """Treat a change in any identity field as a different run."""
        _ok(client, auth_headers, VALID_PAYLOAD)
        _ok(client, auth_headers, _payload(**{field: "different"}))

        assert _row_count(app) == ROWS_FOR_TWO_IDENTITIES

    def test_step_defaults_when_absent(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Default `step` when the client omits it."""
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["step"]

        response = _put(client, auth_headers, payload)

        assert response.status_code == http.HTTPStatus.OK
        assert response.json["step"] == stats_api.DEFAULT_STEP

    def test_step_defaults_when_explicitly_null(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A client that serialises an unset field as null means "absent"."""
        response = _put(client, auth_headers, _payload(step=None))

        assert response.status_code == http.HTTPStatus.OK
        assert response.json["step"] == stats_api.DEFAULT_STEP

    def test_the_three_upgrade_steps_coexist(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The upgrade path reports three steps under one run id."""
        for step in UPGRADE_STEPS:
            _ok(client, auth_headers, _payload(step=step))

        assert _row_count(app) == len(UPGRADE_STEPS)

    def test_a_naive_timestamp_is_read_as_utc(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Read a timestamp without an offset as UTC."""
        _ok(client, auth_headers, _payload(timestamp="2026-05-31T00:39:35"))

        response = client.get("/stats/runs", headers=auth_headers)
        assert response.json[0]["timestamp"].startswith("2026-05-31T00:39:35")

    def test_offset_is_converted_to_utc(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Convert a timestamp with an offset to UTC before storing it."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        response = client.get("/stats/runs", headers=auth_headers)
        # 00:39:35+01:00 is 23:39:35 UTC the previous day.
        assert response.json[0]["timestamp"].startswith("2026-05-30T23:39:35")


class TestValidation:
    """Tests for the payload checks that run before anything is stored."""

    def test_rejects_a_non_object_body(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a body that is valid JSON but not an object."""
        response = _put(client, auth_headers, json.dumps([1, 2, 3]))
        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "JSON object" in response.json["message"]

    def test_rejects_malformed_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a body that is not JSON at all."""
        response = _put(client, auth_headers, "{not json")
        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "not valid JSON" in response.json["message"]

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_the_json_constants_python_accepts(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, literal: str
    ) -> None:
        """json.loads accepts these; JSON does not, and a browser cannot read them back.

        One stored non-finite duration would make SUM(duration) - and so the
        whole unfiltered GET /stats body - unparseable for every caller.
        """
        body = json.dumps(VALID_PAYLOAD).replace(str(TOTAL_DURATION), literal)

        response = _put(client, auth_headers, body)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "not valid JSON" in response.json["message"]
        assert _row_count(app) == 0

    def test_totals_stay_parseable_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A strict parser must be able to read the aggregate back."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        response = client.get("/stats", headers=auth_headers)

        def _no_constants(name: str) -> float:
            msg = f"non-finite {name} in response"
            raise AssertionError(msg)

        json.loads(response.data, parse_constant=_no_constants)

    def test_rejects_an_unknown_schema(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a document version this service cannot read back."""
        response = _put(client, auth_headers, _payload(schema=2))
        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Unsupported schema" in response.json["message"]

    def test_rejects_a_missing_schema(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a document that does not declare its version."""
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["schema"]
        assert _put(client, auth_headers, payload).status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_an_oversized_payload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a body over the blueprint's own size cap."""
        payload = _payload(notes="x" * (stats_api.MAX_PAYLOAD_BYTES + 1))

        response = _put(client, auth_headers, payload)

        assert response.status_code == http.HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        # The blueprint's own cap, not the app-wide 16MB handler.
        assert str(stats_api.MAX_PAYLOAD_BYTES) in response.json["message"]

    @pytest.mark.parametrize("field", ["project", "testrun_name", "run_id", "origin"])
    def test_rejects_a_missing_identity_field(
        self, client: flask.testing.FlaskClient, auth_headers: dict, field: str
    ) -> None:
        """Refuse a document that cannot identify its run."""
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload[field]

        response = _put(client, auth_headers, payload)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Missing or non-string" in response.json["message"]
        assert field in response.json["message"]

    @pytest.mark.parametrize("bad", ["../etc", "has space", "..", "", "a/b"])
    def test_rejects_an_unusable_identity_value(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """A value the read routes could not address must not become a row."""
        response = _put(client, auth_headers, _payload(project=bad))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        # The path-segment rejection specifically, not some earlier check.
        expected = "Missing or non-string" if bad == "" else "Invalid path segment"
        assert expected in response.json["message"]

    def test_rejects_counts_that_exceed_the_total(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse buckets that add up to more than the total."""
        response = _put(
            client,
            auth_headers,
            _payload(counts={"total": 10, "passed": 8, "failed": 5, "broken": 0, "skipped": 0}),
        )
        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "exceeds total" in response.json["message"]

    def test_accepts_counts_that_sum_below_the_total(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The shortfall is the 'other' bucket, which has no column."""
        _ok(
            client,
            auth_headers,
            _payload(
                counts={
                    "total": OTHER_TOTAL,
                    "passed": OTHER_PASSED,
                    "failed": OTHER_FAILED,
                    "broken": 0,
                    "skipped": 0,
                }
            ),
        )

        listed = client.get("/stats/runs", headers=auth_headers).json[0]
        assert listed["other"] == EXPECTED_OTHER

    @pytest.mark.parametrize("field", ["total", "passed", "failed", "broken", "skipped"])
    def test_rejects_a_missing_count(
        self, client: flask.testing.FlaskClient, auth_headers: dict, field: str
    ) -> None:
        """Refuse a counts block with a bucket missing."""
        counts = dict(VALID_PAYLOAD["counts"])
        del counts[field]

        response = _put(client, auth_headers, _payload(counts=counts))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert f"counts.{field}" in response.json["message"]

    @pytest.mark.parametrize("bad", [-1, "5", 1.5, None, True])
    def test_rejects_a_non_integer_count(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: object
    ) -> None:
        """Refuse a count that is not a non-negative integer."""
        counts = {**VALID_PAYLOAD["counts"], "passed": bad}
        assert (
            _put(client, auth_headers, _payload(counts=counts)).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    def test_rejects_a_count_above_the_cap(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """An unbounded JSON integer would raise OverflowError inside the driver."""
        counts = {**VALID_PAYLOAD["counts"], "total": stats_api.MAX_COUNT + 1}

        response = _put(client, auth_headers, _payload(counts=counts))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "larger than" in response.json["message"]
        assert _row_count(app) == 0

    def test_counts_cannot_overflow_the_sum_across_rows(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A per-field bound alone does not stop SUM() overflowing.

        Two rows of 2**62 are each individually under the sqlite integer
        limit, but their sum is not, and `SUM(cases)` then raises
        `integer overflow` - breaking every GET /stats variant permanently,
        with no delete route to recover.
        """
        counts = {**VALID_PAYLOAD["counts"], "total": 2**62}
        for run_id in ("a", "b"):
            response = _put(client, auth_headers, _payload(run_id=run_id, counts=counts))
            assert response.status_code == http.HTTPStatus.BAD_REQUEST

        assert client.get("/stats", headers=auth_headers).status_code == http.HTTPStatus.OK

    def test_the_largest_allowed_counts_still_sum(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The cap has to leave room for a realistic number of runs."""
        counts = {
            "total": stats_api.MAX_COUNT,
            "passed": stats_api.MAX_COUNT,
            "failed": 0,
            "broken": 0,
            "skipped": 0,
        }
        for run_id in RUN_IDS:
            _ok(client, auth_headers, _payload(run_id=run_id, counts=counts))

        totals = client.get("/stats", headers=auth_headers).json
        assert totals["cases"] == stats_api.MAX_COUNT * len(RUN_IDS)

    @pytest.mark.parametrize("literal", ["1e400", "-1e400"])
    def test_rejects_a_numeric_literal_that_overflows_to_infinity(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, literal: str
    ) -> None:
        """`1e400` is an ordinary number literal, so parse_constant never fires.

        Left through, it reaches the stored payload as the token `Infinity`,
        which breaks the promise that the column always holds readable JSON -
        the whole reason the column exists.
        """
        body = json.dumps(VALID_PAYLOAD).replace('"cardano_node": "10.5.0"', f'"x": {literal}')

        response = _put(client, auth_headers, body)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert _row_count(app) == 0

    def test_the_stored_payload_is_always_readable_json(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """No accepted upload may leave a non-finite value in the column."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        with app.app_context():
            conn = flask_db.get_db()
            stored = conn.execute("SELECT payload FROM testrun_stats").fetchone()[0]

        def _no_constants(name: str) -> float:
            msg = f"non-finite {name} in stored payload"
            raise AssertionError(msg)

        json.loads(stored, parse_constant=_no_constants)

    def test_rejects_a_missing_counts_block(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a document with no counts at all."""
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["counts"]
        assert _put(client, auth_headers, payload).status_code == http.HTTPStatus.BAD_REQUEST

    def test_rejects_never_run_above_skipped(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """never_run is a subset of skipped - the registration files are skips."""
        response = _put(client, auth_headers, _payload(quality={"never_run": TOTAL_SKIPPED + 1}))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "never_run" in response.json["message"]

    def test_accepts_never_run_equal_to_skipped(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """An entirely interrupted run: every skip is a test that never ran."""
        _ok(client, auth_headers, _payload(quality={"never_run": TOTAL_SKIPPED}))

    def test_quality_is_required(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Defaulting never_run to 0 would claim an interrupted run was complete."""
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["quality"]

        response = _put(client, auth_headers, payload)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "quality" in response.json["message"]

    @pytest.mark.parametrize("bad", [[], "none", 0])
    def test_rejects_a_non_object_quality(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: object
    ) -> None:
        """Falsy non-dicts must report the type error, not a confusing field error."""
        response = _put(client, auth_headers, _payload(quality=bad))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "non-object field 'quality'" in response.json["message"]

    @pytest.mark.parametrize("bad", [-1.0, "fast", None, True])
    def test_rejects_a_bad_duration(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: object
    ) -> None:
        """Refuse a duration that is not a non-negative number."""
        assert (
            _put(client, auth_headers, _payload(duration=bad)).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    @pytest.mark.parametrize("bad", ["0", 1.5, None, True])
    def test_rejects_a_bad_exit_code(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: object
    ) -> None:
        """Refuse an exit code that is not an integer."""
        assert (
            _put(client, auth_headers, _payload(exit_code=bad)).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    def test_rejects_an_out_of_range_exit_code(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse an exit code sqlite could not store."""
        response = _put(client, auth_headers, _payload(exit_code=stats_api.MAX_SQLITE_INT + 1))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "out of range" in response.json["message"]

    def test_accepts_a_nonzero_exit_code(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Store a run that failed, not just one that passed."""
        _ok(client, auth_headers, _payload(exit_code=1))

    @pytest.mark.parametrize("bad", ["yesterday", "", 20260531, None])
    def test_rejects_a_bad_timestamp(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: object
    ) -> None:
        """Refuse a timestamp that is not an ISO-8601 string."""
        assert (
            _put(client, auth_headers, _payload(timestamp=bad)).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    @pytest.mark.parametrize(
        "bad", ["9999-12-31T23:59:59.999999-23:59", "0001-01-01T00:00:00+23:59"]
    )
    def test_rejects_a_timestamp_that_overflows_on_conversion(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """Fromisoformat accepts +/-24h offsets, so a near-limit year overflows.

        Unhandled this escapes as Werkzeug's HTML 500 and breaks the
        JSON-error contract the rest of the service keeps.
        """
        response = _put(client, auth_headers, _payload(timestamp=bad))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert response.content_type.startswith("application/json")
        assert _row_count(app) == 0

    @pytest.mark.parametrize("bad", ["0001-01-01T00:00:00+00:00", "0999-12-31T00:00:00+00:00"])
    def test_rejects_a_year_the_storage_format_cannot_read_back(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """strftime("%Y") does not zero-pad but strptime("%Y") needs four digits.

        Such a row would be counted by GET /stats and invisible to
        GET /stats/runs, so the two routes would disagree forever.
        """
        response = _put(client, auth_headers, _payload(timestamp=bad))

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "storable range" in response.json["message"]
        assert _row_count(app) == 0

    def test_every_stored_run_is_readable_back(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The totals and the listing must never disagree on the row count."""
        _ok(client, auth_headers, _payload(timestamp=_now_iso()))

        totals = client.get("/stats", headers=auth_headers).json
        listed = client.get("/stats/runs", headers=auth_headers).json

        assert totals["runs"] == len(listed)

    def test_rejects_a_non_boolean_filtered(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a `filtered` flag that is not a boolean."""
        assert (
            _put(client, auth_headers, _payload(filtered="yes")).status_code
            == http.HTTPStatus.BAD_REQUEST
        )

    def test_a_rejected_upload_stores_nothing(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Leave the table untouched when a document is refused."""
        assert _put(client, auth_headers, _payload(schema=99)).status_code == (
            http.HTTPStatus.BAD_REQUEST
        )
        assert _row_count(app) == 0


class TestTotals:
    """Tests for the aggregate route, GET /stats."""

    def test_empty_database_totals_to_zero(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Report zeroes rather than failing when nothing is stored."""
        response = client.get("/stats", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.OK
        assert response.json["runs"] == 0
        assert response.json["cases"] == 0

    def test_sums_across_runs(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        """Add the counts of every matching run together."""
        for run_id in RUN_IDS:
            _ok(client, auth_headers, _payload(run_id=run_id))

        totals = client.get("/stats", headers=auth_headers).json

        assert totals["runs"] == len(RUN_IDS)
        assert totals["cases"] == TOTAL_CASES * len(RUN_IDS)
        assert totals["passed"] == TOTAL_PASSED * len(RUN_IDS)
        assert totals["duration"] == pytest.approx(TOTAL_DURATION * len(RUN_IDS))

    def test_reports_never_run(self, client: flask.testing.FlaskClient, auth_headers: dict) -> None:
        """Without it a caller cannot tell interrupted runs were summed in."""
        _ok(client, auth_headers, _payload(quality={"never_run": TOTAL_SKIPPED}))

        assert client.get("/stats", headers=auth_headers).json["never_run"] == TOTAL_SKIPPED

    def test_narrows_by_project(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Count only the named project when one is given."""
        _ok(client, auth_headers, VALID_PAYLOAD)
        _ok(client, auth_headers, _payload(project="cardano-sync-tests"))

        totals = client.get("/stats?project=cardano-sync-tests", headers=auth_headers).json

        assert totals["runs"] == 1
        assert totals["project"] == "cardano-sync-tests"

    def test_day_window_includes_a_recent_run(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Without this, a window that matched nothing would pass every other test."""
        _ok(client, auth_headers, _payload(timestamp=_now_iso()))

        assert client.get("/stats?days=7", headers=auth_headers).json["runs"] == 1

    def test_day_window_excludes_an_older_run(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Drop a run that falls outside the day window."""
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        _ok(client, auth_headers, _payload(timestamp=old))

        assert client.get("/stats?days=7", headers=auth_headers).json["runs"] == 0
        assert client.get("/stats", headers=auth_headers).json["runs"] == 1

    def test_day_window_keeps_a_run_just_inside_it(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Keep a run that falls just inside the day window."""
        recent = (datetime.now(UTC) - timedelta(days=6)).isoformat()
        _ok(client, auth_headers, _payload(timestamp=recent))

        assert client.get("/stats?days=7", headers=auth_headers).json["runs"] == 1

    @pytest.mark.parametrize("bad", ["x", "0", "-5", "400"])
    def test_rejects_a_bad_day_window(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """Refuse a day window that is not an integer in range."""
        response = client.get(f"/stats?days={bad}", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "days" in response.json["message"]

    def test_rejects_an_unusable_project_filter(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Refuse a project filter that is not a usable path segment."""
        response = client.get("/stats?project=../etc", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "Invalid path segment" in response.json["message"]


class TestListRuns:
    """Tests for the per-run listing, GET /stats/runs."""

    def test_lists_newest_first(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Order the listing by timestamp, newest first."""
        _ok(client, auth_headers, _payload(run_id="old", timestamp="2026-01-01T00:00:00+00:00"))
        _ok(client, auth_headers, _payload(run_id="new", timestamp="2026-02-01T00:00:00+00:00"))

        runs = client.get("/stats/runs", headers=auth_headers).json

        assert [r["run_id"] for r in runs] == ["new", "old"]

    def test_day_window_applies_to_the_listing_too(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Apply the day window to the listing, not only the totals."""
        _ok(client, auth_headers, _payload(run_id="recent", timestamp=_now_iso()))
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        _ok(client, auth_headers, _payload(run_id="old", timestamp=old))

        runs = client.get("/stats/runs?days=7", headers=auth_headers).json

        assert [r["run_id"] for r in runs] == ["recent"]

    def test_limit_caps_the_listing(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Return no more rows than `limit` asks for."""
        for run_id in RUN_IDS:
            _ok(client, auth_headers, _payload(run_id=run_id))

        listed = client.get(f"/stats/runs?limit={LIMIT_BELOW_ROW_COUNT}", headers=auth_headers).json
        assert len(listed) == LIMIT_BELOW_ROW_COUNT

    def test_does_not_expose_the_stored_payload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The listing is summary only - a future public page reads from here."""
        _ok(client, auth_headers, VALID_PAYLOAD)

        run = client.get("/stats/runs", headers=auth_headers).json[0]

        assert "payload" not in run
        assert "versions" not in run
        assert "commands" not in run

    @pytest.mark.parametrize("bad", ["x", "0", "-1"])
    def test_rejects_a_bad_limit(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """Refuse a limit that is not a positive integer."""
        response = client.get(f"/stats/runs?limit={bad}", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert "limit" in response.json["message"]

    def test_refuses_a_limit_above_the_cap_rather_than_truncating(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """There is no cursor, so a silently clamped listing looks complete."""
        response = client.get(
            f"/stats/runs?limit={stats_cache.MAX_LIST_ROWS + 1}", headers=auth_headers
        )

        assert response.status_code == http.HTTPStatus.BAD_REQUEST
        assert str(stats_cache.MAX_LIST_ROWS) in response.json["message"]


class TestStorageFailures:
    """Tests for how a failed write is reported."""

    def test_a_busy_database_asks_the_caller_to_retry(
        self, client: flask.testing.FlaskClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A concurrent writer elsewhere in the service is transient, not a failure."""
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        def _busy(**_kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(stats_api.stats_cache, "save_testrun_stats", _busy)

        response = _put(client, auth_headers, VALID_PAYLOAD)

        assert response.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == "5"
        assert response.json["message"] == "Server busy, try again"

    def test_a_wal_busy_snapshot_is_also_transient(
        self, client: flask.testing.FlaskClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The extended code under WAL is 517; an equality test would miss it."""
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = 517

        def _busy(**_kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(stats_api.stats_cache, "save_testrun_stats", _busy)

        response = _put(client, auth_headers, VALID_PAYLOAD)

        assert response.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE

    def test_a_hard_db_error_is_reported_as_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report a non-transient write failure as a JSON 500."""

        def _boom(**_kwargs: object) -> None:
            msg = "disk I/O error"
            raise sqlite3.DatabaseError(msg)

        monkeypatch.setattr(stats_api.stats_cache, "save_testrun_stats", _boom)

        response = _put(client, auth_headers, VALID_PAYLOAD)

        assert response.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.json["message"] == "Failed to store testrun stats"

    def test_a_failed_upload_stores_nothing(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """A commit failure must not leave a half-written row behind."""
        original = stats_api.stats_cache.save_testrun_stats

        def _save_then_fail(**kwargs: object) -> None:
            original(**kwargs)  # type: ignore[arg-type]
            msg = "commit refused"
            raise sqlite3.DatabaseError(msg)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(stats_api.stats_cache, "save_testrun_stats", _save_then_fail)
            assert _put(client, auth_headers, VALID_PAYLOAD).status_code == (
                http.HTTPStatus.INTERNAL_SERVER_ERROR
            )

        assert _row_count(app) == 0


class TestReadFailures:
    """Tests for how a failed read is reported."""

    def test_totals_report_a_db_error_as_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrun migration must not break the JSON-error contract."""

        def _boom(**_kwargs: object) -> None:
            msg = "no such table: testrun_stats"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(stats_api.stats_cache, "get_totals", _boom)

        response = client.get("/stats", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.json["message"] == "Failed to read testrun stats"

    def test_listing_reports_a_db_error_as_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report a failed listing as a JSON 500."""

        def _boom(**_kwargs: object) -> None:
            msg = "no such table: testrun_stats"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(stats_api.stats_cache, "list_testrun_stats", _boom)

        response = client.get("/stats/runs", headers=auth_headers)

        assert response.status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.json["message"] == "Failed to read testrun stats"

    def test_a_malformed_stored_timestamp_is_skipped_not_fatal(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """One corrupt row must not hide every other run from the listing."""
        _ok(client, auth_headers, _payload(run_id="good", timestamp=_now_iso()))
        _ok(client, auth_headers, _payload(run_id="bad", timestamp=_now_iso()))

        with app.app_context():
            conn = flask_db.get_db()
            conn.execute("UPDATE testrun_stats SET timestamp = 'nonsense' WHERE run_id = 'bad'")
            conn.commit()

        runs: tp.List[dict] = client.get("/stats/runs", headers=auth_headers).json

        assert [r["run_id"] for r in runs] == ["good"]
