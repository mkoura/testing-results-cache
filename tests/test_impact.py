"""Tests for the issues found by our testing (impact.py) and its dashboard wiring."""

import http
import http.server
import json
import pathlib as pl
import threading
import typing as tp
import urllib.parse

import click
import flask
import flask.testing
import pytest
from click.testing import CliRunner
from click.testing import Result

from testing_results_cache import app as app_module
from testing_results_cache import dashboard
from testing_results_cache import impact

OWNER = "IntersectMBO"
FULL_PAGE = impact.PER_PAGE
SECOND_PAGE = 3
TITLE_LIMIT_INPUT = 500


def _item(repo: str, number: object, **fields: object) -> dict:
    """Return one search result shaped like GitHub's."""
    item: dict = {
        "repository_url": f"https://api.github.com/repos/{OWNER}/{repo}",
        "number": number,
        "state": "closed",
        "created_at": "2024-03-05T10:00:00Z",
        "title": f"Issue {number}",
        "labels": [],
    }
    item.update(fields)
    return item


def _stored(repo: str, number: int, state: str, opened: str, bug: bool) -> dict:
    """Return one issue as `fetch` stores it."""
    return {
        "repo": repo,
        "number": number,
        "title": "t",
        "state": state,
        "opened": opened,
        "bug": bug,
    }


class FakeGitHub:
    """Serve canned search pages per author and record the queries asked."""

    def __init__(self, pages: tp.Dict[str, tp.List[tp.List[dict]]]) -> None:
        """Keep the pages to serve, keyed by author."""
        self.pages = pages
        self.queries: tp.List[dict] = []

    def __call__(self, url: str, token: tp.Optional[str]) -> dict:
        """Answer one search request."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.queries.append({**query, "token": token})
        author = query["q"].split("author:")[1].split()[0]
        pages = self.pages.get(author, [])
        page = int(query["page"])
        return {"items": pages[page - 1] if page <= len(pages) else []}


class LabelledFakeGitHub(FakeGitHub):
    """Also answer repository and label searches."""

    def __init__(
        self, authors: tp.Dict[str, tp.List[tp.List[dict]]], labelled: tp.List[tp.List[dict]]
    ) -> None:
        """Keep the author pages and the labelled pages."""
        super().__init__(authors)
        self.labelled = labelled
        self.labelled_queries: tp.List[str] = []

    def __call__(self, url: str, token: tp.Optional[str]) -> dict:
        """Serve labelled pages for a label query, author pages otherwise."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "label:" not in query["q"]:
            return super().__call__(url, token)
        self.labelled_queries.append(query["q"])
        page = int(query["page"])
        return {"items": self.labelled[page - 1] if page <= len(self.labelled) else []}


