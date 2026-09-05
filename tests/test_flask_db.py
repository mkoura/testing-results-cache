"""Tests for flask_db.py's init-db behavior.

Scoped to one thing: the README documents init-db as "drops and recreates
all tables" for existing deployments doing an upgrade. Every other test in
this repo only ever calls init_db() once, against a brand-new empty
database file, so that documented drop-and-recreate promise was never
actually exercised against a populated one.
"""

import flask

from testing_results_cache import flask_db


class TestInitDb:
    def test_second_call_drops_existing_rows(self, app: flask.Flask) -> None:
        with app.app_context():
            conn = flask_db.get_db()
            conn.execute("INSERT INTO users(name, password_hash) VALUES ('leftover', 'x')")
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0

            flask_db.init_db()

            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
