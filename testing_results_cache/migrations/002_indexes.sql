-- `results` grows with every import and had no index at all, so `/passed`,
-- `/pypassed`, `/rerun` and `/pyrerun` scanned the whole table on every call.
-- `history` and `sync_results` already have UNIQUE constraints that serve
-- their lookups, so they need nothing here.

CREATE INDEX IF NOT EXISTS idx_results_testrun_user ON results(testrun_id, user_id);
CREATE INDEX IF NOT EXISTS idx_testrun_name ON testrun(name);
CREATE INDEX IF NOT EXISTS idx_users_name ON users(name);
