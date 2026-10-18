#!/usr/bin/python3
"""Review a pull request in its own tmux window: its diff on the left, an AI agent on the right.

The window is called "review #N" ("review repo#N" outside the main checkout's repo). Claude's session id comes
from the PR's URL, so opening the same PR again resumes that review, or goes to it while it's
open. A review you started yourself, with the PR's URL in a prompt asking for a review, is
resumed too. The dashboard opens it from To review, and asks for a PR with --ask.

Usage: pr_review.py <url | number | repo#number | owner/repo#number>
       pr_review.py --ask   (asks for the PR first, in a popup: the dashboard's +, or prefix R)
"""

import glob
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import tty
import uuid

import agent_sessions  # next to this file (Python puts a symlinked script's real folder on sys.path)

import dash_common  # its MAIN, REPO and ORG: the main checkout, its GitHub repo and that repo's owner

HOME = agent_sessions.HOME
CODE = os.path.join(HOME, "code")  # other repos are looked for here, by name
DIFFNAV_CONFIG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "diffnav")
ASKS_REVIEW = re.compile(r"review|revis", re.I)  # a prompt asking for a review, in English or Spanish
ESCAPE_SEQUENCE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.)")  # arrows and other keys that aren't text
DIM, RESET = "\033[38;5;240m", "\033[0m"

PROMPT = """Review pull request {url} and structure your response as follows:

1. Purpose: what problem the change solves or what it aims to do.
2. Approach: how it works, the main changes, and why they are made this way.
3. Blockers: bugs, data or security risks, and missing tests that should be fixed before merging, with file and line.
4. Improvements: non-blocking suggestions, also with file and line.
5. Verdict: whether it is safe to approve, needs changes, or only needs a comment, and why.

Read the PR with gh (gh pr view, gh pr diff). Do not check out its branch or switch branches in this checkout: other tmux windows use it. Do not post comments, approve, or request changes without showing me the text first."""
REVIEW_AGENTS = ("claude", "codex", "opencode", "pi")


def run(*cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def tmux(*args):
    return run("tmux", *args)


def parse(text):
    """(owner/repo, number) from a PR URL, owner/repo#N, repo#N (in REPO's org) or N (in REPO); None if
    it isn't one, or it's a bare number or repo without a main checkout to say whose."""
    text = text.strip()
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"(?:([\w.-]+)/)?([\w.-]+)#(\d+)", text)
    if m and (m.group(1) or dash_common.ORG):
        return f"{m.group(1) or dash_common.ORG}/{m.group(2)}", int(m.group(3))
    m = re.fullmatch(r"#?(\d+)", text)
    if m and dash_common.REPO:
        return dash_common.REPO, int(m.group(1))
    return None


def window_name(repo, number):
    return f"review #{number}" if repo == dash_common.REPO else f"review {repo.split('/')[-1]}#{number}"


def clone(repo):
    """The local checkout the review agent reads; None if absent."""
    if repo == dash_common.REPO and os.path.isdir(dash_common.MAIN):
        return dash_common.MAIN
    path = os.path.join(CODE, repo.split("/")[-1])
    return path if os.path.exists(os.path.join(path, ".git")) else None


def session_file(sid):
    return next(iter(glob.glob(os.path.join(HOME, ".claude", "projects", "*", f"{sid}.jsonl"))), None)


def session_cwd(path):
    """The folder a Claude session ran in: --resume only finds it from there."""
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"cwd"' in raw:
                    cwd = agent_sessions.parse(raw).get("cwd")
                    if cwd:
                        return cwd
    except OSError:
        pass
    return None


def review_settings():
    """The configured review agent and prompt, with safe defaults."""
    settings = dash_common.read_config().get("review")
    settings = settings if isinstance(settings, dict) else {}
    agent = settings.get("agent", "claude")
    if not isinstance(agent, str) or agent not in REVIEW_AGENTS:
        choices = ", ".join(REVIEW_AGENTS)
        raise ValueError(f"Unknown review agent {agent!r}; choose one of: {choices}")
    prompt = settings.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = PROMPT
    return agent, prompt


def review_prompt(template, url, repo, number):
    """Fill PR placeholders without interpreting braces in custom prompt text."""
    return template.replace("{url}", url).replace("{repo}", repo).replace("{number}", str(number))


def agent_command(agent, prompt, sid, name):
    """A safely quoted interactive command for a supported agent CLI."""
    commands = {
        "claude": ["claude", "--session-id", sid, "-n", name, prompt],
        "codex": ["codex", prompt],
        "opencode": ["opencode", "--prompt", prompt],
        "pi": ["pi", prompt],
    }
    return shlex.join(commands[agent])


def earlier_review(url, sid):
    """(session id, folder) of a Claude review of this PR to resume, or None.

    First the one started from here, whose id comes from the URL; then the newest session where you
    asked for a review with the PR's URL in the prompt (~/.claude/history.jsonl, oldest first)."""
    path = session_file(sid)
    if path and session_cwd(path):
        return sid, session_cwd(path)
    found, needle = None, url.encode()
    try:
        with open(agent_sessions.CLAUDE_HISTORY, "rb") as f:
            for raw in f:
                if needle in raw:
                    d = agent_sessions.parse(raw)
                    if ASKS_REVIEW.search(d.get("display") or "") and d.get("sessionId") and d.get("project"):
                        found = (d["sessionId"], d["project"])
    except OSError:
        pass
    return found if found and session_file(found[0]) else None


