-- Baseline. Everything here already exists on any deployment created from
-- schema.sql, so every statement is written to be safe to re-run. Applying
-- this to a live database records the starting point without changing it.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS testrun (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    test_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    testrun_id INTEGER NOT NULL,
    user_id INTEGER
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    testrun_name TEXT NOT NULL,
    job_id TEXT NOT NULL,
    user_id INTEGER,
    timestamp TEXT NOT NULL,
    UNIQUE (testrun_name, job_id)
);

CREATE TABLE IF NOT EXISTS sync_results (
    version TEXT PRIMARY KEY,
    user_id INTEGER,
    timestamp TEXT NOT NULL
);
