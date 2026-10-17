"""Sessions: the agents at work in tmux, the ones waiting on you first, then your newest other sessions."""

import os
import shlex
import shutil
import subprocess
import sys
import time

import agent_sessions
from dash_common import HISTORY, NOTICE, PANE, SESSION_DAYS, focus_pane, run
from dash_machine import place, recent_sessions
from dash_ui import BRIGHT, CHIP, DIM, GREEN, ICON, MUTED, RESET, TEXT, YELLOW, ago, chip, fit, paint, tool_style


TITLE, TITLE_ICON = "Sessions", ICON["robot"]
SOURCES = {"sessions": recent_sessions}
ALERT_ORDER = 1
HINT = "/  sessions"
SEEN = {}  # (tool, id) -> session, from the last frame, for clicks


def render(data, inner, rows=None, keys=""):
    ags = data["agents"]
    recent = agent_sessions.mark_live(data["sessions"], ags)
    if rows is None:  # every agent and up to 5 other sessions, then the legend
        lines, targets = session_lines(ags, recent, inner, len(ags) + 5)
        lines.append(session_legend())
    else:  # as tall as the card next to it, with the legend pinned to the bottom
        lines, targets = session_lines(ags, recent, inner, rows - 1)
        lines += [""] * (rows - 1 - len(lines)) + [session_legend()]
    SEEN.clear()
    SEEN.update({(t["tool"], t["id"]): t for t in targets if not isinstance(t, str)})
    waiting = sum(1 for a in ags if a["state"])
    note = (
        f"{len(ags)} live" + (f" · {waiting} {'needs' if waiting == 1 else 'need'} you" if waiting else "")
        + f" · {len(recent)} in {SESSION_DAYS} days · click to go or resume"
    )
    targets = [
        (i, None, None, t if isinstance(t, str) else f"session:{t['tool']}:{t['id']}") for i, t in enumerate(targets)
    ]
    targets.append((len(lines) - 1, None, None, "history"))  # the legend opens the picker
    return {"lines": lines, "note": note, "targets": targets}


def min_rows(data):
    """Every agent, and the legend."""
    return len(data["agents"]) + 1


def alerts(data):
    ags = data["agents"]
    out = []
    ask = sum(1 for a in ags if a["state"] == "ask")
    done = sum(1 for a in ags if a["state"] == "done")
    if ask:
        out.append(chip(YELLOW, f"{ICON['ask']} {ask} asking"))
    if done:
        out.append(chip(GREEN, f"{ICON['done']} {done} done"))
    return out


def key(k):
    if k == "/":
        open_history()
        return True
    return False


def act(target):
    if target == "history":
        open_history()
    elif target.startswith("pane:"):
        focus_pane(target[len("pane:"):])
    elif target.startswith("session:"):
        _, tool, sid = target.split(":", 2)
        resume_session(tool, sid)


def session_lines(ags, recent, inner, rows):
    """The agents at work first (their state, and the window they're in), then the newest of the other
    sessions (where, and age). Also each row's click target: the agent's pane, or the session to resume."""
    by_pane = {x["pane"]: x for x in recent if x.get("pane")}
    title_w = inner - 5 - 10 - 1 - 18
    now, lines, targets = time.time(), [], []
    for a in ags:
        color, icon = tool_style(a["tool"])
        # live: a dot while it works, or what it wants from you
        state = {"ask": paint(YELLOW, ICON["ask"]), "done": paint(GREEN, ICON["done"])}.get(a["state"], paint(GREEN, "●"))
        title = (by_pane.get(a["pane"]) or {}).get("title") or a["title"] or "—"  # its /rename title when it has one
        win = f"{CHIP}{TEXT} {a['label']} {RESET} {MUTED}{a['window']}{RESET}"
        lines.append(
            f"{color}{icon}{RESET} {state}  " + fit(paint(BRIGHT, title), title_w - 1) + " "
            + fit(paint(DIM, a["where"]), 10) + " " + fit(win, 18)
        )
        targets.append(f"pane:{a['pane']}")
    for x in [x for x in recent if not x.get("pane")][: max(0, rows - len(lines))]:
        color, icon = tool_style(x["tool"])
        lines.append(
            f"{color}{icon}{RESET}    " + fit(paint(TEXT, x["title"]), title_w - 1) + " "
            + fit(paint(DIM, place(x["cwd"] or "")), 10) + " " + fit(paint(DIM, ago(now - x["updated"])), 18)
        )
        targets.append(x)
    if not lines:
        return [paint(DIM, f"No agents at work, and no sessions in the last {SESSION_DAYS} days")], []
    return lines, targets


def session_legend():
    tools = "  ".join(f"{tool_style(name)[0]}{tool_style(name)[1]}{RESET} {DIM}{name}{RESET}" for name in agent_sessions.TOOLS)
    return (
        f"{GREEN}●{RESET} {DIM}live{RESET}  {YELLOW}{ICON['ask']}{RESET} {DIM}asking{RESET}  {GREEN}{ICON['done']}{RESET} {DIM}done{RESET}"
        f"{DIM}  ·  {RESET}{tools}{DIM}  ·  / all sessions{RESET}"
    )


def resume_session(tool, sid):
    """Go to a running session's pane, or resume it in a new window in its folder."""
    s = SEEN.get((tool, sid))
    if s is None:
        return
    if s.get("pane"):
        focus_pane(s["pane"])
        return
    command = agent_sessions.resume_command(s)
    binary = command.split()[0]
    if not shutil.which(binary):
        NOTICE[:] = [f"{binary} isn't installed", time.time() + 4]
        return
    if not s["cwd"] or not os.path.isdir(s["cwd"]):
        NOTICE[:] = [f"{s['cwd']} doesn't exist any more", time.time() + 4]
        return
    session = run("tmux", "display", "-p", "-t", PANE, "#{session_name}").strip()
    name = f"{tool}·{place(s['cwd'])}"
    pane = run("tmux", "new-window", "-t", f"{session}:", "-n", name, "-c", s["cwd"], "-P", "-F", "#{pane_id}").strip()
    if not pane.startswith("%"):
        NOTICE[:] = ["couldn't open a window", time.time() + 4]
        return
    # A window just created, so its shell is idle: type the command as you would
    run("tmux", "send-keys", "-t", pane, "-l", command)
    run("tmux", "send-keys", "-t", pane, "Enter")


def open_history():
    """The agents-history picker (every session of every CLI) in a tmux popup over the dashboard."""
    subprocess.Popen(
        ["tmux", "display-popup", "-E", "-w", "90%", "-h", "85%", "-T", " agent sessions ",
         f"{shlex.quote(sys.executable)} {shlex.quote(HISTORY)}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
