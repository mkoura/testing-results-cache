-- Per-run test counts, computed by the client from its allure results.

CREATE TABLE IF NOT EXISTS testrun_stats (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    testrun_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    origin TEXT NOT NULL,
    user_id INTEGER,
    -- TEXT, not TIMESTAMP: see the comment on history.timestamp in schema.sql.
    timestamp TEXT NOT NULL,
    cases INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    broken INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    -- A subset of `skipped`: registered tests that never got a real result.
    never_run INTEGER NOT NULL,
    -- Wall clock span of the run in seconds, not the sum of test durations.
    duration REAL NOT NULL,
    exit_code INTEGER NOT NULL,
    filtered INTEGER NOT NULL,
    -- The uploaded JSON, including the fields that have no column.
    payload TEXT NOT NULL,
    UNIQUE (project, testrun_name, run_id, step, origin)
);

CREATE INDEX IF NOT EXISTS idx_testrun_stats_ts ON testrun_stats(timestamp);
CREATE INDEX IF NOT EXISTS idx_testrun_stats_proj_ts ON testrun_stats(project, timestamp);
CREATE INDEX IF NOT EXISTS idx_testrun_stats_name_ts ON testrun_stats(testrun_name, timestamp);