def type_in(pane, command):
    # A pane just created, so its shell is idle: type the command as you would
    tmux("send-keys", "-t", pane, "-l", command)
    tmux("send-keys", "-t", pane, "Enter")


def review(repo, number):
    """Open the PR's review window, or go to it; an error message if that can't be done."""
    try:
        agent, prompt_template = review_settings()
    except ValueError as e:
        return str(e)
    if not shutil.which(agent):
        return f"{agent} isn't installed or isn't on PATH"
    info = run("gh", "pr", "view", str(number), "-R", repo, "--json", "url")
    if not info:
        return f"Couldn't find {repo}#{number} on GitHub"
    url = json.loads(info)["url"]
    name = window_name(repo, number)
    session = tmux("display", "-p", "#{session_name}")
    if session is None:
        return "This needs to run inside tmux"
    for line in (tmux("list-windows", "-t", f"{session}:", "-F", "#{window_id}\t#{window_name}") or "").splitlines():
        window, _, wname = line.partition("\t")
        if wname == name:
            tmux("select-window", "-t", window)
            return None
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    earlier = earlier_review(url, sid) if agent == "claude" else None
    if earlier:
        sid, cwd = earlier
        if os.path.realpath(cwd) == os.path.realpath(HOME):
            return f"The earlier review of {repo}#{number} ran from your home folder; close it and clone the repo before reopening it"
        live = agent_sessions.live_claude().get(sid)
        if live:  # open in another window: go there instead of resuming it twice
            tmux("switch-client", "-Z", "-t", live)
            return None
        if not os.path.isdir(cwd):
            return f"The review of {repo}#{number} ran in {cwd}, which doesn't exist any more"
        command = f"claude --resume {sid}"
    else:
        cwd = clone(repo)
        if not cwd:
            return f"Clone {repo} into {os.path.join(CODE, repo.split('/')[-1])} before starting its {agent} review"
        prompt = review_prompt(prompt_template, url, repo, number)
        command = agent_command(agent, prompt, sid, name)
    # diffnav shows it like GitHub: a file tree and each file's diff (without it, less: n / N jump
    # between files). After q, ↑ Enter reloads it.
    if shutil.which("diffnav"):
        diff = f"gh pr diff {number} -R {repo} --color=never | DIFFNAV_CONFIG_DIR={shlex.quote(DIFFNAV_CONFIG)} diffnav"
    else:
        diff = f"gh pr diff {number} -R {repo} --color=always | less -R -p '^diff --git'"
    left = tmux("new-window", "-t", f"{session}:", "-n", name, "-c", cwd, "-P", "-F", "#{pane_id}")
    if not left:
        return "Couldn't open a tmux window"
    tmux("set-option", "-w", "-t", left, "automatic-rename", "off")
    right = tmux("split-window", "-h", "-t", left, "-c", cwd, "-l", "50%", "-P", "-F", "#{pane_id}")
    type_in(left, diff)
    if right:
        type_in(right, command)
    tmux("select-pane", "-t", left)
    return None


def read_line(prompt):
    """What's typed at the prompt up to Enter, or None for Esc or ctrl-c (raw mode, so Esc can close)."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    text = ""
    try:
        tty.setraw(fd)
        while True:
            chunk = os.read(fd, 1024).decode(errors="ignore")
            # Esc alone is a key; followed by more at once, it starts an arrow's or another key's sequence
            if chunk == "\x1b" and not select.select([fd], [], [], 0.05)[0]:
                return None
            for ch in ESCAPE_SEQUENCE.sub("", chunk):
                if ch in "\r\n":
                    return text
                if ch == "\x03" or (ch == "\x04" and not text):  # ctrl-c, or ctrl-d on an empty line
                    return None
                if ch in "\x7f\x08":
                    if text:
                        text = text[:-1]
                        sys.stdout.write("\b \b")
                elif ch == "\x15":  # ctrl-u: start over
                    sys.stdout.write("\b \b" * len(text))
                    text = ""
                elif ch >= " ":
                    text += ch
                    sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")
        sys.stdout.flush()


def ask():
    print(f"A PR's URL, its number in {dash_common.REPO}, or repo#number" if dash_common.REPO else "A PR's URL, or owner/repo#number")
    print(f"{DIM}Enter opens its review  ·  Esc closes{RESET}\n")
    text = read_line("PR to review › ")
    if not text or not text.strip():
        return
    target = parse(text)
    if target:
        print(f"{DIM}Opening {window_name(*target)}…{RESET}")
    error = review(*target) if target else f"That doesn't look like a PR: {text.strip()}"
    if error:
        print(f"\n{error}")
        read_line(f"{DIM}Enter or Esc to close{RESET}")


def main():
    if sys.argv[1:2] == ["--ask"]:
        ask()
        return
    target = parse(" ".join(sys.argv[1:]))
    if not target:
        sys.exit(__doc__.split("\n\n")[-1])
    error = review(*target)
    if error:
        tmux("display-message", error)  # the dashboard starts it in the background
        sys.exit(1)


if __name__ == "__main__":
    main()
