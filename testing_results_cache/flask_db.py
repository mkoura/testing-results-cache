import sqlite3
from typing import Any

import click
import flask
from werkzeug import security

from testing_results_cache import users


def get_db() -> sqlite3.Connection:
    if "db" not in flask.g:
        conn = sqlite3.connect(
            flask.current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES
        )
        flask.g.db = conn
        flask.g.db = conn
        flask.g.db.row_factory = sqlite3.Row

    return flask.g.db  # type: ignore


def init_db() -> None:
    db = get_db()

    with flask.current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode())


def close_db(e: Any = None) -> None:  # pylint: disable=unused-argument,invalid-name
    db = flask.g.pop("db", None)

    if db is not None:
        db.close()


@click.command("add-user")
@click.option(
    "--username",
    type=str,
    required=True,
    help="Username to add.",
)
@click.password_option()
def add_user(username: str, password: str) -> None:
    """Add user to database."""
    conn = get_db()
    password_hash = security.generate_password_hash(password)
    users.add_user(conn=conn, user_name=username, password_hash=password_hash)
    click.echo(f"Added user {username}.")


@click.command("init-db")
def init_db_command() -> None:
    """Clear the existing data and create new tables."""
    init_db()
    click.echo("Initialized the database.")
