DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS testrun;
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS history;
DROP TABLE IF EXISTS sync_results;
DROP TABLE IF EXISTS testrun_stats;

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

-- Cached sync-test-results zips (JSON metrics + rendered graphs), keyed by
-- cardano-node version only. Unlike history, this upserts: a new upload
-- for a version replaces whatever was stored for it before, so there is
-- only ever one row per version.
CREATE TABLE sync_results (
    version TEXT PRIMARY KEY,
    user_id INTEGER,
    -- Deliberately TEXT, not TIMESTAMP: see the comment on history.timestamp
    -- above. Same reasoning, same format, formatted/parsed by
    -- sync_results_cache.py itself.
    timestamp TEXT NOT NULL
);

-- Per-run counts, uploaded as a small JSON document by the client. The
-- server never parses a test report. Kept in step with
-- migrations/003_testrun_stats.sql - see that file for why each column
-- exists, and for why the counts come from allure rather than JUnit.
CREATE TABLE testrun_stats (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    testrun_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    origin TEXT NOT NULL,
    user_id INTEGER,
    -- Deliberately TEXT, not TIMESTAMP: see the comment on history.timestamp
    -- above. Same reasoning, same format, formatted/parsed by stats_cache.py.
    timestamp TEXT NOT NULL,
    cases INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    broken INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    never_run INTEGER NOT NULL,
    duration REAL NOT NULL,
    exit_code INTEGER NOT NULL,
    filtered INTEGER NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (project, testrun_name, run_id, step, origin)
);

-- Kept in step with migrations/002_indexes.sql, so a database created from
-- this file matches one that was migrated. `results` grows with every import
-- and is scanned by the four query routes.
CREATE INDEX idx_results_testrun_user ON results(testrun_id, user_id);
CREATE INDEX idx_testrun_name ON testrun(name);
CREATE INDEX idx_users_name ON users(name);

-- Kept in step with migrations/003_testrun_stats.sql.
CREATE INDEX idx_testrun_stats_ts ON testrun_stats(timestamp);
CREATE INDEX idx_testrun_stats_proj_ts ON testrun_stats(project, timestamp);
CREATE INDEX idx_testrun_stats_name_ts ON testrun_stats(testrun_name, timestamp);
