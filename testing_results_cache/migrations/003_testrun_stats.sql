-- Per-run counts, so "how many test runs did we do, and how did they go" can
-- be answered at all. Nothing in the existing schema can: `results` has no
-- per-run identity, `testrun` has one row per testrun *name*, and `history`
-- records only that a JUnit dump landed, never what was in it.
--
-- The counts come from allure, not JUnit. `cardano-node-tests` runs pytest
-- twice into one results dir (a `--skipall` registration pass, then the real
-- run), so the client groups result files by allure `historyId` before
-- counting. Allure's statuses are passed/failed/broken/skipped, and `broken`
-- has no JUnit equivalent, which is why it gets its own column here.
--
-- The server never parses a test report. The client computes the counts and
-- uploads a small JSON document, which is stored verbatim in `payload`.
--
-- Written IF NOT EXISTS like every other migration, so re-applying it to a
-- database created from schema.sql changes nothing.

CREATE TABLE IF NOT EXISTS testrun_stats (
    id INTEGER PRIMARY KEY,

    -- Run identity. All five are needed: `run_id` alone repeats across
    -- projects, the upgrade path reports three `step`s under one run, and
    -- `origin` keeps a developer's local run out of the CI numbers.
    project TEXT NOT NULL,
    testrun_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    origin TEXT NOT NULL,

    user_id INTEGER,

    -- Deliberately TEXT, not TIMESTAMP: sqlite3's default PARSE_DECLTYPES
    -- converter is deprecated since Python 3.12 and mishandles UTC offsets.
    -- Same reasoning and same format as history.timestamp, formatted and
    -- parsed by stats_cache.py itself.
    timestamp TEXT NOT NULL,

    -- Counts. `cases` is the total after grouping. The four buckets are
    -- allure statuses; anything else the client saw is the remainder
    -- (`cases` minus the four), so it needs no column of its own.
    cases INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    broken INTEGER NOT NULL,
    skipped INTEGER NOT NULL,

    -- Tests the registration pass registered that never got a real result,
    -- so the run was interrupted and every count above is a floor, not a
    -- total. A subset of `skipped`, never a sibling bucket - the
    -- registration files carry status "skipped".
    never_run INTEGER NOT NULL,

    -- Wall-clock span of the run, in seconds. Not the JUnit `time`
    -- attribute, and not the sum of per-test durations: tests run in
    -- parallel under xdist, so that sum is far larger than the elapsed time.
    duration REAL NOT NULL,

    -- pytest's own exit code, and whether the run was restricted to a subset
    -- of tests. Both exist so an aggregate can exclude a run that is not
    -- comparable with a full one.
    exit_code INTEGER NOT NULL,
    filtered INTEGER NOT NULL,

    -- The uploaded JSON, stored verbatim. About 600 bytes per run. It holds
    -- the fields no column exists for yet (versions, env, CLI coverage) and
    -- the client-side warning counters, so a later column can be backfilled
    -- instead of losing every run recorded before it.
    payload TEXT NOT NULL,

    UNIQUE (project, testrun_name, run_id, step, origin)
);

CREATE INDEX IF NOT EXISTS idx_testrun_stats_ts ON testrun_stats(timestamp);
CREATE INDEX IF NOT EXISTS idx_testrun_stats_proj_ts ON testrun_stats(project, timestamp);
CREATE INDEX IF NOT EXISTS idx_testrun_stats_name_ts ON testrun_stats(testrun_name, timestamp);
