# Testing results cache for cardano-node-tests

Cache testing results from test runs so failures can be re-tested without re-running whole test run.

## Install

Create `tcache` user and group

```sh
sudo groupadd tcache
sudo useradd --gid tcache --create-home --comment "testing cache API" tcache
```

Switch to `tcache` user

```sh
sudo -i -u tcache
```

Clone the repo

```sh
git clone https://github.com/mkoura/testing-results-cache.git
cd testing-results-cache
```

Install [uv](https://docs.astral.sh/uv/), then create the virtual environment and install
the package with its dependencies

```sh
make install
```

This creates a `.venv` virtual environment. Activate it with

```sh
. .venv/bin/activate
```

## Setup

Initialize database

```sh
flask --app testing_results_cache.app:create_app init-db
```

**Note for existing deployments:** `init-db` drops and recreates all tables.
To add the `history` and `sync_results` tables without losing data, run the
following instead:

```sh
sqlite3 instance/testing_results_cache.db <<'EOF'
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
EOF
```

Add user(s)

```sh
$ flask --app testing_results_cache.app:create_app add-user --username team
Password:
Repeat for confirmation:
Added user team.
```

## Run the service

Copy & edit `start_service.sh`

```sh
cp examples/start_service.sh .
vim start_service.sh
```

Create systemd unit file and start the service

```sh
sudo cp examples/tcache.service /etc/systemd/system/tcache.service
sudo vim /etc/systemd/system/tcache.service
sudo systemctl daemon-reload
sudo systemctl enable --now tcache
```

Setup a proxy HTTP server (e.g. [Caddy](https://caddyserver.com/)) and point it to the service.

For Caddy, the `/etc/caddy/Caddyfile` would look like

```text
tcache-3-74-115-22.nip.io {
        reverse_proxy /results/* 127.0.0.1:8000
        reverse_proxy /history/* 127.0.0.1:8000
        reverse_proxy /sync-results/* 127.0.0.1:8000
}
```

## Run the service for local development

Make sure to activate python virtual env and finish setup steps first.

```sh
flask --app 'testing_results_cache.app:create_app()' --debug run
```

## Queries

Submit results:

```sh
curl -X PUT --fail-with-body -u username:password http://localhost:5000/results/testrun1/1/import -F "junitxml=@/home/user/path/to/junit.xml"
```

Get passed tests in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/passed
```

Get passed tests in given testrun formatted as pytest nodeid:

```sh
curl -u username:password http://localhost:5000/results/testrun1/pypassed
```

Get tests that need re-run in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/rerun
```

Get tests formatted as pytest nodeid that need re-run in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/pyrerun
```

## Nightly run history

Separate from `/results`: stores raw JUnit XML per testrun+job without parsing
it, so failure history can be inspected later (e.g. by an AI failure-analysis
step). One upload per testrun+job. Note that history files are never deleted
automatically; prune old files and `history` table rows manually if disk space
becomes a concern. A hard crash mid-upload can also leave stale `.upload-*.tmp`
files under the history folder; they are safe to delete.

Upload the JUnit XML for a nightly job:

```sh
curl -X PUT --fail-with-body -u username:password http://localhost:5000/history/testrun1/job1 -F "junitxml=@/home/user/path/to/junit.xml"
```

List recorded jobs for a testrun within the last N days (default 5):

```sh
curl -u username:password 'http://localhost:5000/history/testrun1?days=7'
```

Download the stored JUnit XML for a job:

```sh
curl -u username:password http://localhost:5000/history/testrun1/job1/xml
```

## Sync-test results cache

Separate again from `/results` and `/history`: caches a zip of cardano-sync-tests
results (JSON metrics plus rendered graphs) per cardano-node version, with no
parsing. Unlike `/history`, there is only ever one entry per version - a new
upload for a version replaces whatever was stored for it before, rather than
being rejected as a duplicate. Entries for older versions are never pruned
automatically; remove old rows/files manually if disk space becomes a
concern. A hard crash mid-upload can also leave stale `.upload-*.tmp` files
under the sync-results folder; they are safe to delete.

Upload the results zip for a version:

```sh
curl -X PUT --fail-with-body -u username:password http://localhost:5000/sync-results/11.1.0 -F "syncresults=@/home/user/path/to/sync_results.zip"
```

List every version that currently has a stored entry:

```sh
curl -u username:password http://localhost:5000/sync-results
```

Download the stored zip for a version:

```sh
curl -u username:password http://localhost:5000/sync-results/11.1.0/zip
```

## Run tests

```sh
make test
```
