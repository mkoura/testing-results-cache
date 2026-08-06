import base64
from pathlib import Path
from typing import Iterator

import flask
import flask.testing
import pytest
from werkzeug import security

from testing_results_cache import app as app_module
from testing_results_cache import flask_db
from testing_results_cache import users

TEST_USERNAME = "tester"
TEST_PASSWORD = "secret"
OTHER_USERNAME = "other-tester"
OTHER_PASSWORD = "other-secret"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[flask.Flask]:
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path / "instance"))
    flask_app = app_module.create_app()
    flask_app.config.update(
        TESTING=True,
        DATABASE=str(tmp_path / "test.db"),
        UPLOAD_FOLDER=str(tmp_path / "uploads"),
        HISTORY_FOLDER=str(tmp_path / "history"),
    )

    with flask_app.app_context():
        flask_db.init_db()
        conn = flask_db.get_db()
        users.add_user(
            conn=conn,
            user_name=TEST_USERNAME,
            password_hash=security.generate_password_hash(TEST_PASSWORD),
        )
        users.add_user(
            conn=conn,
            user_name=OTHER_USERNAME,
            password_hash=security.generate_password_hash(OTHER_PASSWORD),
        )

    yield flask_app

    with flask_app.app_context():
        flask_db.close_db()


@pytest.fixture
def client(app: flask.Flask) -> flask.testing.FlaskClient:
    return app.test_client()


@pytest.fixture
def auth_headers() -> dict:
    creds = base64.b64encode(f"{TEST_USERNAME}:{TEST_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture
def other_auth_headers() -> dict:
    """Return auth headers for a second, distinct authenticated user."""
    creds = base64.b64encode(f"{OTHER_USERNAME}:{OTHER_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}
