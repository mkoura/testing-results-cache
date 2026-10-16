"""Issues found by our testing, read from GitHub.

`fetch` asks the GitHub issue search for the issues each configured account
opened in the configured organisation, leaving out our own test
repositories, plus the issues carrying a given label in a given repository
(issues we file in our own repo for problems found without an upstream
issue). `save` keeps them in a private file next to the database, and
`load_section` turns that file into the bounded `impact` section of the
public dashboard document.
"""

import json
import re
import typing as tp
import urllib.parse
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path

from testing_results_cache import common

SEARCH_URL = "https://api.github.com/search/issues"
PER_PAGE = 100
# The search API returns at most 1000 results per query.
MAX_PAGES = 10
TIMEOUT = 30
MAX_TITLE = 200
LATEST = 12
STATES = frozenset({"open", "closed"})
BUG_TITLE = re.compile(r"^\s*\[?\s*BUG\b", re.IGNORECASE)

Getter = tp.Callable[[str, tp.Optional[str]], dict]


def _get(url: str, token: tp.Optional[str]) -> dict:
    """Fetch one page of search results.

    Args:
        url: The search URL, query included.
        token: A GitHub token, or None to search without one.

    Returns:
        The decoded JSON response.
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "testing-results-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return dict(json.load(response))


def is_bug(item: dict) -> bool:
    """Tell whether a search result is a bug report.

    Args:
        item: One issue from the search results.

    Returns:
        True when a label names a bug or regression, or the title starts with
        "BUG". A "not a bug" label always wins.
    """
    names = [str(label.get("name", "")).lower() for label in item.get("labels") or []]
    if any("not a bug" in n for n in names):
        return False
    if any("bug" in n or "regression" in n for n in names):
        return True
    return bool(BUG_TITLE.match(str(item.get("title", ""))))


def _issue(item: dict) -> tp.Optional[dict]:
    """Keep only the fields the dashboard shows.

    Args:
        item: One issue from the search results.

    Returns:
        The issue, or None when a field is missing or has the wrong type.
    """
    repo = str(item.get("repository_url", "")).rsplit("/", 1)[-1]
    number = item.get("number")
    state = item.get("state")
    created = str(item.get("created_at", ""))
    if not common.valid_path_segment(repo) or not isinstance(number, int) or state not in STATES:
        return None
    try:
        opened = datetime.fromisoformat(created).date().isoformat()
    except ValueError:
        return None
    return {
        "repo": repo,
        "number": number,
        "title": str(item.get("title", ""))[:MAX_TITLE],
        "state": state,
        "opened": opened,
        "bug": is_bug(item),
    }


def _search(query: str, token: tp.Optional[str], get: Getter) -> tp.Iterator[dict]:
    """Yield the usable results of one search, page by page.

    Args:
        query: The GitHub search query.
        token: A GitHub token, or None to search without one.
        get: The page fetcher.

    Yields:
        Each result reduced to the fields the dashboard shows.
    """
    for page in range(1, MAX_PAGES + 1):
        params = urllib.parse.urlencode({"q": query, "per_page": PER_PAGE, "page": page})
        items = get(f"{SEARCH_URL}?{params}", token).get("items") or []
        for item in items:
            issue = _issue(item)
            if issue:
                yield issue
        if len(items) < PER_PAGE:
            return


def fetch(
    authors: tp.Sequence[str],
    owner: str,
    exclude: tp.Collection[str],
    labelled: tp.Sequence[tp.Sequence[str]] = (),
    token: tp.Optional[str] = None,
    get: Getter = _get,
) -> tp.List[dict]:
    """Collect the issues found by our testing, newest first.

    Args:
        authors: GitHub logins whose issues count as found by our testing.
        owner: The GitHub organisation to search in.
        exclude: Repository names to leave out of the author search, such as
            our own test repos.
        labelled: (repository, label) pairs whose issues count from any
            author, even in an excluded repository.
        token: A GitHub token, or None to search without one.
        get: The page fetcher, replaceable in tests.

    Returns:
        One entry per issue, without duplicates.
    """
    issues: tp.Dict[tp.Tuple[str, int], dict] = {}
    for author in authors:
        for issue in _search(f"is:issue author:{author} org:{owner}", token, get):
            if issue["repo"] not in exclude:
                issues[(issue["repo"], issue["number"])] = issue
    for repo, label in labelled:
        for issue in _search(f'is:issue repo:{owner}/{repo} label:"{label}"', token, get):
            issues[(issue["repo"], issue["number"])] = issue
    return sorted(issues.values(), key=lambda i: (i["opened"], i["number"]), reverse=True)


def save(path: Path, owner: str, issues: tp.List[dict]) -> None:
    """Store the fetched issues in the private impact file.

    Args:
        path: The impact file.
        owner: The GitHub organisation the issues belong to.
        issues: The issues from `fetch`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    common.replace_json(
        path, {"fetched_at": datetime.now(UTC).isoformat(), "owner": owner, "issues": issues}
    )


def load_section(path: Path) -> tp.Optional[dict]:
    """Build the dashboard's impact section from the impact file.

    Args:
        path: The impact file.

    Returns:
        Totals, counts per component and per month, and the latest issues,
        or None when no impact file exists yet.
    """
    if not path.is_file():
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    issues: tp.List[dict] = stored["issues"]
    components: tp.Dict[str, int] = {}
    months: tp.Dict[str, int] = {}
    for issue in issues:
        components[issue["repo"]] = components.get(issue["repo"], 0) + 1
        months[issue["opened"][:7]] = months.get(issue["opened"][:7], 0) + 1
    return {
        "fetched_at": stored["fetched_at"],
        "owner": stored["owner"],
        "found": len(issues),
        "bugs": sum(1 for i in issues if i["bug"]),
        "open": sum(1 for i in issues if i["state"] == "open"),
        "components": [
            {"repo": r, "count": n} for r, n in sorted(components.items(), key=lambda c: -c[1])
        ],
        "months": [{"month": m, "found": months[m]} for m in sorted(months)],
        "latest": issues[:LATEST],
    }
