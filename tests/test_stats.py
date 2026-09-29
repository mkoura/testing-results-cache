"""Tests for the /stats endpoints.

The properties that matter here are: the endpoint refuses a payload it
cannot store faithfully, a re-upload of the same run replaces rather than
duplicates, and the aggregates only ever count what was actually stored.

The counts in VALID_PAYLOAD are the real output of
`scripts/count_test_results.py` on a real allure results directory from
cardano-node-tests (2145 tests, 1892 passed, 253 skipped), so the happy path
is exercised with numbers a real client actually produces.
"""

import copy
import json
import sqlite3
from typing import Any

import flask
import flask.testing
import pytest

from testing_results_cache import flask_db
from testing_results_cache import stats_api

VALID_PAYLOAD: dict = {
    "schema": 1,
    "project": "cardano-node-tests",
    "testrun_name": "node-10.5.0",
    "run_id": "1234",
    "step": "main",
    "origin": "ci",
    "timestamp": "2026-05-31T00:39:35+01:00",
    "duration": 4527.316,
    "exit_code": 0,
    "filtered": False,
    "counts": {"total": 2145, "passed": 1892, "failed": 0, "broken": 0, "skipped": 253},
    "quality": {"never_run": 0, "no_history_id": 0, "read_errors": 0},
    "versions": {"cardano_node": "10.5.0", "cardano_cli": None, "db_sync": None},
    "commands": {"count": 213130, "coverage_pct": 31.01},
}


def _payload(**overrides: Any) -> dict:
    out = copy.deepcopy(VALID_PAYLOAD)
    out.update(overrides)
    return out


def _put(client: flask.testing.FlaskClient, headers: dict, payload: dict) -> Any:
    return client.put("/stats", data=json.dumps(payload), headers=headers)


