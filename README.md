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

The `flask` commands below do not read `start_service.sh`, so they do not
inherit its `INSTANCE_PATH`. Point them at the same directory the service uses,
or they act on a different database and still report success:

```sh
export INSTANCE_PATH="$HOME/instance"
```

Without it, a checkout that still has its `.git` directory falls back to
`instance_dev` next to the code, which is not the deployed database.

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
nothing the second time. Add `--dry-run` to list what it would apply and change
nothing.

Check `INSTANCE_PATH` is set first. `migrate` reports success on whatever
database it is pointed at, including an empty one it just created.

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
delete and the unlink leaves behind. Those files are safe to delete; uploads
still in flight are not listed.

If a file cannot be deleted, the command says so and exits non-zero. Its row is
already gone by then, so a later run will not retry it: remove those files by
hand.

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

## Per-run test statistics

Separate again from `/results` and `/history`, and for a specific reason: JUnit
is a standardised format and cannot carry the metadata these counts need
(software versions, CLI command coverage) without breaking its schema.
`/results/.../import` parses JUnit and stores verdicts, `/history` stores raw
JUnit and parses nothing, and this endpoint stores numbers the client already
computed. **The service never parses a test report here.**

The client does the counting because the source is allure, not JUnit.
`cardano-node-tests` runs pytest twice into one results directory (a
`--skipall` registration pass, then the real run), so result files have to be
grouped by allure `historyId` before anything is counted. That logic lives in
`scripts/count_test_results.py` in that repo.

A run is identified by five fields together: `project`, `testrun_name`,
`run_id`, `step` and `origin`. All five are needed - `run_id` repeats across
projects, the upgrade path reports three `step`s under one run, and `origin`
(`ci` or `local`) keeps a developer's local run out of the CI numbers.
Re-uploading the same five replaces the row rather than adding one, so the
uploader is safe to retry.

### Authentication

The same HTTP basic auth as every other route. The "token" is the password
half of the credentials pair, so no separate token store exists:

```sh
flask --app testing_results_cache.app:create_app add-user --username stats
```

Put `stats:<token>` in CI secrets, and export it locally for a local test run.
Revoke by deleting the row from the `users` table.

### Upload the counts for one run

The whole identity lives in the body, not the URL, so the two cannot disagree.

```sh
curl -X PUT --fail-with-body -u stats:token http://localhost:5000/stats \
  -H 'Content-Type: application/json' -d '{
  "schema": 1,
  "project": "cardano-node-tests",
  "testrun_name": "node-10.5.0",
  "run_id": "1234",
  "step": "main",
  "origin": "ci",
  "timestamp": "2026-05-31T00:39:35+01:00",
  "duration": 4527.316,
  "exit_code": 0,
  "filtered": false,
  "counts": {"total": 2145, "passed": 1892, "failed": 0, "broken": 0, "skipped": 253},
  "quality": {"never_run": 0, "no_history_id": 0, "read_errors": 0},
  "versions": {"cardano_node": "10.5.0"},
  "commands": {"count": 213130, "coverage_pct": 31.01}
}'
```

Notes on the fields:

- `counts` holds allure statuses. `broken` has no JUnit equivalent. The four
  buckets may sum to less than `total`; the remainder is reported back as
  `other` and needs no column.
- `quality.never_run` counts tests the registration pass registered that never
  got a real result, which means the run was interrupted and every count is a
  floor rather than a total. It is a **subset of `skipped`**, because a
  registration result carries status `skipped`, and an upload where it exceeds
  `skipped` is refused. `quality` is **required**: defaulting `never_run` to
  zero would silently claim an interrupted run was complete, which is the one
  thing the field exists to prevent.
- `step` defaults to `main` when absent. The upgrade path sends `step1`,
  `step2`, `step3`.
- `timestamp` is ISO-8601. A value without an offset is read as UTC. Years
  before 1000 are refused, because the stored format cannot read them back.
- Each count is capped at 10,000,000, which is about 4600x the largest real
  run. The cap is not about one upload: the counts are summed across rows, and
  a per-field bound alone would let two individually legal rows overflow
  `SUM()` and break every read permanently.
- No number anywhere in the document may be non-finite. `NaN`, `Infinity` and
  `-Infinity` are refused, and so is a literal like `1e400` that overflows to
  infinity. Python's JSON parser accepts all of them; the format does not.
- `duration` is capped at 1,000,000,000 seconds, about 31 years. Like the
  count cap, this is not about one upload: durations are summed across rows,
  and a float sum reaches infinity silently rather than failing, which would
  leave the totals unreadable.
- Anything else in the document is stored and handed back only by the
  database, not by the read routes. That is where `versions` and `commands`
  live until a query needs them as columns. The stored document is
  re-serialised canonically as UTF-8, so the values all survive but key order
  and whitespace do not, and the stored form is never larger than the request
  that carried it.
- The body is capped at 64 kB, well under the service-wide 16 MB limit.

### Read the totals

```sh
curl -u stats:token 'http://localhost:5000/stats?project=cardano-node-tests&days=30'
```

`project` and `days` are both optional. `days` must be between 1 and 366.

The response sums `runs`, `cases`, `passed`, `failed`, `broken`, `skipped`,
`never_run` and `duration`, and reports `other` the same way the per-run
listing does. A non-zero `never_run` means interrupted runs were
included, so the other totals are a floor rather than a total.

### List individual runs, newest first

```sh
curl -u stats:token 'http://localhost:5000/stats/runs?project=cardano-node-tests&limit=20'
```

The listing is summary only. It never returns test names, failure messages or
the stored document, so it stays safe to build a summary page on.

`limit` must be between 1 and 1000. A larger value is refused rather than
quietly capped: there is no cursor on this route, so a truncated listing would
otherwise look complete.

## Run tests

```sh
make test
```

## Run linters

```sh
make lint
```

Runs the same hooks CI runs. `make init-lint` installs them as a git pre-commit
hook if you want them on every commit.
