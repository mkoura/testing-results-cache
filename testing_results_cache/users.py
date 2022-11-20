import sqlite3
from typing import Tuple


def add_user(conn: sqlite3.Connection, user_name: str, password_hash: str) -> int:
    """Add user to database."""
    cur = conn.cursor()
    cur.execute("INSERT INTO users(name, password_hash) VALUES (?,?)", (user_name, password_hash))
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def get_user(conn: sqlite3.Connection, user_name: str) -> Tuple[int, str]:
    """Get user from database."""
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE name = ?", (user_name,))
    response = cur.fetchone()
    if response is None:
        return -1, ""

    (user_id, password_hash) = response
    return user_id or -1, password_hash or ""
