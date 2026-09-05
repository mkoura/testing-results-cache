# Scalability plan

A plan to move this service from "it works" to "it can take another five
endpoints without repeating itself". Written for review, not for merging as is.
None of the ten items below has been started.

This branch is off `master`, but the plan is measured against
`harden-sync-results-zip-checks`, the tip of the stack in flight. So it
describes `/sync-results`, which lands with #17 and #18 and is not on `master`
yet. The `/sync-results` hardening in those two PRs is done and is not part of
this plan.

Item 1 is done as well, in #19. It is kept below with its evidence, because it
is the worked example of the whole argument. The guard existed in two
blueprints and not in the third. The third is the one CI calls.

Every number below can be reproduced. The commands are given so a reviewer can
check the claim rather than take it on trust. If a number does not reproduce,
the item it supports is wrong and should be dropped.

## Why now

Three API modules exist (`results`, `history`, `sync_results`). The third was
added by copying the second. Two defects were fixed in `history_api.py` on
2026-09-01. Both were rediscovered in `sync_results_api.py` on 2026-09-05. The
copy predates the fix, and nothing links the two files:

- `OSError` classified as a client error, so a disk fault answers 400
- rejections that write nothing to the application log

That is the pattern this plan exists to stop. A fourth endpoint copied from the
third inherits the same defects again.

---

## This has been done before, in `cardano-sync-tests`

That repo was in worse shape than this one and was cleaned up over about ten
weeks. The order it happened in is the strongest evidence in this plan, because
it worked. Check it yourself:

    gh pr list --repo IntersectMBO/cardano-sync-tests --state merged --limit 30 \
      --json number,title,mergedAt,additions,deletions

    #139 [2026-06-23] +8203/-10408  Pytest sync test
    #144 [2026-07-10]   +36/-323    Removal of buildkite and obsolete documents
    #156 [2026-07-23]  +347/-1743   Fix CI, remove dead code, harden the harness
    #159 [2026-07-23]   +13/-230    Remove Cabal build system support
    #161 [2026-08-03]  +401/-0      Add tests for log parsing and graph generation
    #162 [2026-07-31]   +32/-0      Add dedicated unit tests workflow
    #165 [2026-07-30]    +6/-3      Split framework tests into own directory
    #170 [2026-09-03]  +537/-0      Cover sync-progress helpers and heartbeat.sh
    #173 [2026-09-04]   +61/-68     Require Python 3.12+, run CI and nix on 3.14
    #174 [2026-09-04]   +10/-9      Move dev deps into pyproject dependency group
    #175 [2026-09-04]  +142/-20     Add Makefile and use it in CI and the devshell
    #176 [2026-09-04]   +14/-12     Update pre-commit hooks

Four things stand out, and this plan follows all four.

**Deletion came before addition.** The three cleanup PRs removed 2,296 lines
and added 396. Nothing was built on code that was about to be deleted.

**Tests came before tooling.** #161 covered "previously the most logic-dense
modules in the repo with zero coverage". Only then did the Python floor, the
dependency groups, the Makefile and the hooks get touched, in September.

**A test that does not run in CI does not exist.** #162 was opened because the unmarked
tests "never ran in CI". Its body lists exactly which workflow excluded them,
and why. Adding tests and adding the workflow that runs them is one job.

**Writing tests finds bugs, and that is the point.** #161 says: "Found and
documented one existing quirk while writing these". Expect the same here, and
expect it to slow item 2 down.

### What this repo already has

Three items from that sequence are done, so do not redo them:

- a `Makefile`, the equivalent of #175
- `[dependency-groups]` in `pyproject.toml`, the equivalent of #174
- `tests.yaml` running pytest on every PR, merged 2026-09-03 as PR #15, the
  equivalent of #162

That is why item 2 below is worth doing now. The workflow to run new tests
already exists.

### Where this plan goes beyond the precedent

Two items have no `cardano-sync-tests` analogue, because that repo has no
database and no HTTP API: the migration mechanism and the indexes. They are
argued on their own evidence.

One item is taken from `cardano-node-tests` rather than `cardano-sync-tests`.
`AGENTS.md` exists in the former and in neither of the
other two. Treat it as the weakest-supported item here.

---

## 1. Close the path traversal in `results_api` (DONE, PR #19)

**Claim.** `results_api.py` performs no path validation, and builds file paths
directly from user-controlled URL segments:

    upload_filepath = upload_folder / testrun_name / job_id / f"upload-{rand}.xml"