class TestFetch:
    """Tests for collecting issues from the search API."""

    def test_follows_pages_until_a_short_one(self) -> None:
        """Ask for the next page only while pages come back full."""
        first = [_item("cardano-node", n) for n in range(FULL_PAGE)]
        second = [_item("cardano-cli", n) for n in range(SECOND_PAGE)]
        fake = FakeGitHub({"alice": [first, second]})
        issues = impact.fetch(["alice"], OWNER, exclude=(), get=fake)
        assert len(issues) == FULL_PAGE + SECOND_PAGE
        assert [q["page"] for q in fake.queries] == ["1", "2"]

    def test_stops_at_the_page_limit(self) -> None:
        """Ask for no more than MAX_PAGES pages, even when every page is full."""
        full = [_item("cardano-node", n) for n in range(FULL_PAGE)]
        fake = FakeGitHub({"alice": [full] * (impact.MAX_PAGES + 3)})
        impact.fetch(["alice"], OWNER, exclude=(), get=fake)
        assert len(fake.queries) == impact.MAX_PAGES

    def test_asks_for_each_author_in_the_organisation(self) -> None:
        """Search issues by every author, inside the configured organisation."""
        fake = FakeGitHub(
            {"alice": [[_item("cardano-node", 1)]], "bob": [[_item("cardano-cli", 2)]]}
        )
        impact.fetch(["alice", "bob"], OWNER, exclude=(), token="t0k", get=fake)
        assert [q["q"] for q in fake.queries] == [
            f"is:issue author:alice org:{OWNER}",
            f"is:issue author:bob org:{OWNER}",
        ]
        assert {q["token"] for q in fake.queries} == {"t0k"}

    def test_leaves_out_our_own_repositories(self) -> None:
        """Drop issues opened in the excluded repositories."""
        fake = FakeGitHub({"alice": [[_item("cardano-node", 1), _item("cardano-node-tests", 2)]]})
        issues = impact.fetch(["alice"], OWNER, exclude={"cardano-node-tests"}, get=fake)
        assert [(i["repo"], i["number"]) for i in issues] == [("cardano-node", 1)]

    def test_counts_labelled_issues_from_an_excluded_repo(self) -> None:
        """Keep an issue with the agreed label, even in our own repo, from any author."""
        fake = LabelledFakeGitHub(
            authors={"alice": [[_item("cardano-node-tests", 1)]]},
            labelled=[[_item("cardano-node-tests", 2)]],
        )
        issues = impact.fetch(
            ["alice"],
            OWNER,
            exclude={"cardano-node-tests"},
            labelled=[("cardano-node-tests", "upstream bug")],
            get=fake,
        )
        assert [i["number"] for i in issues] == [2]
        assert fake.labelled_queries == [
            f'is:issue repo:{OWNER}/cardano-node-tests label:"upstream bug"'
        ]

    def test_drops_malformed_results(self) -> None:
        """Skip a result with a missing field or an unexpected value."""
        fake = FakeGitHub(
            {
                "alice": [
                    [
                        _item("cardano-node", 1),
                        _item("cardano-node", "2"),
                        _item("cardano-node", 3, state="draft"),
                        _item("..", 4),
                        _item("cardano-node", 5, created_at="yesterday"),
                    ]
                ]
            }
        )
        issues = impact.fetch(["alice"], OWNER, exclude=(), get=fake)
        assert [i["number"] for i in issues] == [1]

    def test_keeps_only_the_listed_fields(self) -> None:
        """Copy nothing from a result beyond the fields the page shows."""
        fake = FakeGitHub(
            {"alice": [[_item("cardano-node", 1, body="secret", title="x" * TITLE_LIMIT_INPUT)]]}
        )
        (issue,) = impact.fetch(["alice"], OWNER, exclude=(), get=fake)
        assert set(issue) == {"repo", "number", "title", "state", "opened", "bug"}
        assert len(issue["title"]) == impact.MAX_TITLE
        assert issue["opened"] == "2024-03-05"

    def test_lists_newest_first_without_duplicates(self) -> None:
        """Order by date opened and list a repeated issue once."""
        older = _item("cardano-node", 1, created_at="2023-01-01T00:00:00Z")
        newer = _item("cardano-cli", 2, created_at="2025-01-01T00:00:00Z")
        fake = FakeGitHub({"alice": [[older, newer]], "bob": [[older]]})
        issues = impact.fetch(["alice", "bob"], OWNER, exclude=(), get=fake)
        assert [i["number"] for i in issues] == [2, 1]


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answer one canned search page and remember the request headers."""

    seen: tp.ClassVar[tp.List[dict]] = []

    def do_GET(self) -> None:  # noqa: N802
        """Serve the canned page."""
        type(self).seen.append(dict(self.headers))
        body = json.dumps({"items": [_item("cardano-node", 1)]}).encode()
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def local_github() -> tp.Iterator[str]:
    """Run a local HTTP server standing in for the GitHub search API."""
    _Recorder.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/search/issues?q=x"
    server.shutdown()
    server.server_close()


class TestGet:
    """Tests for the real page fetcher, against a local server."""

    def test_sends_the_token_and_decodes_the_page(self, local_github: str) -> None:
        """Send the token as a bearer header and return the decoded page."""
        page = impact._get(local_github, "t0k")
        assert page["items"][0]["number"] == 1
        (headers,) = _Recorder.seen
        assert headers["Authorization"] == "Bearer t0k"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["User-Agent"] == "testing-results-cache"

    def test_sends_no_token_when_none_is_set(self, local_github: str) -> None:
        """Search anonymously when no token is configured."""
        impact._get(local_github, None)
        (headers,) = _Recorder.seen
        assert "Authorization" not in headers


class TestIsBug:
    """Tests for telling bugs from other issues."""

    @pytest.mark.parametrize(
        ("labels", "title", "expected"),
        [
            (["bug"], "Something", True),
            (["type: bug"], "Something", True),
            (["type: regression"], "Something", True),
            ([], "[BUG] - query hangs", True),
            ([], "BUG: wrong totals", True),
            ([], "bug - lower case", True),
            ([], "[FR] - new option", False),
            ([], "Bugfix release notes", False),
            ([], "Debug output too noisy", False),
            (["bug", "type: not a bug"], "[BUG] - not really", False),
        ],
    )
    def test_classifies(self, labels: tp.List[str], title: str, expected: bool) -> None:
        """Count a bug label or a BUG title, unless a label says not a bug."""
        item = _item("cardano-node", 1, title=title, labels=[{"name": n} for n in labels])
        assert impact.is_bug(item) is expected


class TestSection:
    """Tests for the impact section of the dashboard document."""

    def test_summarises_the_stored_issues(self, tmp_path: pl.Path) -> None:
        """Count issues, bugs, open ones, components and months."""
        issues = [
            _stored("cardano-node", 3, "open", "2024-02-01", True),
            _stored("cardano-node", 2, "closed", "2024-01-20", False),
            _stored("cardano-cli", 1, "closed", "2024-01-02", True),
        ]
        path = tmp_path / "impact.json"
        impact.save(path, OWNER, issues)
        section = impact.load_section(path)
        assert section is not None
        assert (section["found"], section["bugs"], section["open"]) == (3, 2, 1)
        assert section["components"] == [
            {"repo": "cardano-node", "count": 2},
            {"repo": "cardano-cli", "count": 1},
        ]
        assert section["months"] == [
            {"month": "2024-01", "found": 2},
            {"month": "2024-02", "found": 1},
        ]
        assert section["owner"] == OWNER
        assert section["latest"] == issues

    def test_caps_the_latest_issues(self, tmp_path: pl.Path) -> None:
        """List only the newest issues, however many are stored."""
        issues = [
            _stored("cardano-node", n, "closed", "2024-01-01", False)
            for n in range(impact.LATEST + 5)
        ]
        path = tmp_path / "impact.json"
        impact.save(path, OWNER, issues)
        section = impact.load_section(path)
        assert section is not None
        assert len(section["latest"]) == impact.LATEST
        assert section["found"] == len(issues)

    def test_no_file_means_no_section(self, tmp_path: pl.Path) -> None:
        """Return None before any issues were fetched."""
        assert impact.load_section(tmp_path / "missing.json") is None


def _run(app: flask.Flask, command: click.Command) -> Result:
    """Invoke a click command inside the app context it expects."""
    with app.app_context():
        return CliRunner().invoke(command, [], obj=app)


ONE_ISSUE = [_stored("cardano-node", 7, "open", "2024-01-01", True)]


class TestCommand:
    """Tests for the update-impact command."""

    def test_needs_authors(self, app: flask.Flask) -> None:
        """Refuse to run without IMPACT_AUTHORS."""
        result = _run(app, dashboard.update_impact_command)
        assert result.exit_code != 0
        assert "IMPACT_AUTHORS" in result.output

    def test_stores_issues_and_rebuilds_the_dashboard(
        self, app: flask.Flask, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Save the fetched issues and put them into the dashboard file."""
        app.config.update(
            IMPACT_AUTHORS=["alice"],
            IMPACT_FILE=str(tmp_path / "impact.json"),
            DASHBOARD_FOLDER=str(tmp_path / "site"),
        )
        monkeypatch.setattr(impact, "fetch", lambda **_kw: ONE_ISSUE)
        result = _run(app, dashboard.update_impact_command)
        assert result.exit_code == 0, result.output
        written = json.loads((tmp_path / "site" / dashboard.DATA_FILE).read_text())
        assert written["impact"]["found"] == 1
        assert written["impact"]["latest"] == ONE_ISSUE

    def test_stores_issues_without_a_dashboard_folder(
        self, app: flask.Flask, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Store the issues and skip the rebuild when no dashboard folder is set."""
        path = tmp_path / "impact.json"
        app.config.update(IMPACT_AUTHORS=["alice"], IMPACT_FILE=str(path), DASHBOARD_FOLDER=None)
        monkeypatch.setattr(impact, "fetch", lambda **_kw: ONE_ISSUE)
        result = _run(app, dashboard.update_impact_command)
        assert result.exit_code == 0, result.output
        assert "Stored 1 issues." in result.output
        assert "Dashboard rebuilt" not in result.output
        assert json.loads(path.read_text())["issues"] == ONE_ISSUE

    def test_a_failed_fetch_keeps_the_old_file(
        self, app: flask.Flask, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leave the stored issues alone when GitHub cannot be reached."""
        path = tmp_path / "impact.json"
        impact.save(path, OWNER, ONE_ISSUE)
        before = path.read_text()
        app.config.update(IMPACT_AUTHORS=["alice"], IMPACT_FILE=str(path))

        def unreachable(**_kw: object) -> tp.List[dict]:
            """Fail the fetch as if GitHub could not be reached."""
            msg = "network is unreachable"
            raise OSError(msg)

        monkeypatch.setattr(impact, "fetch", unreachable)
        result = _run(app, dashboard.update_impact_command)
        assert result.exit_code != 0
        assert "Could not fetch issues" in result.output
        assert path.read_text() == before


class TestConfig:
    """Tests for where the impact settings come from."""

    def test_the_token_comes_from_the_environment(
        self, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read the GitHub token from IMPACT_GITHUB_TOKEN when it is set."""
        monkeypatch.setenv("INSTANCE_PATH", str(tmp_path))
        monkeypatch.setenv("IMPACT_GITHUB_TOKEN", "ghp_from_env")
        assert app_module.create_app().config["IMPACT_GITHUB_TOKEN"] == "ghp_from_env"

    def test_no_token_by_default(self, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Search without a token when none is configured."""
        monkeypatch.setenv("INSTANCE_PATH", str(tmp_path))
        monkeypatch.delenv("IMPACT_GITHUB_TOKEN", raising=False)
        assert app_module.create_app().config["IMPACT_GITHUB_TOKEN"] is None

    def test_the_instance_config_wins(
        self, tmp_path: pl.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Let the instance config.py override the environment."""
        monkeypatch.setenv("INSTANCE_PATH", str(tmp_path))
        monkeypatch.setenv("IMPACT_GITHUB_TOKEN", "ghp_from_env")
        (tmp_path / "config.py").write_text('IMPACT_GITHUB_TOKEN = "ghp_from_file"\n')
        assert app_module.create_app().config["IMPACT_GITHUB_TOKEN"] == "ghp_from_file"


class TestUploadHook:
    """Tests for the impact section in the file an upload rebuilds."""

    def test_an_upload_keeps_the_impact_section(
        self,
        app: flask.Flask,
        client: flask.testing.FlaskClient,
        auth_headers: dict,
        tmp_path: pl.Path,
    ) -> None:
        """Carry the stored issues into the file a stats upload rebuilds."""
        path = tmp_path / "impact.json"
        impact.save(path, OWNER, ONE_ISSUE)
        app.config.update(IMPACT_FILE=str(path), DASHBOARD_FOLDER=str(tmp_path / "site"))
        doc = {
            "schema": 1,
            "project": "cardano-node-tests",
            "testrun_name": "nightly",
            "run_id": "1",
            "origin": "ci",
            "timestamp": "2026-10-01T00:00:00+00:00",
            "duration": 1.0,
            "exit_code": 0,
            "counts": {"total": 1, "passed": 1, "failed": 0, "broken": 0, "skipped": 0},
            "quality": {"never_run": 0},
        }
        response = client.put("/stats", data=json.dumps(doc), headers=auth_headers)
        assert response.status_code == http.HTTPStatus.OK, response.data
        written = json.loads((tmp_path / "site" / dashboard.DATA_FILE).read_text())
        assert written["impact"]["found"] == 1
        assert written["all_time"]["runs"] == 1
