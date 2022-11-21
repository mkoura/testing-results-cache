"""A flask app for caching testing results."""
import logging
import os
from pathlib import Path
from typing import Optional

import flask

from testing_results_cache import flask_db
from testing_results_cache import results_api


INSTANCE_PATH = Path(__file__).parent.parent / "instance_dev"

logging.basicConfig(format="%(name)s: %(levelname)s: %(message)s", level=logging.WARNING)


def get_instance_path() -> Optional[str]:
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
        MAX_CONTENT_LENGTH=16 * 1000 * 1000,  # 16MB
    )

    # load the instance config, if it exists
    app.config.from_pyfile("config.py", silent=True)

    app.teardown_appcontext(flask_db.close_db)

    app.cli.add_command(flask_db.init_db_command)
    app.cli.add_command(flask_db.add_user)

    app.register_blueprint(results_api.results)

    return app
