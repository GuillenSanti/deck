"""To review: the open PRs that ask you or your teams for a review, the ones asking you first."""

import shlex
import subprocess
import sys
import time
import urllib.parse

import pr_review
from dash_common import BROADCAST_TEAMS, MIN_REVIEW_ROWS, NOTICE, ORGS, PR_KEYS, PR_REVIEW, REVIEW_ROWS
from dash_github import MY_ORGS, REVIEWS, pending_reviews, reviews_key
from dash_ui import (
    BLUE, BRIGHT, DIM, GREEN, ICON, LINE, MUTED, NO_UNDERLINE, RED, RESET, TEXT, UNDERLINE, YELLOW, ago, chip,
    fit, key_chip, link, paint, repo_label, repo_width, width,
)


TITLE, TITLE_ICON = "To review", ICON["review"]
FLEX = True
ALERT_ORDER = 3
HINT = "A-Z  review it here  ·  +  review a PR"
ONLY_ASKED_YOU = [False]  # @ toggles To review between everything and what asks you by name
REVIEW_KEYS = {}  # upper-case key -> url of a PR in the card, to review it in the terminal


def refresh(data, force):
    REVIEWS.refresh([reviews_key()], force)


def fetched_at(data):
    return REVIEWS.fetched_at([reviews_key()])


def render(data, inner, rows=None, keys=PR_KEYS):
    """As many PRs as rows leaves room for, under the line of notes, at least MIN_REVIEW_ROWS."""
    limit = REVIEW_ROWS if rows is None else max(MIN_REVIEW_ROWS, rows - 1)
    entry = REVIEWS.get(reviews_key())
    lines, links, targets = review_lines(entry, keys, inner, {a["window"] for a in data["agents"]}, limit)
    REVIEW_KEYS.clear()
    REVIEW_KEYS.update({k.upper(): url for k, url in links.items()})
    worth = pending_reviews(entry)
    if ONLY_ASKED_YOU[0]:
        asked = sum(1 for p in worth if p["direct"])
        note = f"{asked} asking you by name · @ shows all"
    else:
        note = f"{len(worth)} waiting · asking you first, then newest · @ only the ones asking you" if worth else ""
    return {"lines": lines, "note": note, "links": links, "targets": targets}


def alerts(data):
    """The reviews asked of you by name."""
    direct = sum(1 for p in pending_reviews(REVIEWS.get(reviews_key())) if p["direct"])
    return [chip(YELLOW, f"{ICON['review']} {direct} asked you")] if direct else []


def key(k):
    if k == "+":
        open_review("ask")
    elif k == "@":
        ONLY_ASKED_YOU[0] = not ONLY_ASKED_YOU[0]
    elif k in REVIEW_KEYS:
        open_review(REVIEW_KEYS[k])
    else:
        return False
    return True


def act(target):
    if target.startswith("review:"):
        open_review(target[len("review:"):])


def open_review(target):
    """A PR's review window (diff and Claude), from pr_review.py; for "ask", a popup asks which PR first."""
    if target == "ask":
        cmd = ["tmux", "display-popup", "-E", "-w", "80", "-h", "10", "-T", " review a PR ",
               f"{shlex.quote(sys.executable)} {shlex.quote(PR_REVIEW)} --ask"]
    else:
        cmd = [sys.executable, PR_REVIEW, target]
        NOTICE[:] = [f"opening the review of #{target.rsplit('/', 1)[-1]}", time.time() + 3]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def short_login(login):
    """A login without the suffix of one of your organisations many carry: name-acme, name-ac for acme."""
    head, sep, tail = login.rpartition("-")
    ours = sep and len(tail) >= 3 and any(org.lower().startswith(tail.lower()) for org in MY_ORGS + ORGS)
    return head if ours else login


