"""The dashboard's settings (DECK_* overrides), tunables and the bits every part shares."""

import importlib
import json
import os
import pkgutil
import re
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor


HOME = os.path.expanduser("~")
# Where things live on this machine; override with DECK_* (e.g. in ~/.zshrc.local).
# Whatever doesn't exist is left out of the dashboard.
CACHE_DIR = os.path.join(HOME, ".cache", "tmux-dash")
ERRORS = os.path.join(CACHE_DIR, "errors.log")  # what broke, written there instead of printed over the deck


def config_path():
    """The deck config path; keep DASH_CONFIG as a fallback for existing shell setups."""
    value = os.environ.get("DECK_CONFIG") or os.environ.get("DASH_CONFIG") or "~/.config/deck/config.json"
    return os.path.expanduser(value)


CONFIG = config_path()  # which cards, in what order
CARD_ORDER = ("sessions", "machine", "my_prs", "inbox", "to_review")  # the cards in cards/, as they show by default
INTERVAL = 5  # seconds between redraws while visible
UNFOCUSED_INTERVAL = 30  # while visible but the terminal app isn't focused (maybe on another monitor)
HIDDEN_CHECK = 30  # with focus events, how often a hidden dashboard double-checks, in case one was missed
SLOW_CHECK = 30  # seconds between checks of what changes little and costs most, like ports and docker
FOCUS_IN, FOCUS_OUT = "\033[I", "\033[O"
GH_TTL = 300  # seconds before GitHub data is fetched again
MERGED_DAYS = 7  # merged PRs count this far back
MERGED_SHOWN = 5  # and this many of them are listed
MERGED_MAX = 100  # counted up to this many (a GitHub search page); more show as "100+"
LETTERS = "abcdefghijklmnopstuvwxyz"  # r and q are left out
KEY_PREFIXES = "wxyz"  # these start the two-letter keys (wa, wb...) for the rows the single letters don't reach
PR_KEYS = [k for k in LETTERS if k not in KEY_PREFIXES] + [p + k for p in KEY_PREFIXES for k in LETTERS]
REVIEW_ROWS = 8  # review requests listed when the window's height isn't known (--once without --height)
MIN_REVIEW_ROWS = 3  # To review takes the rows the other cards leave, and gives them up down to this many
BROADCAST_TEAMS = 3  # a request to more teams than this is CODEOWNERS fan-out, not a real ask
MARGIN = 4  # columns kept free on each side
SIDE_WIDTH = 52  # machine card, when there is room for two columns
SESSION_DAYS = 14  # how far back recent sessions go
HISTORY = os.path.join(os.path.dirname(os.path.realpath(__file__)), "agents-history.py")  # the / picker
PR_REVIEW = os.path.join(os.path.dirname(os.path.realpath(__file__)), "pr_review.py")  # a PR's review window
PANE = os.environ.get("TMUX_PANE", "")

EXEC = ThreadPoolExecutor(max_workers=16)
WAKE = None  # write end of the pipe that makes the main loop redraw now
NOTICE = ["", 0.0]  # footer message and until when to show it
CARDS = []  # the modules of the cards turned on, in the order they show (load_cards fills it)


def read_config():
    """The config, {} when there's none or it can't be read.

    It's {"cards": {"sessions": true, "machine": false, ...}}, a card name being its module in cards/."""
    try:
        with open(CONFIG) as f:
            config = json.load(f)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def card_choices():
    """Every card there is, in cards/ and in the cards folder next to the config, and whether it's on, in the
    order they show: the config's, then CARD_ORDER's, then the rest by name; on unless the config says no."""
    import cards  # here, not up top: the cards import this module
    found = {m.name for m in pkgutil.iter_modules(cards.__path__) if not m.name.startswith("_")}
    chosen = read_config().get("cards")
    chosen = chosen if isinstance(chosen, dict) else {}
    names = list(chosen) + [n for n in CARD_ORDER if n not in chosen] + sorted(found - set(chosen) - set(CARD_ORDER))
    return [(name, bool(chosen.get(name, True))) for name in names if name in found]  # a card that's gone is skipped


def load_cards():
    """The cards turned on into CARDS; one that doesn't load is left out, and why goes to ERRORS."""
    loaded = []
    for name, on in card_choices():
        if not on:
            continue
        try:
            loaded.append(importlib.import_module(f"cards.{name}"))
        except Exception:
            log_error(f"loading the {name} card")
            NOTICE[:] = [f"the {name} card didn't load (see {ERRORS.replace(HOME, '~')})", time.time() + 10]
    CARDS[:] = loaded


def log_error(what):
    """Add the exception being handled to ERRORS, after the time and what broke."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(ERRORS, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {what}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def save_cards(choices):
    """Keep in the config whether each card is on, from (card, on) pairs."""
    cards = read_config().get("cards")
    cards = cards if isinstance(cards, dict) else {}
    cards.update(choices)
    save_config({"cards": cards})


def save_config(changes):
    """Write these top-level keys into the config (None takes one out); anything else in it stays as it was."""
    config = read_config()
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG + ".tmp", "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(CONFIG + ".tmp", CONFIG)


def cards_with(name):
    """The cards that define a function called name themselves (one they import doesn't count), in order."""
    return [c for c in CARDS if getattr(getattr(c, name, None), "__module__", None) == c.__name__]


def run(*cmd, timeout=5):
    """Run a command and return its stdout, or "" if it fails or hangs."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def focus_pane(pane_id):
    """Take the client looking at this dashboard to that pane, its window and its session."""
    session = run("tmux", "display", "-p", "-t", PANE, "#{session_name}").strip()
    clients = [
        line.split(" ", 1)
        for line in run("tmux", "list-clients", "-t", session, "-F", "#{client_activity} #{client_name}").splitlines()
        if " " in line
    ]
    # -Z keeps the window zoomed if it was; no keys are ever sent to the pane
    cmd = ["tmux", "switch-client", "-Z", "-t", pane_id]
    if clients:
        cmd += ["-c", max(clients, key=lambda c: int(c[0]) if c[0].isdigit() else 0)[1]]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        failed = p.returncode != 0
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed:
        NOTICE[:] = ["that pane is gone", time.time() + 3]
    return not failed


NAME = (run("id", "-F").split() or [os.environ.get("USER", "")])[0]


def origin_repo(path):
    """owner/name of a checkout's GitHub origin, from its git config; "" if it has none."""
    url = run("git", "-C", path, "config", "--get", "remote.origin.url").strip()
    m = re.search(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


# Your main repo and its checkout, from the config ({"repo": "owner/name", "checkout": "~/code/name"}; the
# repo is the checkout's origin when only that is given): a PR's bare number is one of the repo's (repo#N
# one of its owner's), and its PRs are reviewed in the checkout.
def load_repo():
    """MAIN, REPO and ORG from the config; again whenever the settings change them."""
    global MAIN, REPO, ORG
    config = read_config()
    MAIN = os.path.expanduser(str(config.get("checkout") or ""))
    REPO = str(config.get("repo") or "") or (origin_repo(MAIN) if MAIN else "")
    ORG = REPO.split("/")[0]


load_repo()
ORGS = read_config().get("orgs")  # To review only looks in these organisations, when the config lists any
ORGS = [org for org in ORGS if isinstance(org, str)] if isinstance(ORGS, list) else []
# My PRs leaves out the repos you own (personal projects, like these dotfiles) with {"my_prs": {"own_repos": false}}
OWN_REPOS = read_config().get("my_prs")
OWN_REPOS = not (isinstance(OWN_REPOS, dict) and OWN_REPOS.get("own_repos") is False)
