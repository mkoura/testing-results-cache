import sqlite3

import click
import flask
from werkzeug import security

from testing_results_cache import migrations_runner
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


def close_db(_exc: BaseException | None = None) -> None:
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


@click.command("migrate")
@click.option("--dry-run", is_flag=True, help="Show what would be applied, change nothing.")
def migrate_command(dry_run: bool) -> None:
    """Apply any migrations the database has not seen yet."""
    conn = get_db()
    try:
        todo = migrations_runner.pending(conn=conn)
    except migrations_runner.MigrationError as err:
        raise click.ClickException(str(err)) from err

    if not todo:
        click.echo(f"Up to date at version {migrations_runner.current_version(conn)}.")
        return

    if dry_run:
        for migration in todo:
            click.echo(f"Would apply {migration.path.name}")
        return

    try:
        applied = migrations_runner.apply(conn=conn)
    except migrations_runner.MigrationError as err:
        raise click.ClickException(str(err)) from err

    for migration in applied:
        click.echo(f"Applied {migration.path.name}")
