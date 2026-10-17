"""Inbox: what GitHub has for you, mentions of you first: who wrote what, on threads you're part of.

Your unread notifications, read-only: the deck never marks one read, it goes once you read it on GitHub.
Only the ones that talk to you: mentions of you or your teams, replies where you commented, activity on
what you opened or were assigned. Review requests are To review's, and repos you watch stay out.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

from dash_common import PR_KEYS
from dash_github import Store, gh_json, iso_time
from dash_ui import (
    BLUE, BRIGHT, DIM, ICON, LINE, MUTED, NO_UNDERLINE, RED, RESET, TEXT, UNDERLINE, YELLOW, ago, chip, fit,
    key_chip, link, paint, plural, repo_label, repo_width,
)


TITLE, TITLE_ICON = "Inbox", ICON["comment"]
ALERT_ORDER = 3
SHOWN = 8  # rows at most; the rest are on GitHub
WHY = {  # the reasons GitHub notifies you that talk to you, and how the card names them
    "mention": "mentioned you", "team_mention": "mentioned your team", "comment": "replied",
    "author": "on yours", "assign": "assigned you", "manual": "you follow it", "state_change": "changed",
}
COMMENTS = {}  # comment url -> what it says, read once: a new comment has a new url
MENTIONS = {}  # (thread, when it changed) -> where you or your team were mentioned in it, looked for once
ME = []  # @you and @org/team for your teams: what a mention of you looks like, looked up once
RANK = {"mention": 0, "team_mention": 2}  # mentions of you first, your teams' last (often a heads-up to all)
COMMENT_EXEC = ThreadPoolExecutor(max_workers=6)  # its own, as it runs inside a fetch of the deck's


def fetch_inbox(_):
    """Your unread notifications that talk to you, each with its last comment; False if gh failed."""
    found = gh_json("api", "notifications?per_page=50")  # unread only
    if found is None:
        return False
    handles()  # once, before the threads below need it
    return list(COMMENT_EXEC.map(entry, [n for n in found if n.get("reason") in WHY]))


def handles():
    """@you, and @org/team for each team you're in; [] if gh failed (then tried again next time)."""
    if not ME:
        me = gh_json("api", "user")
        query = ("query=query($login: String!) { viewer { organizations(first: 100) { nodes { login "
                 "teams(first: 100, userLogins: [$login]) { nodes { slug } } } } } }")
        d = gh_json("api", "graphql", "-f", query, "-f", f"login={me['login']}") if me else None
        orgs = ((((d or {}).get("data") or {}).get("viewer") or {}).get("organizations") or {}).get("nodes") or []
        if me and d:
            teams = [
                (org["login"], team["slug"])
                for org in orgs if org for team in (org.get("teams") or {}).get("nodes") or []
            ]
            ME[:] = [f"@{me['login']}"] + [f"@{org}/{slug}" for org, slug in teams]
    return ME


def entry(n):
    subject, repo = n.get("subject") or {}, (n.get("repository") or {}).get("full_name") or ""
    said = {}
    if n["reason"] in RANK:  # what mentioned you or your team, not whatever came after it
        said = mentioned(subject.get("url"), repo, n.get("updated_at"))
    if not said:  # the last comment, or the PR or issue itself when nothing was said since it opened
        url = subject.get("latest_comment_url") or subject.get("url")
        said = (COMMENTS.get(url) or comment(url)) if url else {}
    return {
        "reason": n["reason"], "repo": repo, "title": subject.get("title") or "",
        "url": said.get("url") or page(subject.get("url"), repo), "author": said.get("author") or "",
        "said": said.get("said") or "", "at": iso_time(n.get("updated_at")),
    }


def comment(url):
    """Who wrote a comment, its first line and its page; {} if gh failed."""
    d = gh_json("api", url)
    if not d:
        return {}
    COMMENTS[url] = {"author": (d.get("user") or {}).get("login") or "", "said": first_line(d.get("body")),
                     "url": d.get("html_url") or ""}
    return COMMENTS[url]


def mentioned(thread, repo, changed):
    """Who mentioned you or one of your teams in a PR or issue, the line that does and its page: the newest
    comment that does, else the description; {} when it's elsewhere (a review, an edit) or gh failed."""
    if (thread, changed) in MENTIONS:
        return MENTIONS[(thread, changed)]
    m = re.search(r"/(?:pulls|issues)/(\d+)$", thread or "")
    if not m or not ME:
        return {}
    pattern = re.compile(r"(?<![\w/-])(" + "|".join(re.escape(h) for h in ME) + r")(?![\w/-])", re.I)
    comments = gh_json("api", f"repos/{repo}/issues/{m.group(1)}/comments?per_page=100") or []
    found = {}
    for text in list(reversed(comments)) + [gh_json("api", thread) or {}]:  # the newest first, the description last
        line = first_line(text.get("body"), pattern)
        if line:
            who = (text.get("user") or {}).get("login") or ""
            found = {"author": who, "said": line, "url": text.get("html_url") or ""}
            break
    MENTIONS[(thread, changed)] = found
    return found


