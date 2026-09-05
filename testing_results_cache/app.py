"""A flask app for caching testing results."""

import logging
import os
from pathlib import Path

import flask

from testing_results_cache import flask_db
from testing_results_cache import history_api
from testing_results_cache import results_api
from testing_results_cache import sync_results_api

INSTANCE_PATH = Path(__file__).parent.parent / "instance_dev"

logging.basicConfig(format="%(name)s: %(levelname)s: %(message)s", level=logging.WARNING)


def get_instance_path() -> str | None:
    """Get the absolute instance path."""
    instance_path_env = os.environ.get("INSTANCE_PATH")
    if instance_path_env:
        return str(Path(instance_path_env).expanduser().resolve())

    # if `instance_path` was not specified and the package is installed,
    # use the default instance path
    if not (INSTANCE_PATH.parent / ".git").exists():
        return None

    return str(INSTANCE_PATH)


def create_app() -> flask.Flask:
    """Create and configure an instance of the Flask application."""
    app = flask.Flask(__name__, instance_relative_config=True, instance_path=get_instance_path())

    instance_path = Path(app.instance_path)
    instance_path.mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE=str(instance_path / "testing_results_cache.db"),
        UPLOAD_FOLDER=str(instance_path / "uploads"),
        HISTORY_FOLDER=str(instance_path / "history"),
        SYNC_RESULTS_FOLDER=str(instance_path / "sync_results"),
        MAX_CONTENT_LENGTH=16 * 1000 * 1000,  # 16MB
    )

    # load the instance config, if it exists
    app.config.from_pyfile("config.py", silent=True)

    app.teardown_appcontext(flask_db.close_db)

    app.cli.add_command(flask_db.init_db_command)
    app.cli.add_command(flask_db.add_user)
    app.cli.add_command(flask_db.migrate_command)

    app.register_blueprint(results_api.results)
    app.register_blueprint(history_api.history)
    app.register_blueprint(sync_results_api.sync_results)

    @app.errorhandler(413)
    def _request_entity_too_large(_exc: Exception) -> flask.Response:
        # Every other error response in this app is JSON - without this,
        # Werkzeug's default HTML error page is the one exception, and
        # nothing records that an oversized upload was rejected.
        app.logger.warning(
            f"Rejected an oversized request ({flask.request.content_length} bytes) "
            f"to {flask.request.path}"
        )
        response: flask.Response = flask.jsonify(message="Request too large")
        response.status_code = 413
        return response

    return app