An encoded `..` is accepted, and the file lands outside `UPLOAD_FOLDER`:

    results_api       PUT /results/%2e%2e/%2e%2e/import  -> 200, escaped 2 levels
    history_api       PUT /history/%2e%2e/1              -> 400 Invalid path segment
    sync_results_api  PUT /sync-results/%2e%2e           -> 400 Invalid path segment

One `..` per segment, and there are two segments, so the write lands two
directories above the upload folder. A bare `..` in the URL is normalised away
by the router and gives a 404; the percent-encoded form is not.

**Why this is first.** The two newer endpoints reject this. The oldest one, and
the only one `cardano-node-tests` actually calls today, does not. It is the same copy-paste story as the
rest of this plan. Here the missing piece is a security control rather than a
log line.

Scope of the exposure. It needs a valid login, the written file is always
`.xml` with a checksum name, and the content must parse as a JUnit report. So
it is not arbitrary file write in the general sense. It does let any
authenticated user create or replace `.xml` files in directories the service
account can reach, outside the folder meant to contain them.

**How to fix.** Use the same guard the other two use. `_reject_invalid_segments`
already exists in `history_api.py`, and item 3 moves it to `common.py` anyway.
So take a temporary copy now, and let item 3 deduplicate. Do not wait for the
extraction.

**How to test.** Assert a 400 for `%2e%2e`, `.`, `..`, a name with a slash, a
name over the length limit, and a name that is empty after validation. Then
assert that no file exists outside `UPLOAD_FOLDER` after those calls. Test the traversal in both `testrun_name`
and `job_id`, because both are interpolated.

**Impact.** Any existing testrun whose name contains a character outside
`[A-Za-z0-9_.-]` will start being rejected. Check the live database first:

    SELECT DISTINCT name FROM testrun;

`cardano-node-tests` already strips its testrun names to that character set
before calling, so the callers we know about are safe. An unknown manual caller
may not be.

**Risk.** Low to apply, and it is the one item here that should not wait for
the rest of the plan.

**Outcome.** Done in #19, together with item 3. Implementing it exposed three
more faults, which is the argument for item 2. `_pytestify` raised on a test id
holding `::`, wedging `/pypassed` for a testrun permanently. The password check
had no test at all, so deleting it left the suite green. And the extraction
itself briefly rebound a shared constant. None of those were visible without
writing the tests.

---

## 2. Test the code that is already live

**Claim.** The oldest and most used module has the least coverage.

    uv run --with pytest-cov pytest tests/ -q --cov=testing_results_cache --cov-report=term-missing

    results_api.py       142 stmts, 93 missed   35%
    results_cache.py      32 stmts, 26 missed   19%
    junittools.py         57 stmts, 46 missed   19%
    history_api.py       101 stmts,  5 missed   95%
    sync_results_api.py  169 stmts, 19 missed   89%
    TOTAL                                       70%

**Why this is first.** `results_api` has served the `cardano-node-tests` retry
flow since 2022. Five endpoints have no test at all:

    /results/<testrun_name>/<job_id>/import
    /results/<testrun_name>/passed
    /results/<testrun_name>/pypassed
    /results/<testrun_name>/rerun
    /results/<testrun_name>/pyrerun

`junittools.py` is worse than its number suggests, because every upload now
passes through it. It also holds two known landmines. `_get_xml_root` reads with
`read_text()` and no encoding, so it decodes with the process locale.
`_sanitize_xml` exists to strip the raw escape character pytest emits. Neither
is pinned by a test.

**How to test.** Add `tests/test_results.py` mirroring the shape of
`tests/test_history.py`, and `tests/test_junittools.py` as direct unit tests.
Cover, per endpoint: happy path, wrong extension, no file part, bad path
segment, unknown testrun, and one re-import of the same job. For `junittools`,
cover a report with the escape character, a report with non-ASCII characters,
a truncated report, and a report declaring a non-UTF-8 encoding.

**Impact.** No behaviour change. It is the prerequisite for every other item,
because none of them can be done safely against untested code.

**Risk.** Low, but expect the new tests to fail on first write. The
`iso-8859-1` and UTF-16 cases already fail today, and the locale dependency is
real. Decide whether to fix `_get_xml_root` or to pin current behaviour and fix
it separately. Do not silently "fix" a test to match a bug.

**Precedent.** `cardano-sync-tests` #161 and #170, both purely additive
(+401/-0 and +537/-0). Neither changed behaviour.

