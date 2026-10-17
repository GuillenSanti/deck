#!/usr/bin/python3
"""Home dashboard for tmux window 0.

Shows its cards: the agents running in tmux (the ones waiting on you first), your PRs, the reviews
waiting for you, and the machine's resources, plus any of your own. ~/.config/deck/config.json picks the cards and their order, and , turns them on and off
from here. It only redraws while its window is on screen, and caches the GitHub data because that
goes over the network.

Keys: a-z (or a click on a PR number or title) opens that PR, a click on an agent goes to its pane,
r refreshes GitHub now, Ctrl-C quits; the footer lists the cards' own keys.
Usage: dash.py [--once [--width N] [--height N]]   (--once prints a single frame and exits)

This file lays the frame out, handles clicks and keys, and runs the loop. Each card is a module in
cards/ (cards/__init__.py says what a card has); the rest lives next to it: dash_common (settings
and shared bits), dash_ui (colours, widths, cards), dash_machine (agents, sessions, CPU, memory,
docker) and dash_github (PRs, CI, reviews).
"""

import datetime
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

import dash_common
import dash_settings
from dash_common import (
    CARDS, EXEC, FOCUS_IN, FOCUS_OUT, HIDDEN_CHECK, INTERVAL, KEY_PREFIXES, MARGIN, NAME, NOTICE, PANE,
    PR_KEYS, SIDE_WIDTH, SLOW_CHECK, UNFOCUSED_INTERVAL, cards_with, load_cards, log_error, run,
)
from dash_ui import BLUE, BOLD, BRIGHT, DIM, ICON, RED, RESET, ago, card, fit, link_regions, paint, width
from dash_machine import agents
from dash_github import STORES


LINKS = {}  # key -> PR url, from the last frame drawn
REGIONS = []  # (row, first column, end column, (card, target)) of what can be clicked in the last frame
TARGETS = []  # the cards' own, like agent rows and review buttons; links (card None) come from the lines
CLICK_SETTLE = 0.4  # act this long after the click, so a double click can't reach the pane we go to
MOUSE = re.compile(r"\033\[<(\d+);(\d+);(\d+)([Mm])")  # SGR mouse report
SLOW = {}  # the last value of each slow source, and when they were read ("at")
LOGGED = set()  # what has broken already, so a card that keeps breaking fills ERRORS only once
BROKEN = {"lines": [paint(RED, "this card broke, see ~/.cache/tmux-dash/errors.log")]}
CHORD = ["", 0.0]  # the first letter of a two-letter key typed, and when
CHORD_WAIT = 3  # seconds the second letter has


def visible():
    if not PANE:
        return True
    out = run("tmux", "display", "-p", "-t", PANE, "#{window_active} #{session_attached}").split()
    return len(out) != 2 or (out[0] == "1" and out[1] != "0")


def broke(what, e):
    """Log what broke, the first time it does."""
    if (what, repr(e)) not in LOGGED:
        LOGGED.add((what, repr(e)))
        log_error(what)


def call(card, hook, *args, default=None):
    """card.hook(*args), or default when it breaks: a card going wrong doesn't take the deck with it."""
    try:
        return getattr(card, hook)(*args)
    except Exception as e:
        broke(f"the {card.__name__.split('.')[-1]} card's {hook}", e)
        return default


def result(name, job):
    """A source's value, or None when it broke."""
    try:
        return job.result()
    except Exception as e:
        broke(f"reading {name}", e)
        return None


def collect(force_gh=False):
    """Everything a frame shows: the agents, and what the cards read. Their slow sources (like ports and
    docker) are read every SLOW_CHECK seconds (r reads them now): they take over half a frame's CPU,
    and a service just started shows as starting meanwhile."""
    fast, slow = {"agents": agents}, {}
    for c in CARDS:
        fast.update(getattr(c, "SOURCES", {}))
        slow.update(getattr(c, "SLOW_SOURCES", {}))
    due = force_gh or time.time() - SLOW.get("at", 0) >= SLOW_CHECK or any(name not in SLOW for name in slow)
    jobs = {name: EXEC.submit(fn) for name, fn in {**fast, **(slow if due else {})}.items()}
    data = {name: result(name, job) for name, job in jobs.items()}
    if due:
        SLOW.update({name: data[name] for name in slow}, at=time.time())
    data.update({name: SLOW[name] for name in slow})
    for c in cards_with("refresh"):
        call(c, "refresh", data, force_gh)
    return data