def review_lines(entry, keys, inner, windows=(), limit=REVIEW_ROWS):
    """Rows for the PRs waiting for your review, the key -> url map for opening them, and
    (line, first column, end column, target) for the buttons that review one in the terminal."""
    if entry is None:
        return [paint(DIM, "Loading review requests from GitHub…")], {}, []
    if entry["value"] is None:
        return [paint(RED, "Couldn't read review requests (check gh auth status)")], {}, []
    button = f"{BLUE}{ICON['console']} {UNDERLINE}review a PR…{NO_UNDERLINE}{RESET}"
    worth = pending_reviews(entry)
    hidden = len(entry["value"] or []) - len(worth)
    if ONLY_ASKED_YOU[0]:
        worth, hidden = [p for p in worth if p["direct"]], 0
    if not worth:
        nothing = paint(DIM, "Nothing asks you by name (@ shows all)" if ONLY_ASKED_YOU[0] else "Nothing waiting for your review")
        nothing += f"  {DIM}·{RESET}  "
        return [nothing + button], {}, [(0, width(nothing), width(nothing) + width(button), "review:ask")]
    shown = worth[:limit]  # a letter for the first ones, as long as there are letters left
    # Columns: key 4, number 9, review button 3, reviewed before 2, repo (the longest, up to 16, 32 with
    # owners), title (fills), author 22, via 26, CI 3, size 12, age 5
    repos = [pr_review.parse(pr["url"])[0] for pr in shown]
    mixed = len({r.split("/")[0] for r in repos}) > 1  # then each repo goes with its owner
    repo_w = repo_width(repos, mixed)
    title_w = max(20, inner - 4 - 9 - 3 - 2 - repo_w - 22 - 26 - 3 - 12 - 5)
    ci_icon = {"SUCCESS": paint(GREEN, ICON["check"]), "FAILURE": paint(RED, ICON["cross"]),
               "ERROR": paint(RED, ICON["cross"]), "PENDING": paint(YELLOW, ICON["sand"]),
               "EXPECTED": paint(YELLOW, ICON["sand"])}
    now = time.time()
    lines, links, targets = [], {}, []
    for i, pr in enumerate(shown):
        key = keys[i] if i < len(keys) else ""
        if key:
            links[key] = pr["url"]
        teams = pr["via"] or pr["teams"]  # your teams, or every team asked when none is yours
        if pr["direct"]:
            via = paint(YELLOW, "asked you")
        else:
            via = paint(DIM, teams[0] + (f" +{len(teams) - 1}" if len(teams) > 1 else "")) if teams else ""
        if pr.get("reviewed"):  # blue when there are commits since your review
            fresh = pr.get("pushed") and pr["pushed"] > pr["reviewed"]
            seen = paint(BLUE if fresh else DIM, ICON["history"])
        else:
            seen = ""
        repo, number = pr_review.parse(pr["url"])
        reviewing = pr_review.window_name(repo, number) in windows  # its review window is open
        targets.append((len(lines), 4 + 9, 4 + 9 + 2, f"review:{pr['url']}"))
        lines.append(
            fit(key_chip(key), 4)
            + fit(link(pr["url"], paint(BLUE, f"{UNDERLINE}#{pr['number']}{NO_UNDERLINE}")), 9)
            + fit(paint(GREEN if reviewing else MUTED, ICON["console"]), 3)
            + fit(seen, 2)
            + fit(repo_label(repo, mixed, MUTED), repo_w - 2) + "  "
            + fit(link(pr["url"], paint(BRIGHT if pr["direct"] else TEXT, pr["title"])), title_w - 2) + "  "
            + fit(paint(MUTED, short_login(pr["author"])), 20) + "  "
            + fit(via, 24) + "  "
            + fit(ci_icon.get(pr["ci"], paint(DIM, "–")), 3)
            + fit(f"{GREEN}+{pr['adds']}{RESET} {RED}−{pr['dels']}{RESET}", 12)
            + fit(paint(DIM, ago(now - pr["at"])) if pr["at"] else "", 5)
        )
    more = len(worth) - len(shown)
    search = "https://github.com/pulls?q=" + urllib.parse.quote_plus(
        "is:open is:pr review-requested:@me" + "".join(f" org:{org}" for org in ORGS) + " draft:false"
    )
    notes = [paint(DIM, f"+{more} more")] if more else []
    if any(p.get("reviewed") for p in shown):
        notes.append(f"{BLUE}{ICON['history']}{RESET} {DIM}you reviewed it, new commits since{RESET}")
    if hidden:
        notes.append(paint(DIM, f"{hidden} hidden: from bots or sent to more than {BROADCAST_TEAMS} teams"))
    if notes:
        notes.append(link(search, f"{BLUE}{UNDERLINE}all on GitHub{NO_UNDERLINE}{RESET}"))
    sep = f"  {DIM}·{RESET}  "
    start = " " * 4 + f"{LINE}└{RESET} " + "".join(n + sep for n in notes)
    targets.append((len(lines), width(start), width(start) + width(button), "review:ask"))
    lines.append(start + button)
    return lines, links, targets