**Effort.** Two days. This is the largest item and the least glamorous.

---

## 3. Extract the duplicated helpers into `common.py`

**Claim.** Four names are defined twice, comments included.

    grep -l "_abort_json\|_valid_path_segment\|MAX_PATH_SEGMENT_LENGTH\|_SAFE_SEGMENT_RE" testing_results_cache/*.py

    history_api.py
    sync_results_api.py

**Why.** This is the mechanism behind the repeated defects described above. A
fix applied to one copy does not reach the other, and nothing warns you. The
duplication also grows: `sync_results_api.py` copied `history_api.py`, which
copied the shape of `results_api.py`.

**What to move.** `_abort_json`, `_valid_path_segment`, `_SAFE_SEGMENT_RE`,
`MAX_PATH_SEGMENT_LENGTH`, `_reject_invalid_segments`. `_abort_json` takes an
optional `headers` argument in `sync_results_api` only; keep that parameter in
the shared version and let the other caller ignore it.

**Do not move** the upload handlers themselves. They differ in a way that
matters: `/history` refuses to overwrite, `/sync-results` replaces. Merging
those would produce a conditional that is harder to read than two functions.

**How to test.** The existing suite is the test. Move the helpers, change
nothing else, and confirm the same tests pass with the same counts. Then, as a
mutation check, break the shared `_valid_path_segment` and confirm tests in
both `test_history.py` and `test_sync_results.py` fail. If only one fails, the
extraction did not actually take.

**Impact.** No behaviour change. About 60 lines removed.

**Precedent.** `cardano-sync-tests` #156, +347/-1743. Removing duplicated and
dead code was the single largest cleanup in that repo, and it came before any
new feature work.

**Risk.** Low. The main risk is doing it before item 2, on modules with 19% and
35% coverage.

---

## 4. Add a migration mechanism

**Claim.** There is none.

    ls testing_results_cache/migrations     # does not exist
    grep -c "CREATE TABLE IF NOT EXISTS" README.md    # 2

Two schema changes so far, both shipped as SQL snippets pasted into the README
and run by hand against the live database.

**Why.** This has already caused a production failure mode nobody could
diagnose. During the `cardano-node-tests` #3624 work we could not tell, from CI
alone, whether the `history` table existed on the live host. The caller sees
only `500 Failed to store history XML`. The real cause, `no such table:
history`, stays in the server log, which the caller cannot read. That is still
true today: `history_api.py` has no missing-table message, because the change
that added one was on the closed PR #16 branch and was never merged.

`/sync-results` is better but not fixed. Its routes now answer JSON rather than
an HTML page, verified at `49b65c3`:

    GET /sync-results  ->  500  application/json  {"message": "Failed to read sync results"}

The status is right and the contract is kept, but the message still does not
say the table is missing, so a caller still cannot diagnose the one deployment
mistake it could act on.

Every new endpoint adds a table, so this cost repeats.

**What to build.** The smallest thing that works. A `migrations/` directory of
numbered `.sql` files. A `schema_version` table holding the last applied
number. A `flask --app testing_results_cache.app:create_app migrate` command
that applies anything newer, in order, inside one transaction. No external
dependency. Alembic is more than this service needs.

**How to test.** Build a database from the schema as it was before `/history`
existed. Seed it with rows in `results`, `testrun` and `users`. Run `migrate`.
Assert every seeded row survives and the new tables exist. Then run `migrate`
again and assert it is a no-op. That procedure already exists as a
shell script at `~/Desktop/work/tcache-local-test/test_migration.sh` and can be
adapted.

**Impact.** Deployment stops being a manual step that can be forgotten.

**Risk.** Medium, and it is the highest-risk item here. A migration runner that
gets it wrong can damage the live database. Mitigations. Apply inside one transaction. Refuse to run if `schema_version`
is ahead of the code. Make the first release a no-op that only records the
current version. Take a copy of the live sqlite file before the first real
run.

---

## 5. Add indexes

**Claim.** There are none.

    grep -c "CREATE INDEX" testing_results_cache/schema.sql    # 0

**Why.** Every query is a scan. These are the queries the service runs:

    SELECT test_name, verdict FROM results WHERE testrun_id = ? AND user_id = ?
    SELECT job_id, timestamp FROM history WHERE testrun_name = ? AND timestamp >= ?
    SELECT id FROM testrun WHERE name = ?
    SELECT id, password_hash FROM users WHERE name = ?

`results` is the one that matters. It grows with every import and has no index
at all, so the `pypassed` and `rerun` endpoints scan the whole table on every
call. `history` and `sync_results` already have a `UNIQUE` constraint that
serves their lookups.

**What to add.**

    CREATE INDEX idx_results_testrun_user ON results(testrun_id, user_id);
    CREATE INDEX idx_testrun_name ON testrun(name);
    CREATE INDEX idx_users_name ON users(name);

**How to test.** Do not assume an index helps. Measure it. Seed a copy of the
live database, or generate 100k `results` rows, and compare
`EXPLAIN QUERY PLAN` plus wall time before and after. Keep only the indexes
that change the plan from `SCAN` to `SEARCH`.

**Impact.** Faster reads. Slightly slower writes and a larger file, both
negligible at this size.

**Risk.** Low, and it needs item 4 first, or it becomes a third README snippet.

---

## 6. Raise the Python floor

**Claim.** `requires-python = ">=3.8"`, while `cardano-node-tests` is
`>=3.13`.

**Why.** The floor is not free. Two concrete costs already in the code:

- `sync_results_api.py` detects a locked database with
  `"database is locked" in str(exc)`, at line 319. That is the only such match
  left in the repo. `sqlite3.Error.sqlite_errorcode` is the correct check and
  needs 3.11.

Item 4 will add a second one, for the missing table, unless the floor is raised
first. Matching on English error text is fragile, and it is only there because
of the floor. Nothing in this repo needs 3.8. Python 3.8 reached end of life in
October 2024.

**What to change.** `requires-python = ">=3.12"`, matching
`cardano-sync-tests`. Then replace the substring match with a
`sqlite_errorcode` comparison, and add lower bounds to the three runtime
dependencies.

**How to test.** `uv lock` and run the suite. Add a test that provokes a real
`SQLITE_BUSY` and asserts the 503, and one that drops a table and asserts the
message names it.

**Precedent.** `cardano-sync-tests` #173 went from `>=3.10` to `>=3.12`, moved
CI to 3.14, and added version bounds to every runtime dependency in one change.
Match the floor, and take the bounds with it. This repo pins none of its three
dependencies (`lxml`, `flask`, `flask-httpauth`), so an upstream break arrives
unannounced.

**Impact.** Better error handling, and modern typing available.

**Risk.** Low, but confirm the deployment host has 3.12 before merging. The
service runs under gunicorn on Martin's server and that is the one thing this
plan cannot check.

---

## 7. Write an `AGENTS.md`

**Claim.** `cardano-node-tests` has `AGENTS.md`, `CLAUDE.md` and ten files in
`agent_docs/`. `cardano-sync-tests` and this repo have none.

**Why.** The conventions here are real and consistent, but they live only in
review comments. The same points get raised repeatedly. Keep comments to the
reason rather than the mechanism. Use `_abort_json` rather than raw `abort`.
Put `NoReturn` on helpers that always abort. Validate before mutating. Commit
after the rename. A contributor cannot know any of that without being told
each time.

**What to write.** Short. The house patterns above. The ordering rule for
uploads, and why it exists. The difference between the three endpoints'
overwrite semantics. How to run the tests.

**Impact.** Fewer review rounds.

**Precedent.** Weak. `cardano-node-tests` has one. `cardano-sync-tests` still
does not, and cleaned itself up anyway. Treat this as optional.

**Risk.** None, beyond the document going stale. Keep it under one page.

---

## 8. Split the tests

**Claim.** `tests/` holds `test_history.py` and `test_sync_results.py`. There
is no `test_results.py` and no unit tests for the cache modules or
`junittools`.

**What to do.** One test file per API module, plus unit tests for
`junittools`, `results_cache` and `history_cache`. Follow the existing
`conftest.py` fixtures, which are already good: function-scoped `app` on
`tmp_path`, so every test gets a fresh database.

**Precedent.** `cardano-sync-tests` #165, +6/-3. It moved five files into a
top-level `framework_tests/` directory, "matching cardano-node-tests". Its body
also notes that the move exposed a bug. One test had a hardcoded path depth.
Expect something similar.

Largely covered by item 2. Listed separately because the layout decision is
worth making deliberately rather than by accretion.

---

## 9. Fix the flaky mypy hook

**Claim.** `pre-commit run -a` fails intermittently with a mypy internal error,
and passes on a re-run. It reproduces on clean master, so it is not caused by
any current branch. `uv run mypy testing_results_cache tests` always succeeds.

**Cause.** `.pre-commit-config.yaml` declares the hook as
`language: system, types: [python]` with no `require_serial`, so pre-commit
splits the files across several concurrent mypy processes that share one
`.mypy_cache`.

**Fix.** Add `require_serial: true` to that hook.

**How to test.** Run `pre-commit run mypy -a` ten times and confirm no internal
error. Ten times, not once, because the failure is intermittent.

**Risk.** None. Slightly slower hook.

---

## 10. Decide a retention policy

**Claim.** Nothing ever removes a stored file or row, and no route can.

    grep -oE '@(history|sync_results)\.route\("[^"]*"' testing_results_cache/*_api.py
    # PUT/POST and GET only. No DELETE anywhere.

`GET /history/<name>?days=n` filters the query. It does not prune. Everything
older stays on disk and in the table.

**Why.** Growth is slow but unbounded, and only the person with shell access to
the host can reverse any of it. Measured on a real 392 KB nightly report. Three
nightlies failing every night is about 0.44 GB a year. One in three failing is
about 0.15 GB. `/sync-results` adds a zip per node version. Neither is alarming this
year. Both are permanent.

There is a second cost that is already real. Any test upload against the live
service, including one made while verifying a change, is permanent. That is why
testing `/history` against production was avoided during the
`cardano-node-tests` #3624 work.

**What to decide, and it is a decision rather than a task.** Three options,
in increasing order of work:

- A cron job on the host running `find <folder> -mtime +N -delete`, plus a
  matching `DELETE` on the table. No code, no API change, and it can be in place
  this week.
- A `flask prune --days N` command, so the rule lives with the code and is
  testable.
- A `DELETE` route. Most work, and it adds an endpoint that can remove data,
  which is a wider surface than the problem needs.

The first is probably right. The point of listing it is that no option is
currently chosen. The answer today is "keep everything forever", by default
rather than by decision.

**How to test.** Whichever option, seed entries with old timestamps and run
the prune. Assert that only the intended rows and files are gone, and that the
listing still resolves. Test the row and the file together. Removing one and not the other leaves a
row pointing at a missing file, or a file no listing mentions. Both are states
the upload path already works hard to avoid.

**Impact.** Bounded disk use, and test uploads stop being permanent.

**Risk.** Medium if done wrong, because it deletes data. Mitigate by pruning
files only when the matching row was deleted in the same transaction, and by
running it in report-only mode first.

---

## Sequencing

     1  close the path traversal    <- DONE, PR #19
     3  extract shared helpers      <- DONE, PR #19
     9  fix the mypy hook           <- DONE, PR #19
     2  test the live code          <- partly done in #19, see below
     4  migration mechanism         <- needs 2
     5  indexes                     <- needs 4, or it is a third README snippet
    10  retention policy            <- needs 4 for the table half
     6  raise the Python floor      <- needs 2, and a check on the host
     7  AGENTS.md                   <- independent, optional
     8  split the tests             <- falls out of 2

Items 1, 3 and 9 are done in #19. That PR also took `results_api` from 35% to
97% and `junittools` from 19% to 100%, so item 2 is largely covered for those
two modules. What remains of it is `results_cache` and the untested branches in
`sync_results_api`.

Item 4 is the next one that matters, because it stops silent deployment
failures. The rest is tidying.

## Deliberately not in this plan

- **A delete route for `/history`.** Item 10 covers retention. It argues that a
  cron job beats an endpoint that can remove data.
- **Changing `/history` to overwrite.** It refuses on purpose. A nightly's first
  attempt currently wins over a re-run, which is a real limitation, but changing
  it is a contract change for consumers.
- **Alembic, an ORM, or a database other than sqlite.** The service takes a
  handful of uploads a night. None of that is justified by the load.
- **Rewriting the upload handlers to share one implementation.** See item 2.

## Open questions for the reviewer

1. Does the deployment host have Python 3.12? Item 6 depends on it, and this
   plan cannot check it.
2. Is a hand-run migration actually a problem in practice, or is it fine
   because one person deploys? Item 4's priority depends on the answer.
3. Is `results_api`'s 35% coverage acceptable, given it has not changed in
   years and works? The argument for item 2 is that items 3 to 6 all touch
   shared code underneath it.
4. How far back does the failure analysis need to look? Item 10 cannot be
   sized without that.
5. Who else calls `/results`? Item 1 tightens the accepted characters in
   `testrun_name` and `job_id`, and `cardano-node-tests` is the only caller we
   know about.
