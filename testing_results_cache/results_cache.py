"""Helper functions for handling data in sqlite3."""
import sqlite3
from typing import List

from testing_results_cache import common


def get_testrun_id(conn: sqlite3.Connection, testrun_name: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM testrun WHERE name = ?", (testrun_name,))
    response = cur.fetchone()
    if response is None:
        return -1

    (testrun_id,) = response
    return testrun_id or -1


def save_testrun(
    conn: sqlite3.Connection, testrun_name: str, user_id: int, testsuite_data: common.TestsuiteData
) -> int:
    """Save testrun data to database."""
    cur = conn.cursor()
    testrun_id = get_testrun_id(conn=conn, testrun_name=testrun_name)
    if testrun_id == -1:
        cur.execute("INSERT INTO testrun(name) VALUES (?)", (testrun_name,))
        testrun_id = get_testrun_id(conn=conn, testrun_name=testrun_name)

    to_db = [(r.testid, r.verdict, testrun_id, user_id) for r in testsuite_data.tests_verdicts]

    cur.executemany(
        "INSERT INTO results(test_name, verdict, testrun_id, user_id) VALUES (?,?,?,?)", to_db
    )
    conn.commit()
    return testrun_id


def load_testrun(
    conn: sqlite3.Connection, testrun_name: str, user_id: int
) -> List[common.TestVerdict]:
    """Get testrun data from database."""
    cur = conn.cursor()
    testrun_id = get_testrun_id(conn=conn, testrun_name=testrun_name)
    if testrun_id == -1:
        return []

    cur.execute(
        "SELECT test_name, verdict FROM results WHERE testrun_id = ? AND user_id = ?",
        (testrun_id, user_id),
    )
    response = cur.fetchall()
    if response is None:
        return []

    tests_verdicts = [
        common.TestVerdict(testid=test_name, verdict=verdict) for test_name, verdict in response
    ]
    return tests_verdicts
