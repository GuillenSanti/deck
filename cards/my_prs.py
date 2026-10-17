"""My PRs: your open PRs, with CI, reviews and conflicts, and the ones merged lately."""

import time

from dash_common import MERGED_DAYS, MERGED_MAX, MERGED_SHOWN, PR_KEYS, cards_with
from dash_github import MY_PRS, mine_key
from dash_ui import (
    BLUE, BRIGHT, DIM, GREEN, ICON, MUTED, NO_UNDERLINE, PURPLE, RED, RESET, TEXT, UNDERLINE, YELLOW, ago, chip,
    fit, key_chip, link, paint, plural, repo_label, repo_width,
)


TITLE, TITLE_ICON = "My PRs", ICON["pull"]
ALERT_ORDER = 2
HINT = "click or a-z  open PR"


def refresh(data, force):
    MY_PRS.refresh([mine_key()], force)


def fetched_at(data):
    return MY_PRS.fetched_at([mine_key()])


def render(data, inner, rows=None, keys=PR_KEYS):
    entry = MY_PRS.get(mine_key())  # None until the first fetch lands
    lines, links = my_pr_lines(entry, data, inner, keys)
    mine = (entry or {}).get("value") or []
    opened = sum(1 for p in mine if p["state"] == "OPEN")
    merged = len(mine) - opened
    note = f"{opened} open · {merged}{'+' if merged >= MERGED_MAX else ''} merged in {MERGED_DAYS} days" if mine else ""
    return {"lines": lines, "note": note, "links": links}


def alerts(data):
    """Your open PRs failing CI, with changes requested or with conflicts."""
    mine = [p for p in (MY_PRS.get(mine_key()) or {}).get("value") or [] if p["state"] == "OPEN"]
    out = []
    red = sum(1 for p in mine if p["failed"])
    changes = sum(1 for p in mine if p.get("changes_by") or p["review"] == "CHANGES_REQUESTED")
    conflicts = sum(1 for p in mine if p["conflicts"])
    if red:
        out.append(chip(RED, f"{ICON['cross']} {red} failing CI"))
    if changes:
        out.append(chip(RED, f"{ICON['comment']} {changes} changes requested"))
    if conflicts:
        out.append(chip(RED, f"{ICON['merge']} {plural(conflicts, 'conflict', 'conflicts')}"))
    return out


def queue_text(q):
    """Where a PR stands in the merge queue, or "" when it isn't in it (nor just thrown out)."""
    if not q:
        return ""
    if q["in_queue"]:
        if q["state"] == "UNMERGEABLE":
            return paint(RED, f"{ICON['cross']} queue #{q['position']}: failing")
        eta = q.get("eta")
        eta = f" · ~{max(1, round(eta / 60))}m" if eta and eta < 3600 else f" · ~{ago(eta)}" if eta else ""
        return paint(YELLOW, f"{ICON['sand']} queued #{q['position']}{eta}")
    if q.get("removed") and q["removed"].lower() != "merged":
        return paint(RED, f"{ICON['cross']} out of queue: {q['removed'].lower().replace('_', ' ')}")
    return ""


def ci_text(pr):
    """The merge queue when the PR is in it, else CI; naming the checks when only one or two are left."""
    queued = queue_text(pr.get("queue"))
    if queued:
        return queued
    if pr.get("ci_unknown"):
        return paint(DIM, "? CI")
    if pr["failed"]:
        names = pr.get("failed_names") or []
        return paint(RED, f"{ICON['cross']} " + (", ".join(names) if 0 < len(names) <= 2 else f"CI {pr['failed']}"))
    if pr["pending"]:
        names = pr.get("pending_names") or []
        return paint(YELLOW, f"{ICON['sand']} " + (", ".join(names) if 0 < len(names) <= 2 else "CI"))
    return paint(GREEN, f"{ICON['check']} CI")


def draft_text(pr):
    """Where the others say how CI goes, a draft says it's one; CI follows, briefly (a draft can break it too)."""
    if pr.get("ci_unknown"):
        ci = paint(DIM, "? CI")
    elif pr["failed"]:
        ci = paint(RED, f"{ICON['cross']} CI")
    elif pr["pending"]:
        ci = paint(YELLOW, f"{ICON['sand']} CI")
    else:
        ci = paint(GREEN, f"{ICON['check']} CI")
    return f"{TEXT}{ICON['draft']} draft{RESET}{DIM}  ·  {RESET}{ci}"


