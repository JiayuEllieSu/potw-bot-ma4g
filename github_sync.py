"""Publish POTW scores and archived weeks to a separate GitHub data branch.

Only PUBLIC scoreboard information is uploaded. Discord and GitHub credentials
must be provided through environment variables, never committed to Git.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.getenv("GITHUB_REPO", "JiayuEllieSu/ma4g-bot")
BRANCH = os.getenv("GITHUB_DATA_BRANCH", "leaderboard-data")


def configured():
    return bool(os.getenv("GITHUB_PAT", "").strip())


def _request(method, path, payload=None):
    token = os.getenv("GITHUB_PAT", "").strip()
    if not token:
        raise RuntimeError("Set GITHUB_PAT in Railway Variables before using GitHub sync.")
    url = f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path, safe='/')}"
    if method == "GET":
        url += "?ref=" + urllib.parse.quote(BRANCH, safe="")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MA4G-POTW-bot",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {path} failed (HTTP {exc.code}).") from None


def fetch_text(path):
    obj = _request("GET", path)
    if obj is None:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "file":
        raise RuntimeError(f"GitHub path is not a file: {path}")
    return base64.b64decode(obj["content"]).decode("utf-8")


def fetch_json(path):
    content = fetch_text(path)
    return json.loads(content) if content is not None else None


def publish_text(path, content, message):
    """Upsert with SHA validation, serial retries, and no-op detection."""
    if not content.endswith("\n"):
        content += "\n"
    for attempt in range(3):
        old = _request("GET", path)
        if old is not None:
            previous = base64.b64decode(old["content"]).decode("utf-8")
            if previous == content:
                return False
        payload = {
            "message": message + " [skip ci]",
            "branch": BRANCH,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if old is not None:
            payload["sha"] = old["sha"]
        try:
            _request("PUT", path, payload)
            return True
        except RuntimeError as exc:
            if "HTTP 409" not in str(exc) or attempt == 2:
                raise
            time.sleep(attempt + 1)
    return False


def _dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _safe(s):
    return str(s).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _table(scores, names):
    rows = ["| Rank | Participant | Points |",
            "| ---: | :--- | ---: |"]
    ordered = sorted(scores.items(), key=lambda entry: (-float(entry[1]), entry[0]))
    for i, (uid, points) in enumerate(ordered, 1):
        name = _safe(names.get(uid, f"User {uid}"))
        rows.append(f"| {i} | {name} | {float(points):.1f} |")
    if not ordered:
        rows.append("| | No scores yet | |")
    return "\n".join(rows)


def leaderboard_markdown(leaderboard):
    names = leaderboard.get("names", {})
    week = leaderboard.get("active_week", "?")
    weekly = leaderboard.get("weekly_users", {})
    all_time = leaderboard.get("users", {})
    return (
        "# MA4G Problem of the Week Leaderboard\n\n"
        "Automatically updated by the Discord bot. "
        "The weekly scores reset when the next POTW is posted; "
        "all-time totals are retained.\n\n"
        f"## Week {week}: current leaderboard\n\n"
        f"{_table(weekly, names)}\n\n"
        "## All-time leaderboard\n\n"
        f"{_table(all_time, names)}\n\n"
        "Previous weeks: [archives](archives/). "
        "The complete scoring history is in [points_history.json](points_history.json).\n"
    )


def archive_markdown(archive):
    names = archive.get("names", {})
    return (
        f"# MA4G POTW Week {archive['week']}\n\n"
        f"Closed: {archive['closed_at_utc']} (UTC)\n\n"
        "## Final weekly standings\n\n"
        f"{_table(archive['weekly_users'], names)}\n\n"
        "## All-time standings at close\n\n"
        f"{_table(archive['all_time_users'], names)}\n"
    )


def publish_snapshot(leaderboard, history):
    publish_text("leaderboard.md", leaderboard_markdown(leaderboard),
                 "Update POTW leaderboard")
    publish_text("leaderboard.json", _dump(leaderboard),
                 "Update POTW scores")
    publish_text("points_history.json", _dump(history),
                 "Update POTW point ledger")


def publish_archive(archive):
    tag = archive["tag"].replace("/", "-")
    week = int(archive["week"])
    stem = f"archives/{tag}_week_{week:03d}"
    publish_text(stem + ".json", _dump(archive),
                 f"Archive POTW week {week}")
    publish_text(stem + ".md", archive_markdown(archive),
                 f"Publish POTW week {week} standings")
