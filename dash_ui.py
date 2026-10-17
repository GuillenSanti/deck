"""Text layout for the dashboard: colours, icons, widths, cards, chips and gauges."""

import re
import unicodedata

import agent_sessions  # next to this file (Python puts a symlinked script's real folder on sys.path)


RESET, BOLD = "\033[0m", "\033[1m"


def fg(n):
    return f"\033[38;5;{n}m"


def bg(n):
    return f"\033[48;5;{n}m"


# The greys match the tmux status bar (colour235-252)
DIM, MUTED, TEXT, BRIGHT, LINE = fg(240), fg(244), fg(248), fg(252), fg(238)
BLUE, GREEN, YELLOW, RED, PURPLE = fg(110), fg(108), fg(179), fg(167), fg(140)
CHIP = bg(236)

# Nerd Font Material Design icons; done/ask are the ones attention.sh sets
ICON = {
    "home": "\U000f02dc",
    "robot": "\U000f06a9",
    "branch": "\U000f062c",
    "pull": "\U000f04c2",
    "draft": "\uf4dd",  # GitHub's draft PR
    "merge": "\U000f062d",
    "docker": "\U000f0868",
    "disk": "\U000f02ca",
    "memory": "\U000f035b",
    "cpu": "\U000f061a",
    "check": "\U000f012c",
    "cross": "\U000f0156",
    "sand": "\U000f051f",
    "board": "\U000f056e",
    "tunnel": "\U000f0318",
    "chart": "\U000f0128",
    "review": "\U000f0208",
    "history": "\U000f02da",
    "console": "\U000f018d",  # review a PR in the terminal
    "diff": "\uf4d2",  # a checkout's diff with main
    "comment": "\U000f017e",
    "done": "\U000f009e",
    "ask": "\U000f02d7",
}

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]|\033\]8;[^\033]*\033\\")  # colours and OSC 8 links
LINK_END = "\033]8;;\033\\"
OSC8 = re.compile(r"\033\]8;;([^\033]*)\033\\")
UNDERLINE, NO_UNDERLINE = "\033[4m", "\033[24m"


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s):
    return sum(char_width(c) for c in ANSI.sub("", s))


def fit(s, w):
    """Pad or cut s, which may hold colour codes, to exactly w columns."""
    if w <= 0:
        return ""
    n = width(s)
    if n <= w:
        return s + " " * (w - n)
    out, used, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        cw = char_width(s[i])
        if used + cw > w - 1:
            break
        out.append(s[i])
        used += cw
        i += 1
    return "".join(out) + "…" + RESET + LINK_END + " " * (w - used - 1)


def paint(color, s):
    return f"{color}{s}{RESET}"


def link(url, s):
    """OSC 8 hyperlink; tmux passes it through with the hyperlinks feature."""
    return f"\033]8;;{url}\033\\{s}{LINK_END}"


def link_regions(lines):
    """Where each link landed on screen, so a click can open it."""
    found = []
    for row, line in enumerate(lines):
        col, i, url, start = 0, 0, "", 0
        while i < len(line):
            m = OSC8.match(line, i)
            if m:
                if url:
                    found.append((row, start, col, url))
                url, start, i = m.group(1), col, m.end()
                continue
            m = ANSI.match(line, i)
            if m:
                i = m.end()
                continue
            col += char_width(line[i])
            i += 1
        if url:
            found.append((row, start, col, url))
    return found


def repo_label(repo, mixed, color):
    """A repo's name, after its owner (dimmer) when the list it's in mixes owners: owner/name."""
    owner, _, name = repo.rpartition("/")
    return f"{DIM}{owner}/{RESET}{paint(color, name)}" if mixed and owner else paint(color, name)


def repo_width(repos, mixed):
    """The repo column for these repos: as wide as the longest label, up to 16 (32 with owners), and a gap."""
    return min(32 if mixed else 16, max(len(r if mixed else r.rpartition("/")[2]) for r in repos)) + 2


def key_chip(key):
    """A row's key: " a ", or "wa " with its first letter dimmer, so the last one lines up with the single
    letters; "" for a row without one."""
    if len(key) == 2:
        return f"{CHIP}{MUTED}{key[0]}{TEXT}{key[1]} {RESET}"
    return f"{CHIP}{TEXT} {key} {RESET}" if key else ""


def chip(color, text):
    return f"{CHIP}{color} {text} {RESET}"


def gauge(frac, w, color):
    n = max(0, min(w, round(frac * w)))
    return f"{color}{'━' * n}{LINE}{'─' * (w - n)}{RESET}"


def level(frac, warn, bad):
    return RED if frac >= bad else YELLOW if frac >= warn else GREEN


def card(title, icon, color, lines, w, height=0, note=""):
    """Rounded box of exactly w columns, padded to height lines if given."""
    head = f"{LINE}╭─{RESET} {color}{icon}{RESET}  {BRIGHT}{BOLD}{title}{RESET}"
    if note:
        room = w - width(head) - 5  # two spaces before the note, one after, and at least "─╮"
        head += f"  {DIM}{fit(note, room) if width(note) > room else note}{RESET}"
    head += " "
    head += f"{LINE}{'─' * max(0, w - width(head) - 1)}╮{RESET}"
    body = [f"{LINE}│{RESET} {fit(line, w - 4)} {LINE}│{RESET}" for line in lines]
    while len(body) < height - 2:
        body.append(f"{LINE}│{RESET}{' ' * (w - 2)}{LINE}│{RESET}")
    return [head] + body + [f"{LINE}╰{'─' * (w - 2)}╯{RESET}"]


def ago(seconds):
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def size(n):
    g = n / 1024**3
    return f"{g:.1f}G" if g < 10 else f"{g:.0f}G"


def tool_style(tool):
    """(colour, icon) of a CLI; one the dashboard doesn't know gets the robot."""
    t = agent_sessions.TOOLS.get(tool)
    return (fg(t["color"]), t["icon"]) if t else (DIM, ICON["robot"])


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"
