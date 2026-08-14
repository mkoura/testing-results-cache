DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS testrun;
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS history;

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE testrun (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE results (
    id INTEGER PRIMARY KEY,
    test_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    testrun_id INTEGER NOT NULL,
    user_id INTEGER
);

-- Raw JUnit XML dumps for nightly runs. Separate from results/testrun -
-- no verdict is parsed out, and any logged-in user may read any row.
-- user_id is kept only as an upload record, not for access control.
CREATE TABLE history (
    id INTEGER PRIMARY KEY,
    testrun_name TEXT NOT NULL,
    job_id TEXT NOT NULL,
    user_id INTEGER,
    -- Deliberately TEXT, not TIMESTAMP: sqlite3's default (deprecated since
    -- Python 3.12) PARSE_DECLTYPES converter crashes reading back a value
    -- with a zero-microsecond, UTC-offset timestamp, and silently drops the
    -- offset when microseconds are nonzero. This column is formatted/parsed
    -- by history_cache.py itself.
    timestamp TEXT NOT NULL,
    UNIQUE (testrun_name, job_id)
);
