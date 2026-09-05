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

`init-db` creates the schema from scratch and drops any existing tables, so
run it only on a new deployment.

Bring an existing database up to the current schema instead with

```sh
flask --app testing_results_cache.app:create_app migrate
```

`migrate` applies only the migrations the database has not seen yet, in one
transaction each, and records each one it applies. Running it twice does
nothing the second time. Add `--dry-run` to list what it would apply.

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
        reverse_proxy /sync-results 127.0.0.1:8000
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
step). One upload per testrun+job. History is not pruned on a schedule; see
[Pruning old history](#pruning-old-history) below. A hard crash mid-upload can
leave stale `.upload-*.tmp` files under the history folder; they are safe to
delete.

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

## Pruning old history

Remove history entries older than a number of days. This deletes both the
`history` row and its stored XML.

```sh
flask --app testing_results_cache.app:create_app prune-history --days 90
```

Use `--dry-run` first to list what would go. The command also reports any
stored file that no `history` row mentions, which is what a crash between the
delete and the unlink leaves behind. Those files are safe to delete.

Nothing runs this for you. Put it in a cron job or a systemd timer if the
history folder needs to stay bounded.

## Sync-test results cache

Separate again from `/results` and `/history`: caches a zip of cardano-sync-tests
results (JSON metrics plus rendered graphs) per cardano-node version, with no
parsing. Unlike `/history`, there is only ever one entry per version - a new
upload for a version replaces whatever was stored for it before, rather than
being rejected as a duplicate. Entries for older versions are never pruned
automatically; remove old rows/files manually if disk space becomes a
concern. A hard crash mid-upload can also leave stale `.upload-*.tmp` files
under the sync-results folder; they are safe to delete. A hard crash can also
leave a `<version>.zip.prev` file: this endpoint moves an existing zip aside
to that name before replacing it, and only deletes it once the replacement is
confirmed stored, so a crash at exactly that point can leave it behind. Check
that `<version>.zip` itself is present and correct before deleting the
`.prev` file - if `<version>.zip` is missing or corrupt, `.prev` is the last
good copy.

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