def alerts(data):
    """The cards' chips for the greeting line, the most urgent card's first: (chip, (card, target) or None)."""
    out = []
    for c in sorted(cards_with("alerts"), key=lambda c: getattr(c, "ALERT_ORDER", 9)):
        for alert in call(c, "alerts", data, default=[]):
            text, target = alert if isinstance(alert, tuple) else (alert, None)
            out.append((text, (c, target) if target else None))
    return out


def header(w, alerts=()):
    """The greeting and what wants you now (as many alerts as fit, most urgent first; tmux's status bar has
    the date); and (first column, end column, target) of the alerts you can click."""
    now = datetime.datetime.now()
    greet = "Good morning" if 5 <= now.hour < 12 else "Good afternoon" if 12 <= now.hour < 18 else "Good evening"
    left = f"{BLUE}{ICON['home']}{RESET}   {BRIGHT}{BOLD}{greet}, {NAME}{RESET}"
    alerts = list(alerts)
    while alerts and width(left) + 6 + width(" ".join(c for c, _ in alerts)) > w:
        alerts.pop()
    targets = []
    for i, (c, target) in enumerate(alerts):
        left += ("      " if i == 0 else " ")
        if target:
            targets.append((width(left), width(left) + width(c), target))
        left += c
    return left, targets


def footer(data, w, shown):
    """When the frame was drawn and GitHub read, any notice, and the keys of the cards shown."""
    times = [t for t in (call(c, "fetched_at", data) for c in cards_with("fetched_at")) if t]
    at = min(times) if times else None
    if at is None:
        gh = "GitHub loading…"
    elif time.time() - at < 60:
        gh = "GitHub up to date"
    else:
        gh = f"GitHub {ago(time.time() - at)} ago"
    left = paint(DIM, f"updated {datetime.datetime.now():%H:%M:%S}  ·  {gh}")
    if NOTICE[0] and time.time() < NOTICE[1]:
        left += f"  {DIM}·{RESET}  {BLUE}{NOTICE[0]}{RESET}"
    hints = [c.HINT for c in shown if getattr(c, "HINT", "")] + [",  settings", "r  refresh", "ctrl-c  quit"]
    right = paint(DIM, "  ·  ".join(hints))
    return left + " " * max(1, w - width(left) - width(right)) + right


def card_targets(c, view, x, cw):
    """The card's targets in its row, as (line, first column, end column, (card, target)): the card starts x
    columns in and is cw wide, its lines 2 columns into it (after "│ "); a target without columns spans it."""
    return [
        (1 + li, x if a is None else x + 2 + a, x + cw if b is None else x + 2 + b, (c, t))
        for li, a, b, t in view.get("targets", ())
    ]


def card_row(group, data, w, rows, keys):
    """A card, or a card and the SIDE card next to it: (lines, targets, links, cards shown); None when none
    shows. rows is how many lines a single card may take (None: as many as it needs)."""
    if len(group) == 2:
        main, side = group
        side_view = call(side, "render", data, SIDE_WIDTH - 4, None, "", default=BROKEN)
        if side_view is None:
            return card_row([main], data, w, rows, keys)
        main_w = w - SIDE_WIDTH - 2
        body = max(call(main, "min_rows", data, default=0) if main in cards_with("min_rows") else 0, len(side_view["lines"]))
        main_view = call(main, "render", data, main_w - 4, body, keys, default=BROKEN)
        if main_view is None:
            return card_row([side], data, w, rows, keys)
        h = max(len(main_view["lines"]), len(side_view["lines"])) + 2  # both as tall, with their borders
        left = card(main.TITLE, main.TITLE_ICON, BLUE, main_view["lines"], main_w, h, main_view.get("note", ""))
        right = card(side.TITLE, side.TITLE_ICON, BLUE, side_view["lines"], SIDE_WIDTH, h, side_view.get("note", ""))
        targets = card_targets(main, main_view, 0, main_w) + card_targets(side, side_view, main_w + 2, SIDE_WIDTH)
        return ["  ".join(parts) for parts in zip(left, right)], targets, main_view.get("links", {}), group
    c = group[0]
    view = call(c, "render", data, w - 4, rows, keys, default=BROKEN)
    if view is None:
        return None
    lines = card(c.TITLE, c.TITLE_ICON, BLUE, view["lines"], w, note=view.get("note", ""))
    return lines, card_targets(c, view, 0, w), view.get("links", {}), [c]


