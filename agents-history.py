#!/usr/bin/python3
"""Find and resume any agent session (Claude Code, opencode, pi, Codex) with fzf.

Newest first; type to filter by title, folder, date or CLI. Enter goes to a live session's pane,
or resumes the session in a new tmux window in its folder (right here outside tmux). The
dashboard opens it in a popup with /.

Usage: agents-history.py [--print]   (--print shows the resume command instead of running it)
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time

import agent_sessions as sessions  # next to this file (Python puts a symlinked script's real folder on sys.path)
import dash_common
import dash_machine

CACHE_DIR = os.path.join(sessions.HOME, ".cache", "tmux-dash")
INDEX = os.path.join(CACHE_DIR, "session-index.json")  # what's been read of each session file
SNAPSHOT = os.path.join(CACHE_DIR, "history-snapshot.json")  # the listed sessions, for the preview

RESET, BOLD = "\033[0m", "\033[1m"


def fg(n):
    return f"\033[38;5;{n}m"


DIM, MUTED, TEXT, BRIGHT, GREEN = fg(240), fg(244), fg(248), fg(252), fg(108)


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def where(path):
    """A folder's short name, as the deck shows it: what a card calls it, ~ for home, else its name.
    The cards are only loaded the first time, so the preview, which doesn't need them, stays quick."""
    if not dash_common.CARDS:
        dash_common.load_cards()
    return dash_machine.place(path or "")


def when(ts, with_year=False):
    return time.strftime("%a %d %b %Y %H:%M" if with_year else "%a %d %b %H:%M", time.localtime(ts)) if ts else "?"


def tmux_agents():
    """The panes running an agent CLI, so sessions other than Claude's can be marked live too."""
    if not os.environ.get("TMUX"):
        return []
    found = []
    for line in run("tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}").splitlines():
        pane, cmd, cwd, title = (line.split("\t") + ["", "", ""])[:4]
        if cmd in ("opencode", "codex", "pi"):
            title = title[len("OC | "):] if title.startswith("OC | ") else title
            found.append({"tool": cmd, "pane": pane, "cwd": cwd, "title": title.strip()})
    return found


def all_sessions():
    sessions.load_index(INDEX)
    found = sessions.mark_live(sessions.sessions(since=0, opencode_limit=None), tmux_agents())
    sessions.save_index(INDEX)
    with open(SNAPSHOT + ".tmp", "w") as f:
        json.dump({f"{s['tool']}:{s['id']}": s for s in found}, f)
    os.replace(SNAPSHOT + ".tmp", SNAPSHOT)
    return found


def rows(found):
    """One line per session for fzf: tool and id (hidden fields), then what you see."""
    for s in found:
        tool = sessions.TOOLS[s["tool"]]
        live = f"  {GREEN}live{RESET}" if s.get("pane") else ""
        yield (
            f"{s['tool']}\t{s['id']}\t"
            f"{fg(tool['color'])}{tool['icon']}{RESET}  {DIM}{when(s['updated'])}{RESET}  "
            f"{BRIGHT if s.get('pane') else TEXT}{s['title']}{RESET}  {DIM}{where(s['cwd'])}{RESET}{live}"
        )


def preview(tool, sid):
    try:
        with open(SNAPSHOT) as f:
            s = json.load(f)[f"{tool}:{sid}"]
    except (OSError, ValueError, KeyError):
        print("(no details)")
        return
    t = sessions.TOOLS[tool]
    started = sessions.iso_ts(s.get("started"))
    print(f"{fg(t['color'])}{t['icon']}{RESET}  {BOLD}{BRIGHT}{s['title']}{RESET}")
    print(f"{DIM}{tool}  ·  {s['cwd'] or '?'}{'  ·  ' + GREEN + 'live' + DIM if s.get('pane') else ''}{RESET}")
    print(f"{DIM}started {when(started, True)}  ·  last {when(s['updated'], True)}{RESET}")
    print(f"{DIM}resume  {sessions.resume_command(s)}{RESET}")
    print()
    if tool == "claude":
        prompts = sessions.claude_prompts(sid)
        print(f"{MUTED}Your prompts ({len(prompts)}){' · last 12' if len(prompts) > 12 else ''}{RESET}")
        for ts, text in prompts[-12:]:
            text = " ".join(text.split())
            print(f"{DIM}{time.strftime('%d %b %H:%M', time.localtime(ts))}{RESET}  {text[:300]}")
    elif tool == "opencode":
        d = sessions.opencode_details(sid)
        if d:
            cost = f"  ·  ${d['cost']:.2f}" if d.get("cost") else ""
            print(f"{MUTED}{d.get('files') or 0} files  ·  +{d.get('additions') or 0} −{d.get('deletions') or 0}{cost}"
                  f"{'  ·  ' + d['model'] if d.get('model') else ''}{RESET}")
    elif s.get("first"):
        print(f"{MUTED}First prompt{RESET}")
        print(" ".join(s["first"].split())[:600])


def resume(s, print_only):
    command = sessions.resume_command(s)
    if print_only:
        print(f"cd {shlex.quote(s['cwd'] or '.')} && {command}")
        return
    in_tmux = bool(os.environ.get("TMUX"))
    if s.get("pane") and in_tmux:
        run("tmux", "switch-client", "-Z", "-t", s["pane"])  # it's open: go there, don't start a second one
        return
    binary = command.split()[0]
    if not shutil.which(binary):
        sys.exit(f"{binary} isn't installed")
    if not s["cwd"] or not os.path.isdir(s["cwd"]):
        sys.exit(f"{s['cwd']} doesn't exist any more")
    if not in_tmux:
        os.chdir(s["cwd"])
        os.execvp(binary, shlex.split(command))
    session = run("tmux", "display", "-p", "#{session_name}").strip()
    pane = run("tmux", "new-window", "-t", f"{session}:", "-n", f"{s['tool']}·{where(s['cwd'])}",
               "-c", s["cwd"], "-P", "-F", "#{pane_id}").strip()
    if pane.startswith("%"):
        # A window just created, so its shell is idle: type the command as you would
        run("tmux", "send-keys", "-t", pane, "-l", command)
        run("tmux", "send-keys", "-t", pane, "Enter")


def main():
    if sys.argv[1:2] == ["--preview"]:
        preview(sys.argv[2], sys.argv[3])
        return
    if not shutil.which("fzf"):
        sys.exit("agents-history needs fzf (brew install fzf)")
    found = all_sessions()
    by_key = {(s["tool"], s["id"]): s for s in found}
    picked = subprocess.run(
        [
            "fzf", "--ansi", "--no-sort", "--layout=reverse", "--delimiter=\t", "--with-nth=3..",
            f"--prompt={len(found)} sessions > ",
            "--header=enter: resume, or go to it if it's live  ·  esc: close",
            f"--preview={shlex.quote(sys.executable)} {shlex.quote(os.path.realpath(__file__))} --preview {{1}} {{2}}",
            "--preview-window=right,45%,wrap",
        ],
        input="\n".join(rows(found)), stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    if picked:
        tool, sid = picked.split("\t")[:2]
        resume(by_key[(tool, sid)], "--print" in sys.argv)


if __name__ == "__main__":
    main()
