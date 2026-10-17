"""Agent CLI sessions (Claude Code, opencode, pi, Codex), read from each CLI's own files.

Shared by dash.py (the Recent sessions card) and agents-history.py (the picker). Session
files are append-only, so each one is read incrementally: only the lines added since the
last read, and only the ones that can matter get parsed.
"""

import calendar
import glob
import json
import os
import sqlite3
import time

HOME = os.path.expanduser("~")
OPENCODE_DB = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
CLAUDE_HISTORY = os.path.join(HOME, ".claude", "history.jsonl")  # every prompt you typed, per session

TOOLS = {  # 256-colour code, icon and resume command per CLI
    # Material icons close to each logo: Codicons' own Claude and OpenAI logos are too new for most
    # Nerd Fonts (Ghostty's built-in symbols included) and show up as missing glyphs
    "claude": {"color": 173, "icon": "\U000f06c4", "resume": "claude --resume {id}"},  # asterisk, like the starburst
    "opencode": {"color": 110, "icon": "\U000f0169", "resume": "opencode --session {id}"},  # code braces
    "pi": {"color": 140, "icon": "\U000f03ff", "resume": "pi --session {id}"},  # π
    "codex": {"color": 108, "icon": "\U000f09f0", "resume": "codex resume {id}"},  # flower, like OpenAI's knot
}

JSONL_CACHE = {}  # path -> what's been read of a session file, and up to where
OPENCODE_CACHE = [None, []]  # (db and wal mtimes, since, limit), sessions


def resume_command(s):
    return TOOLS[s["tool"]]["resume"].format(id=s["id"])


# ── Reading files incrementally ──────────────────────────────────────────


