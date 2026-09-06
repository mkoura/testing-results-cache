import pathlib
import sqlite3

import click
import flask
from werkzeug import security

from testing_results_cache import migrations_runner
from testing_results_cache import retention
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

    # schema.sql already creates everything the migrations would, so record
    # them as applied. Without this a brand-new database reports version 0 and
    # `migrate` offers to re-apply the baseline. That is harmless only while
    # every migration is written IF NOT EXISTS; the first ALTER TABLE would
    # fail on every fresh install.
    migrations_runner.stamp_as_current(db)
    db.commit()


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
    except (migrations_runner.MigrationError, sqlite3.Error) as err:
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
    except (migrations_runner.MigrationError, sqlite3.Error) as err:
        raise click.ClickException(str(err)) from err

    for migration in applied:
        click.echo(f"Applied {migration.path.name}")


@click.command("prune-history")
@click.option("--days", type=int, required=True, help="Keep entries newer than this many days.")
@click.option("--dry-run", is_flag=True, help="List what would go, delete nothing.")
def prune_history_command(days: int, dry_run: bool) -> None:
    """Remove stored history older than the given number of days."""
    history_folder = pathlib.Path(flask.current_app.config["HISTORY_FOLDER"])
    conn = get_db()

    try:
        removed, stranded = retention.prune(
            conn=conn, history_folder=history_folder, days=days, dry_run=dry_run
        )
    except (ValueError, sqlite3.Error) as err:
        raise click.ClickException(str(err)) from err

    verb = "Would remove" if dry_run else "Removed"
    for entry in removed:
        click.echo(f"{verb} {entry.testrun_name}/{entry.job_id}")
    click.echo(f"{verb} {len(removed)} entries older than {days} days.")

    orphans = retention.find_orphans(conn=conn, history_folder=history_folder)
    if orphans:
        click.echo(f"{len(orphans)} stored files have no matching row:")
        for path in orphans:
            click.echo(f"  {path}")

    if stranded:
        # A non-zero exit, so a cron job that could not delete a single file
        # does not report success. The rows are already gone, so rerunning
        # will not retry these: the files have to be removed by hand.
        msg = (
            f"{len(stranded)} files could not be removed and are now orphans. "
            "Their rows are gone, so a later run will not retry them."
        )
        raise click.ClickException(msg)