def review_text(pr):
    if pr["draft"]:
        return paint(DIM, "draft")
    return {
        "APPROVED": paint(GREEN, "approved"),
        "CHANGES_REQUESTED": paint(RED, "changes requested"),
        "REVIEW_REQUIRED": paint(DIM, "review required"),
    }.get(pr["review"], "")


def approval_text(pr):
    """Whether an open PR has the approvals it needs, still needs some, or has changes requested."""
    changes, approvals = pr.get("changes_by") or [], pr.get("approvals") or 0
    if changes or pr["review"] == "CHANGES_REQUESTED":
        who = f"{changes[0]}" + (f" +{len(changes) - 1}" if len(changes) > 1 else "") if changes else "requested"
        return paint(RED, f"{ICON['cross']} changes: {who}")
    if pr["review"] == "APPROVED":  # every approval the repo requires
        return paint(GREEN, f"{ICON['check']} approved" + (f" ({approvals})" if approvals > 1 else ""))
    if pr["review"] == "REVIEW_REQUIRED" and not pr["draft"]:  # a draft isn't asking for reviews yet
        return paint(YELLOW, "needs review" + (f" · {approvals} {ICON['check']}" if approvals else ""))
    return paint(DIM, f"{approvals} {ICON['check']}") if approvals else ""


def size_text(pr):
    if pr.get("adds") is None:
        return ""
    return f"{GREEN}+{pr['adds']}{RESET} {RED}−{pr['dels']}{RESET}"


def my_pr_lines(entry, data, inner, keys=PR_KEYS):
    """Rows for your PRs, and the key -> url map for opening them."""
    if entry is None:
        return [paint(DIM, "Loading your PRs from GitHub…")], {}
    if entry["value"] is None:
        return [paint(RED, "Couldn't read your PRs (check gh auth status)")], {}
    opened = [p for p in entry["value"] if p["state"] == "OPEN"]
    merged = [p for p in entry["value"] if p["state"] == "MERGED"][:MERGED_SHOWN]
    prs = opened + merged  # a letter for the first ones, as long as there are letters left
    if not prs:
        return [paint(DIM, f"No open PRs, and none merged in the last {MERGED_DAYS} days")], {}
    taggers = [c.pr_tag for c in cards_with("pr_tag")]  # what other cards say of a PR
    # Columns: key 4, number 9, repo (the longest, up to 16, 32 with owners), title (fills), CI or queue 28,
    # approvals 24, unresolved 16, flags 12, size 14, tag 8, age 5
    mixed = len({p["repo"].split("/")[0] for p in prs}) > 1  # then each repo goes with its owner
    repo_w = repo_width([p["repo"] for p in prs], mixed)
    title_w = max(20, inner - 4 - 9 - repo_w - 28 - 24 - 16 - 12 - 14 - 8 - 5)
    now = time.time()
    lines, links = [], {}
    for i, pr in enumerate(prs):
        key = keys[i] if i < len(keys) else ""
        if key:
            links[key] = pr["url"]
        merged = pr["state"] == "MERGED"
        color = DIM if merged else MUTED if pr["draft"] else BRIGHT
        comments = ""
        if merged:
            status, flags = paint(PURPLE, f"{ICON['merge']} merged"), ""
            review = ""
        else:
            status, review = draft_text(pr) if pr["draft"] else ci_text(pr), approval_text(pr)
            if pr.get("unresolved"):
                comments = paint(YELLOW, f"{ICON['comment']} {pr['unresolved']} unresolved")
            if pr["conflicts"]:
                flags = paint(RED, "conflicts")
            elif pr["auto_merge"]:
                flags = paint(GREEN, "auto-merge")
            else:
                flags = ""
        tag = next((t for t in (tagger(pr, data) for tagger in taggers) if t), "")
        lines.append(
            fit(key_chip(key), 4)
            + fit(link(pr["url"], paint(DIM if merged else BLUE, f"{UNDERLINE}#{pr['number']}{NO_UNDERLINE}")), 9)
            + fit(repo_label(pr["repo"], mixed, DIM if merged else MUTED), repo_w - 2) + "  "
            + fit(link(pr["url"], paint(color, pr["title"])), title_w - 2) + "  "
            + fit(status, 28)
            + fit(review, 24)
            + fit(comments, 16)
            + fit(flags, 12)
            + fit(size_text(pr), 14)
            + fit(tag, 8)
            + fit(paint(DIM, ago(now - pr["at"])) if pr["at"] else "", 5)
        )
    return lines, links