def load_index(path):
    """Pick up what an earlier run read, so only new lines get read (the picker keeps it on disk)."""
    try:
        with open(path) as f:
            JSONL_CACHE.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_index(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(JSONL_CACHE, f)
    os.replace(path + ".tmp", path)


def read_new_lines(path, handle):
    """Call handle(cache, raw line) for each complete line added since the last read; return the cache."""
    size = os.path.getsize(path)
    cache = JSONL_CACHE.setdefault(path, {"offset": 0})
    if size < cache["offset"]:  # rewritten: start over
        cache.clear()
        cache["offset"] = 0
    if size > cache["offset"]:
        with open(path, "rb") as f:
            f.seek(cache["offset"])
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # still being written
                cache["offset"] += len(raw)
                handle(cache, raw)
    return cache


def parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text"))
    return ""


def first_prompt(c, message):
    text = message_text(message).strip()
    if text and not text.startswith("<"):  # skip command and hook wrappers
        c["first"] = text


# ── Per CLI ──────────────────────────────────────────────────────────────


def claude_line(c, raw):
    # Only lines that can matter get parsed: titles, and the first ones until cwd and prompt are known
    if b'"ai-title"' in raw or b'"custom-title"' in raw:
        d = parse(raw)
        if d.get("type") == "ai-title" and d.get("aiTitle"):
            c["title"] = d["aiTitle"]
        elif d.get("type") == "custom-title" and d.get("customTitle"):
            c["custom"] = d["customTitle"]  # set with /rename, wins over Claude's own title
    elif ("cwd" not in c or "first" not in c) and b'"cwd"' in raw:
        d = parse(raw)
        if "cwd" not in c and d.get("cwd"):
            c["cwd"] = d["cwd"]
            c["started"] = d.get("timestamp")
        if "first" not in c and d.get("type") == "user" and not d.get("isSidechain"):
            first_prompt(c, d.get("message"))


def claude_sessions(since):
    found = []
    for path in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            c = read_new_lines(path, claude_line)
        except OSError:
            continue
        if c.get("cwd"):
            found.append(
                {"tool": "claude", "id": os.path.basename(path)[: -len(".jsonl")],
                 "title": c.get("custom") or c.get("title") or c.get("first"), "first": c.get("first"),
                 "cwd": c["cwd"], "updated": os.path.getmtime(path), "started": c.get("started")}
            )
    return found


def opencode_sessions(since, limit):
    """Top-level, unarchived opencode sessions, straight from its SQLite (read-only)."""
    try:
        wal = OPENCODE_DB + "-wal"
        key = (os.path.getmtime(OPENCODE_DB), os.path.getmtime(wal) if os.path.exists(wal) else 0,
               int(since // 3600), limit)
    except OSError:
        return []
    if OPENCODE_CACHE[0] == key:
        return OPENCODE_CACHE[1]
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "select id, title, directory, time_updated, time_created from session"
            " where parent_id is null and time_archived is null and time_updated > ?"
            " order by time_updated desc limit ?",
            (since * 1000, limit if limit else -1),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return OPENCODE_CACHE[1]
    OPENCODE_CACHE[:] = [key, [
        {"tool": "opencode", "id": i, "title": t, "cwd": d, "updated": u / 1000, "started": c / 1000}
        for i, t, d, u, c in rows
    ]]
    return OPENCODE_CACHE[1]


def pi_line(c, raw):
    if "id" not in c and b'"session"' in raw:
        d = parse(raw)
        if d.get("type") == "session":
            c["id"], c["cwd"], c["started"] = d.get("id"), d.get("cwd"), d.get("timestamp")
    elif "first" not in c and b'"user"' in raw:
        d = parse(raw)
        if d.get("type") == "message" and (d.get("message") or {}).get("role") == "user":
            first_prompt(c, d["message"])


def codex_line(c, raw):
    # Codex rollouts, per its documented layout; untested until codex is installed here
    if "id" not in c and b'"session_meta"' in raw:
        payload = parse(raw).get("payload") or {}
        c["id"], c["cwd"], c["started"] = payload.get("id"), payload.get("cwd"), payload.get("timestamp")
    elif "first" not in c and b'"user"' in raw:
        payload = parse(raw).get("payload") or {}
        if payload.get("role") == "user":
            first_prompt(c, payload)


def jsonl_sessions(tool, pattern, handle, since):
    found = []
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < since:
                continue
            c = read_new_lines(path, handle)
        except OSError:
            continue
        if c.get("id"):
            found.append({"tool": tool, "id": c["id"], "title": c.get("first"), "first": c.get("first"),
                          "cwd": c.get("cwd"), "updated": os.path.getmtime(path), "started": c.get("started")})
    return found


def live_claude():
    """sessionId -> tmux pane of each Claude Code running now, from ~/.claude/sessions/<pid>.json."""
    live = {}
    for path in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            os.kill(int(d["pid"]), 0)  # still running?
        except (OSError, ValueError, KeyError, TypeError):
            continue
        pane = (d.get("tmux") or "").rsplit(".", 1)[-1]  # "0:@3.%8" -> "%8"
        live[d.get("sessionId")] = pane if pane.startswith("%") else ""
    return live


def sessions(since=0, opencode_limit=50):
    """Sessions of every CLI updated after `since`, newest first; Claude's running ones marked with their pane."""
    found = (
        claude_sessions(since)
        + opencode_sessions(since, opencode_limit)
        + jsonl_sessions("pi", os.path.join(HOME, ".pi", "agent", "sessions", "*", "*.jsonl"), pi_line, since)
        + jsonl_sessions("codex", os.path.join(HOME, ".codex", "sessions", "*", "*", "*", "rollout-*.jsonl"), codex_line, since)
    )
    live = live_claude()
    for s in found:
        s["title"] = " ".join((s["title"] or "(untitled)").split())
        s["pane"] = live.get(s["id"]) if s["tool"] == "claude" else None
    return sorted(found, key=lambda s: -s["updated"])


def mark_live(found, agents):
    """Link the other CLIs' sessions to the agent panes running them, then put live ones first.

    Only Claude keeps a record of its running sessions. For the rest, a session is live when a pane of
    that tool runs in its folder with its title; if no title matches, that folder's newest session."""
    for a in agents:
        if a["tool"] == "claude" or not a.get("cwd"):
            continue
        here = [
            s for s in found
            if s["tool"] == a["tool"] and not s["pane"] and s["cwd"]
            and os.path.realpath(s["cwd"]) == os.path.realpath(a["cwd"])
        ]
        pick = next((s for s in here if s["title"] == a["title"]), here[0] if here else None)
        if pick:
            pick["pane"] = a["pane"]
    return sorted(found, key=lambda s: (s["pane"] is None, -s["updated"]))


# ── Details, for the picker's preview ────────────────────────────────────


def claude_prompts(session_id):
    """(time, text) of each prompt you typed in a Claude session, from ~/.claude/history.jsonl."""
    needle = session_id.encode()
    found = []
    try:
        with open(CLAUDE_HISTORY, "rb") as f:
            for raw in f:
                if needle in raw:
                    d = parse(raw)
                    if d.get("sessionId") == session_id and d.get("display"):
                        found.append((d.get("timestamp", 0) / 1000, d["display"]))
    except OSError:
        pass
    return found


def opencode_details(session_id):
    """Files changed, lines added and removed, and cost of an opencode session."""
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1)
        row = con.execute(
            "select summary_files, summary_additions, summary_deletions, cost, model from session where id = ?",
            (session_id,),
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    d = dict(zip(("files", "additions", "deletions", "cost", "model"), row))
    try:  # the model is stored as JSON: {"id": "gpt-5.3-codex", "providerID": ...}
        d["model"] = json.loads(d["model"]).get("id") or d["model"]
    except (TypeError, ValueError, AttributeError):
        pass
    return d


def iso_ts(value):
    """Epoch seconds from an ISO timestamp or epoch number, None if neither."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else value
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))  # these are UTC
    except (TypeError, ValueError):
        return None