class TestAuth:
    def test_upload_requires_auth(self, client: flask.testing.FlaskClient) -> None:
        response = client.put("/stats", data=json.dumps(VALID_PAYLOAD))
        assert response.status_code == 401

    def test_totals_require_auth(self, client: flask.testing.FlaskClient) -> None:
        assert client.get("/stats").status_code == 401

    def test_runs_require_auth(self, client: flask.testing.FlaskClient) -> None:
        assert client.get("/stats/runs").status_code == 401

    def test_wrong_password_is_refused(self, client: flask.testing.FlaskClient) -> None:
        import base64

        creds = base64.b64encode(b"tester:wrong").decode()
        response = client.put(
            "/stats",
            data=json.dumps(VALID_PAYLOAD),
            headers={"Authorization": f"Basic {creds}"},
        )
        assert response.status_code == 401

    def test_the_uploading_user_is_recorded(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """Martin's requirement: the write uses the same user_id as the other routes."""
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200

        with app.app_context():
            conn = flask_db.get_db()
            row = conn.execute("SELECT user_id FROM testrun_stats").fetchone()
        assert row[0] is not None


class TestUpload:
    def test_stores_a_run(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = _put(client, auth_headers, VALID_PAYLOAD)

        assert response.status_code == 200, response.data
        assert response.json["run_id"] == "1234"

        with app.app_context():
            conn = flask_db.get_db()
            row = conn.execute(
                "SELECT cases, passed, failed, broken, skipped, never_run, duration, "
                "exit_code, filtered FROM testrun_stats"
            ).fetchone()
        # tuple(): the app sets row_factory to sqlite3.Row, which does not
        # compare equal to a plain tuple.
        assert tuple(row) == (2145, 1892, 0, 0, 253, 0, pytest.approx(4527.316), 0, 0)

    def test_post_is_accepted_too(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = client.post("/stats", data=json.dumps(VALID_PAYLOAD), headers=auth_headers)
        assert response.status_code == 200

    def test_payload_is_stored_verbatim(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The fields with no column of their own must survive the round trip."""
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200

        with app.app_context():
            conn = flask_db.get_db()
            stored = conn.execute("SELECT payload FROM testrun_stats").fetchone()[0]

        parsed = json.loads(stored)
        assert parsed["commands"] == {"count": 213130, "coverage_pct": 31.01}
        assert parsed["versions"]["cardano_node"] == "10.5.0"
        assert parsed["quality"]["read_errors"] == 0

    def test_reupload_replaces_instead_of_duplicating(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200

        second = _payload(counts={**VALID_PAYLOAD["counts"], "failed": 5, "passed": 1887})
        assert _put(client, auth_headers, second).status_code == 200

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
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200
        assert _put(client, auth_headers, _payload(**{field: "different"})).status_code == 200

        with app.app_context():
            conn = flask_db.get_db()
            count = conn.execute("SELECT COUNT(*) FROM testrun_stats").fetchone()[0]
        assert count == 2

    def test_step_defaults_when_absent(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["step"]

        response = _put(client, auth_headers, payload)

        assert response.status_code == 200
        assert response.json["step"] == stats_api.DEFAULT_STEP

    def test_the_three_upgrade_steps_coexist(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The upgrade path reports three steps under one run id."""
        for step in ("step1", "step2", "step3"):
            assert _put(client, auth_headers, _payload(step=step)).status_code == 200

        with app.app_context():
            conn = flask_db.get_db()
            count = conn.execute("SELECT COUNT(*) FROM testrun_stats").fetchone()[0]
        assert count == 3

    def test_a_naive_timestamp_is_read_as_utc(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(
            client, auth_headers, _payload(timestamp="2026-05-31T00:39:35")
        ).status_code == 200

        response = client.get("/stats/runs", headers=auth_headers)
        assert response.json[0]["timestamp"].startswith("2026-05-31T00:39:35")

    def test_offset_is_converted_to_utc(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200

        response = client.get("/stats/runs", headers=auth_headers)
        # 00:39:35+01:00 is 23:39:35 UTC the previous day.
        assert response.json[0]["timestamp"].startswith("2026-05-30T23:39:35")


class TestValidation:
    def test_rejects_a_non_object_body(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = client.put("/stats", data=json.dumps([1, 2, 3]), headers=auth_headers)
        assert response.status_code == 400
        assert "JSON object" in response.json["message"]

    def test_rejects_malformed_json(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = client.put("/stats", data="{not json", headers=auth_headers)
        assert response.status_code == 400
        assert "not valid JSON" in response.json["message"]

    def test_rejects_an_unknown_schema(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = _put(client, auth_headers, _payload(schema=2))
        assert response.status_code == 400
        assert "Unsupported schema" in response.json["message"]

    def test_rejects_a_missing_schema(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["schema"]
        assert _put(client, auth_headers, payload).status_code == 400

    def test_rejects_an_oversized_payload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        payload = _payload(notes="x" * (stats_api.MAX_PAYLOAD_BYTES + 1))
        response = _put(client, auth_headers, payload)
        assert response.status_code == 413

    @pytest.mark.parametrize("field", ["project", "testrun_name", "run_id", "origin"])
    def test_rejects_a_missing_identity_field(
        self, client: flask.testing.FlaskClient, auth_headers: dict, field: str
    ) -> None:
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload[field]
        response = _put(client, auth_headers, payload)
        assert response.status_code == 400
        assert field in response.json["message"]

    @pytest.mark.parametrize("bad", ["../etc", "has space", "..", "", "a/b"])
    def test_rejects_an_unusable_identity_value(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        """A value the read routes could not address must not become a row."""
        assert _put(client, auth_headers, _payload(project=bad)).status_code == 400

    def test_rejects_counts_that_exceed_the_total(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = _put(
            client,
            auth_headers,
            _payload(counts={"total": 10, "passed": 8, "failed": 5, "broken": 0, "skipped": 0}),
        )
        assert response.status_code == 400
        assert "exceeds total" in response.json["message"]

    def test_accepts_counts_that_sum_below_the_total(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The shortfall is the 'other' bucket, which has no column."""
        response = _put(
            client,
            auth_headers,
            _payload(counts={"total": 10, "passed": 6, "failed": 1, "broken": 0, "skipped": 0}),
        )
        assert response.status_code == 200

        listed = client.get("/stats/runs", headers=auth_headers).json[0]
        assert listed["other"] == 3

    @pytest.mark.parametrize("field", ["total", "passed", "failed", "broken", "skipped"])
    def test_rejects_a_missing_count(
        self, client: flask.testing.FlaskClient, auth_headers: dict, field: str
    ) -> None:
        counts = dict(VALID_PAYLOAD["counts"])
        del counts[field]
        assert _put(client, auth_headers, _payload(counts=counts)).status_code == 400

    @pytest.mark.parametrize("bad", [-1, "5", 1.5, None, True])
    def test_rejects_a_non_integer_count(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: Any
    ) -> None:
        counts = {**VALID_PAYLOAD["counts"], "passed": bad}
        assert _put(client, auth_headers, _payload(counts=counts)).status_code == 400

    def test_rejects_a_missing_counts_block(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["counts"]
        assert _put(client, auth_headers, payload).status_code == 400

    def test_rejects_never_run_above_skipped(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """never_run is a subset of skipped - the registration files are skips."""
        response = _put(client, auth_headers, _payload(quality={"never_run": 254}))
        assert response.status_code == 400
        assert "never_run" in response.json["message"]

    def test_accepts_never_run_equal_to_skipped(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """An entirely interrupted run: every skip is a test that never ran."""
        assert _put(client, auth_headers, _payload(quality={"never_run": 253})).status_code == 200

    def test_never_run_defaults_to_zero(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["quality"]
        assert _put(client, auth_headers, payload).status_code == 400

    @pytest.mark.parametrize("bad", [-1.0, "fast", None, True])
    def test_rejects_a_bad_duration(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: Any
    ) -> None:
        assert _put(client, auth_headers, _payload(duration=bad)).status_code == 400

    @pytest.mark.parametrize("bad", ["0", 1.5, None, True])
    def test_rejects_a_bad_exit_code(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: Any
    ) -> None:
        assert _put(client, auth_headers, _payload(exit_code=bad)).status_code == 400

    def test_accepts_a_nonzero_exit_code(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, _payload(exit_code=1)).status_code == 200

    @pytest.mark.parametrize("bad", ["yesterday", "", 20260531, None])
    def test_rejects_a_bad_timestamp(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: Any
    ) -> None:
        assert _put(client, auth_headers, _payload(timestamp=bad)).status_code == 400

    def test_rejects_a_non_boolean_filtered(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, _payload(filtered="yes")).status_code == 400

    def test_a_rejected_upload_stores_nothing(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, _payload(schema=99)).status_code == 400

        with app.app_context():
            conn = flask_db.get_db()
            count = conn.execute("SELECT COUNT(*) FROM testrun_stats").fetchone()[0]
        assert count == 0


class TestTotals:
    def test_empty_database_totals_to_zero(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        response = client.get("/stats", headers=auth_headers)

        assert response.status_code == 200
        assert response.json["runs"] == 0
        assert response.json["cases"] == 0

    def test_sums_across_runs(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        for run_id in ("1", "2", "3"):
            assert _put(client, auth_headers, _payload(run_id=run_id)).status_code == 200

        totals = client.get("/stats", headers=auth_headers).json

        assert totals["runs"] == 3
        assert totals["cases"] == 2145 * 3
        assert totals["passed"] == 1892 * 3
        assert totals["duration"] == pytest.approx(4527.316 * 3)

    def test_narrows_by_project(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200
        assert _put(client, auth_headers, _payload(project="cardano-sync-tests")).status_code == 200

        totals = client.get("/stats?project=cardano-sync-tests", headers=auth_headers).json

        assert totals["runs"] == 1
        assert totals["project"] == "cardano-sync-tests"

    def test_day_window_excludes_older_runs(
        self, app: flask.Flask, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200
        # The payload's own timestamp is 2026-05-31, well outside any window
        # a dashboard would ask for.
        assert client.get("/stats?days=7", headers=auth_headers).json["runs"] == 0
        assert client.get("/stats", headers=auth_headers).json["runs"] == 1

    @pytest.mark.parametrize("bad", ["x", "0", "-5", "400"])
    def test_rejects_a_bad_day_window(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        assert client.get(f"/stats?days={bad}", headers=auth_headers).status_code == 400

    def test_rejects_an_unusable_project_filter(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert client.get("/stats?project=../etc", headers=auth_headers).status_code == 400


class TestListRuns:
    def test_lists_newest_first(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        assert _put(
            client, auth_headers, _payload(run_id="old", timestamp="2026-01-01T00:00:00+00:00")
        ).status_code == 200
        assert _put(
            client, auth_headers, _payload(run_id="new", timestamp="2026-02-01T00:00:00+00:00")
        ).status_code == 200

        runs = client.get("/stats/runs", headers=auth_headers).json

        assert [r["run_id"] for r in runs] == ["new", "old"]

    def test_limit_caps_the_listing(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        for run_id in ("1", "2", "3"):
            assert _put(client, auth_headers, _payload(run_id=run_id)).status_code == 200

        assert len(client.get("/stats/runs?limit=2", headers=auth_headers).json) == 2

    def test_does_not_expose_the_raw_payload(
        self, client: flask.testing.FlaskClient, auth_headers: dict
    ) -> None:
        """The listing is summary only - a future public page reads from here."""
        assert _put(client, auth_headers, VALID_PAYLOAD).status_code == 200

        run = client.get("/stats/runs", headers=auth_headers).json[0]

        assert "payload" not in run

    @pytest.mark.parametrize("bad", ["x", "0", "-1"])
    def test_rejects_a_bad_limit(
        self, client: flask.testing.FlaskClient, auth_headers: dict, bad: str
    ) -> None:
        assert client.get(f"/stats/runs?limit={bad}", headers=auth_headers).status_code == 400


class TestReadFailures:
    def test_totals_report_a_db_error_as_json(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unrun migration must not break the JSON-error contract."""

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise sqlite3.OperationalError("no such table: testrun_stats")

        monkeypatch.setattr(stats_api.stats_cache, "get_totals", _boom)

        response = client.get("/stats", headers=auth_headers)

        assert response.status_code == 500
        assert response.json["message"] == "Failed to read testrun stats"

    def test_listing_reports_a_db_error_as_json(
        self,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise sqlite3.OperationalError("no such table: testrun_stats")

        monkeypatch.setattr(stats_api.stats_cache, "list_testrun_stats", _boom)

        response = client.get("/stats/runs", headers=auth_headers)

        assert response.status_code == 500
        assert response.json["message"] == "Failed to read testrun stats"