def first_line(body, pattern=None):
    """A comment's first line that says something (that matches pattern, with one), without markup: not a
    quote, an image or HTML."""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    for line in body.splitlines():
        line = re.sub(r"<[^>]*>|!\[[^\]]*\]\([^)]*\)", "", line).strip()  # tags and images
        line = re.sub(r"^([-*+]\s+(\[[ xX]\]\s*)?|#+\s+|\d+\.\s+)", "", line).strip()  # list, task, heading
        if line and not line.startswith(">") and re.search(r"\w", line) and (not pattern or pattern.search(line)):
            return re.sub(r"\s+", " ", line)
    return ""


def page(api_url, repo):
    """A PR's or issue's page from its API url; your notifications when it's neither."""
    m = re.search(r"/(pulls|issues)/(\d+)$", api_url or "")
    if not m or not repo:
        return "https://github.com/notifications"
    return f"https://github.com/{repo}/{'pull' if m.group(1) == 'pulls' else 'issues'}/{m.group(2)}"


INBOX = Store("inbox", fetch_inbox)


def refresh(data, force):
    INBOX.refresh(["unread"], force)


def fetched_at(data):
    return INBOX.fetched_at(["unread"])


def mentions(items):
    """The ones that mention you, not just a team of yours."""
    return [n for n in items if n["reason"] == "mention"]


def ordered(items):
    """Mentions of you first, then replies and the rest, then your teams' mentions; the newest first in each."""
    return sorted(items, key=lambda n: (RANK.get(n["reason"], 1), -(n["at"] or 0)))


def alerts(data):
    found = len(mentions((INBOX.get("unread") or {}).get("value") or []))
    return [chip(YELLOW, f"@ {plural(found, 'mention', 'mentions')}")] if found else []


def render(data, inner, rows=None, keys=PR_KEYS):
    entry = INBOX.get("unread")
    if entry is None:
        return {"lines": [paint(DIM, "Loading your notifications from GitHub…")]}
    if entry["value"] is None:
        return {"lines": [paint(RED, "Couldn't read your notifications (check gh auth status)")]}
    items = ordered(entry["value"])
    if not items:
        return {"lines": [paint(DIM, "Nothing new for you on GitHub")]}
    shown = items[:SHOWN]
    mixed = len({n["repo"].split("/")[0] for n in shown}) > 1
    repo_w = repo_width([n["repo"] for n in shown], mixed)
    # Columns: key 4, why 3, who 18, what they said and the title share what's left (55/45), repo, age 5
    left = max(40, inner - 4 - 3 - 18 - repo_w - 5)
    said_w, title_w = left * 55 // 100, left - left * 55 // 100
    now, lines, links = time.time(), [], {}
    for i, n in enumerate(shown):
        key = keys[i] if i < len(keys) else ""
        if key:
            links[key] = n["url"]
        mention = n["reason"] == "mention"
        said = paint(TEXT, f"“{n['said']}”") if n["said"] else paint(DIM, WHY[n["reason"]])
        lines.append(
            fit(key_chip(key), 4)
            + fit(paint(YELLOW, "@") if mention else paint(MUTED, "@" if n["reason"] in RANK else ICON["comment"]), 3)
            + fit(paint(BRIGHT if mention else TEXT, n["author"] or "—"), 16) + "  "
            + fit(link(n["url"], said), said_w - 2) + "  "
            + fit(repo_label(n["repo"], mixed, MUTED), repo_w - 2) + "  "
            + fit(link(n["url"], paint(DIM, n["title"])), title_w - 2) + "  "
            + fit(paint(DIM, ago(now - n["at"])) if n["at"] else "", 5)
        )
    if len(items) > len(shown):
        lines.append(
            " " * 4 + f"{LINE}└{RESET} " + paint(DIM, f"+{len(items) - len(shown)} more") + f"  {DIM}·{RESET}  "
            + link("https://github.com/notifications", f"{BLUE}{UNDERLINE}all on GitHub{NO_UNDERLINE}{RESET}")
        )
    found = len(mentions(items))
    first = f" · {plural(found, 'mention', 'mentions')} first" if found else ""
    note = f"{len(items)} unread{first} · read them on GitHub"
    return {"lines": lines, "note": note, "links": links}