def unused(keys, piece):
    """The keys left once a row of cards (None when it doesn't show) has taken its own."""
    return [k for k in keys if k not in piece[2]] if piece else keys


def render(cols, rows=None, force_gh=False):
    """The frame's lines; with rows, the FLEX card (To review) takes what the others leave, so that everything fits."""
    data = collect(force_gh)
    w = max(60, cols - 2 * MARGIN)  # as wide as the window
    pad = max(0, (cols - w) // 2)
    top, top_targets = header(w, alerts(data))
    out = ["", top, ""]
    TARGETS[:] = [(1, pad + a, pad + b, t) for a, b, t in top_targets]
    groups = []  # the rows of cards: a SIDE card goes next to the one before it when there's room
    for c in CARDS:
        beside = groups and len(groups[-1]) == 1 and not getattr(groups[-1][0], "FLEX", False)
        if getattr(c, "SIDE", False) and w >= 110 and beside:
            groups[-1].append(c)
        else:
            groups.append([c])
    flex = next((i for i, g in enumerate(groups) if getattr(g[0], "FLEX", False)), None)
    pieces, keys = [None] * len(groups), PR_KEYS
    for i in range(len(groups)):  # all but the FLEX card, which takes the lines they leave
        if i != flex:
            pieces[i] = card_row(groups[i], data, w, None, keys)
            keys = unused(keys, pieces[i])
    if flex is not None:
        room = None
        if rows:  # the window less the greeting, the other cards, a blank line between cards, the footer
            others = [p for p in pieces if p]  # and its own borders
            room = rows - len(out) - sum(len(p[0]) for p in others) - len(others) - 2 - 2
        # Letters go in reading order: the cards above it, then it, then the ones below
        keys = PR_KEYS
        for piece in pieces[:flex]:
            keys = unused(keys, piece)
        pieces[flex] = card_row(groups[flex], data, w, room, keys)
        keys = unused(keys, pieces[flex])
        for i in range(flex + 1, len(groups)):
            if pieces[i] and pieces[i][2]:  # it took letters: again, with the ones left after the FLEX card
                pieces[i] = card_row(groups[i], data, w, None, keys)
            keys = unused(keys, pieces[i])
    LINKS.clear()
    shown = []
    for piece in pieces:
        if piece is None:
            continue
        lines, targets, links, cards_in = piece
        if shown:
            out.append("")
        TARGETS.extend((len(out) + li, pad + a, pad + b, t) for li, a, b, t in targets)
        out += lines
        LINKS.update(links)
        shown += cards_in
    if rows:  # the footer at the bottom of the window, however little the cards take
        out += [""] * (rows - len(out) - 2)
    out += ["", footer(data, w, shown)]
    return [" " * pad + fit(line, w) for line in out]


def draw(clear=False, force_gh=False):
    cols, rows = shutil.get_terminal_size()
    try:
        lines = render(cols, rows, force_gh)
    except Exception as e:  # keep the window alive; show what broke
        lines = ["", f"  {RED}dash.py: {type(e).__name__}: {e}{RESET}"]
    lines = lines[:rows]  # no line reaches the last column, so the bottom row doesn't scroll
    if dash_settings.OPEN[0]:  # over everything, and the only thing to click
        lines, regions = dash_settings.over(lines, cols, rows)
        REGIONS[:] = [(r, a, b, (dash_settings, t)) for r, a, b, t in regions]
    else:
        links = [(r, a, b, (None, url)) for r, a, b, url in link_regions(lines)]
        REGIONS[:] = links + [t for t in TARGETS if t[0] < len(lines)]
    frame = ("\033[2J" if clear else "") + "\033[H" + "\n".join(l + "\033[K" for l in lines) + "\033[J"
    sys.stdout.write(frame)
    sys.stdout.flush()


def resolve(key):
    """The key to act on. The first letter of a two-letter key (wa, wb...) waits for the next one, and then
    both go, in capitals when either was (review the PR here); a key that isn't a letter drops it."""
    first, CHORD[0] = CHORD[0], ""
    if first and time.time() - CHORD[1] < CHORD_WAIT:
        NOTICE[:] = ["", 0.0]
        if not key.isalpha():
            return None
        both = first + key
        return both.upper() if not both.islower() else both
    if key.lower() in KEY_PREFIXES and not dash_settings.OPEN[0] and any(k[0] == key.lower() for k in LINKS if len(k) == 2):
        CHORD[:] = [key, time.time()]
        NOTICE[:] = [f"{key}…", time.time() + CHORD_WAIT]
        return None
    return key


def act(target):
    """A click: a link opens in the browser, anything else goes to the card it belongs to."""
    owner, t = target
    if owner is None:
        open_url(t)
    else:
        call(owner, "act", t)


def open_url(url):
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    NOTICE[:] = [f"opening #{url.rsplit('/', 1)[-1]}", time.time() + 3]


def main():
    load_cards()
    if "--once" in sys.argv:
        cols = int(sys.argv[sys.argv.index("--width") + 1]) if "--width" in sys.argv else shutil.get_terminal_size().columns
        rows = int(sys.argv[sys.argv.index("--height") + 1]) if "--height" in sys.argv else None
        render(cols, rows)  # starts the PR fetch for stale branches
        deadline = time.time() + 30
        while any(store.busy for store in STORES) and time.time() < deadline:
            time.sleep(0.2)
        print("\n".join(render(cols, rows)))
        return

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    wake_r, dash_common.WAKE = os.pipe()
    resized = threading.Event()

    def on_resize(*_):
        resized.set()
        os.write(dash_common.WAKE, b"w")

    signal.signal(signal.SIGWINCH, on_resize)
    tty.setcbreak(fd)
    # tmux tells the pane when it gains or loses focus (window switched, terminal app
    # unfocused, client detached), so a hidden dashboard waits instead of polling tmux
    focus_events = bool(PANE) and run("tmux", "show", "-gv", "focus-events").strip() == "on"
    # alternate screen, hide cursor, report mouse clicks (SGR coordinates), and focus changes
    sys.stdout.write("\033[?1049h\033[?25l\033[?1000h\033[?1006h" + ("\033[?1004h" if focus_events else ""))
    sys.stdout.flush()
    try:
        shown, focused, next_check, force_gh = False, True, 0.0, False
        pending = None  # (when, target) of a click waiting out a possible double click
        while True:
            if time.time() >= next_check:
                if visible():
                    draw(clear=resized.is_set() or not shown, force_gh=force_gh)
                    resized.clear()
                    shown, force_gh = True, False
                    next_check = time.time() + (INTERVAL if focused else UNFOCUSED_INTERVAL)
                else:
                    shown = False  # redraw as soon as the window is back on screen
                    next_check = time.time() + (HIDDEN_CHECK if focus_events else 1)
            timeout = max(0.0, next_check - time.time())
            if pending is not None:
                timeout = min(timeout, max(0.0, pending[0] - time.time()))
            ready, _, _ = select.select([fd, wake_r], [], [], timeout)
            if wake_r in ready:
                os.read(wake_r, 64)
                next_check = 0.0
            if fd in ready:
                typed = os.read(fd, 256).decode(errors="ignore")
                gained, lost = typed.rfind(FOCUS_IN), typed.rfind(FOCUS_OUT)
                if gained > lost:
                    focused, next_check = True, 0.0  # back on it: draw now
                elif lost > gained:
                    focused = False  # the next check tells hidden from just unfocused
                typed = typed.replace(FOCUS_IN, "").replace(FOCUS_OUT, "")
                for button, x, y, kind in MOUSE.findall(typed):
                    if button != "0":  # left button only; wheel and others are ignored
                        continue
                    if pending:  # the rest of a double click: absorb it and wait again
                        pending = (time.time() + CLICK_SETTLE, pending[1])
                    elif kind == "m":  # act on release, so the press and release both land here
                        target = next((t for r, a, b, t in REGIONS if r == int(y) - 1 and a <= int(x) - 1 < b), None)
                        if target:
                            pending = (time.time() + CLICK_SETTLE, target)
                for key in MOUSE.sub("", typed):
                    key = resolve(key)
                    if key is None:  # half of a two-letter key, or one dropped
                        next_check = 0.0
                    elif dash_settings.key(key):  # while they're open, every key is theirs (r too, to type it)
                        next_check = 0.0
                    elif key in ("r", "R"):
                        force_gh, next_check = True, 0.0
                        NOTICE[:] = ["refreshing GitHub…", time.time() + 3]
                    elif any(call(c, "key", key, default=False) for c in cards_with("key")):  # the first that takes it
                        next_check = 0.0
                    elif key in LINKS:
                        open_url(LINKS[key])
                        next_check = 0.0
            if pending and time.time() >= pending[0]:
                act(pending[1])
                pending, next_check = None, 0.0
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?1004l\033[?1006l\033[?1000l\033[?25h\033[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
