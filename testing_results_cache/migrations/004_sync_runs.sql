-- One row per sync run instead of one per cardano-node version. A version is
-- run on more than one network and can be run more than once, and the sync
-- analyzer lets a visitor pick any stored run, so every run must stay.
--
-- Rows already stored carry over as mainnet runs started when they were
-- uploaded. Their zips are named `<version>.zip` on disk and must be renamed
-- to `<id>.zip` by hand; see "Sync-test results cache" in the README.

ALTER TABLE sync_results RENAME TO sync_results_old;

CREATE TABLE IF NOT EXISTS sync_results (
    id INTEGER PRIMARY KEY,
    environment TEXT NOT NULL,
    version TEXT NOT NULL,
    -- When the sync started, as the uploader reports it.
    -- TEXT, not TIMESTAMP: see the comment on history.timestamp in schema.sql.
    started TEXT NOT NULL,
    user_id INTEGER,
    timestamp TEXT NOT NULL,
    -- The numbers the sync analyzer shows, read from the zip at upload.
    -- NULL when the zip holds no readable node results file.
    facts TEXT,
    UNIQUE (environment, version, started)
);

INSERT INTO sync_results (environment, version, started, user_id, timestamp)
    SELECT 'mainnet', version, timestamp, user_id, timestamp FROM sync_results_old;

DROP TABLE sync_results_old;

CREATE INDEX IF NOT EXISTS idx_sync_results_started ON sync_results(started);
