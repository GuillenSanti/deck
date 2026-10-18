"""The settings: , opens them over the deck, and a setting's key or a click turns it on or off, or types in it.

Which cards show, whether My PRs has the PRs in your own repos too, which organisations To review keeps
to, and your main repo and its checkout, each with what it's for and where it comes from. It's a box drawn
over the cards, with the rest dimmed, that takes every key and click while it's open. What it changes is
saved in the config and applies at once.
"""

import importlib
import os
import re
import time

import dash_common
from dash_common import CONFIG, NOTICE, ORGS, card_choices, load_cards, read_config, save_cards, save_config
from dash_github import MY_ORGS, REVIEWS, reviews_key
from pr_review import PROMPT, REVIEW_AGENTS
from dash_ui import (
    ANSI, BLUE, BRIGHT, DIM, GREEN, ICON, MUTED, RED, RESET, YELLOW, card, char_width, fit, key_chip, paint, width,
)


TITLE, TITLE_ICON = "Settings", ICON["board"]
HINT = "a setting's key or a click changes it  ·  , or esc closes"
TYPING = "type, then enter saves it  ·  empty clears it  ·  esc leaves it as it was"
KEYS = "123456789abcdefghijklmnopqstuvwxyz"  # r is left out: it refreshes the deck (gitleaks:allow, not a key)
OPEN = [False]  # , opens and closes it
EDIT = ["", ""]  # the setting being typed in (repo, checkout or org) and what's typed so far
PROMPT_CURSOR = [0]
SELECT_AGENT = [""]
MARKS = {"on": paint(GREEN, "●"), "off": paint(DIM, "○"), "text": paint(MUTED, "✎"), "add": paint(MUTED, "+")}


def sections():
    """[(heading, what it's for and where it comes from, [(target, state, title, value)])], a state being
    on or off for what turns on and off, text for what's typed in, and add for what adds one."""
    cards = []
    for name, on in card_choices():
        state = "on" if on else "off"
        try:
            module = importlib.import_module(f"cards.{name}")
        except Exception:  # load_cards says why when it's turned on
            broken = paint(RED, "doesn't load, see ~/.cache/tmux-dash/errors.log")
            cards.append((f"card:{name}", state, name, broken))
            continue
        about = (module.__doc__ or "").strip().split("\n")[0].split(": ", 1)[-1].rstrip(".")
        cards.append((f"card:{name}", state, getattr(module, "TITLE", name), about))
    orgs = MY_ORGS + [org for org in ORGS if org not in MY_ORGS]
    config = read_config()
    repo = config.get("repo") or ""
    review = config.get("review")
    review = review if isinstance(review, dict) else {}
    agent = review.get("agent") or "claude (default)"
    prompt = "custom" if review.get("prompt") else "default"
    if not repo and dash_common.REPO:
        repo = f"{dash_common.REPO} {DIM}(the checkout's origin){BRIGHT}"
    return [
        ("Cards", ["Which cards show, in this order. Your own go in ~/.config/deck/cards/."], cards),
        ("My PRs", ["Your PRs come from a GitHub search of the ones you opened, in any repo."], [
            ("own_repos", "on" if dash_common.OWN_REPOS else "off", "Own repos",
             "the ones in repos you own too, like your dotfiles"),
        ]),
        ("To review", [
            "Your organisations, as GitHub lists them with your review requests. The ticked ones keep",
            "To review to their requests; with none ticked it shows every one. Add others by name.",
        ], [(f"org:{org}", "on" if org in ORGS else "off", org, "") for org in orgs] + [
            ("edit:org", "add", "Add", "an organisation, by its name on GitHub"),
        ]),
        ("Reviewing in the terminal (+ or prefix R)", [
            "A bare number, like 4821, is a PR of this repo, and reviews open in this checkout so Claude",
            "can read the code around them. With no repo, the checkout's origin is used.",
        ], [
            ("edit:repo", "text", "Repo", repo),
            ("edit:checkout", "text", "Checkout", config.get("checkout") or ""),
            ("edit:review_agent", "text", "Review agent", agent),
            ("edit:review_prompt", "text", "Review prompt", prompt),
        ]),
    ]


def body():
    """The box's lines, (line, target) for each setting, and key -> target."""
    if SELECT_AGENT[0]:
        lines = ["Choose the agent for terminal reviews:", ""]
        targets, keys = [], {}
        current = SELECT_AGENT[0]
        for i, agent in enumerate(REVIEW_AGENTS, 1):
            key = str(i)
            keys[key] = f"agent:{agent}"
            lines.append(f"{key_chip(key)}" + (paint(GREEN, "●") if agent == current else paint(DIM, "○"))
                         + f"  {agent}")
            targets.append((len(lines) - 1, f"agent:{agent}"))
        lines += ["", paint(DIM, "enter saves  ·  esc cancels  ·  click or press 1–4")]
        return lines, targets, keys
    if EDIT[0] == "review_prompt":
        text = EDIT[1]
        cursor = min(PROMPT_CURSOR[0], len(text))
        before = text[:cursor]
        line_no, col = before.count("\n"), len(before.rsplit("\n", 1)[-1])
        content = text.split("\n")
        content[line_no] = content[line_no][:col] + paint(YELLOW, "▏") + content[line_no][col:]
        lines = [paint(MUTED, "Edit the prompt sent to the review agent."), ""]
        lines += [fit(line, 100) for line in content]
        lines += ["", paint(DIM, "arrows move  ·  enter adds a line  ·  ctrl-o saves  ·  esc cancels")]
        return lines, [], {}
    lines, targets, keys = [""], [], {}
    for heading, about, settings in sections():
        if targets:
            lines.append("")
        lines.append(paint(MUTED, heading))
        lines += [paint(DIM, line) for line in about]
        for target, state, title, value in settings:
            key = KEYS[len(targets)] if len(targets) < len(KEYS) else ""
            if key:
                keys[key] = target
            mark = MARKS[state]
            if target == f"edit:{EDIT[0]}":  # being typed in
                mark, value = paint(YELLOW, "✎"), f"{BRIGHT}{EDIT[1]}{YELLOW}▏{RESET}"
            elif state == "text":
                value = paint(BRIGHT, value) if value else paint(DIM, "not set")
            else:
                value = paint(DIM, value)
            title = fit(paint(DIM if state == "off" else BRIGHT, title), 14)
            lines.append(fit(key_chip(key), 4) + mark + "  " + title + value)
            targets.append((len(lines) - 1, target))
    return lines + ["", paint(DIM, TYPING if EDIT[0] else HINT)], targets, keys


def columns(s, start, end=None):
    """The part of a line without colours between two screen columns (or to its end), padded to end."""
    out, col = [], 0
    for ch in s:
        cw = char_width(ch)
        if col >= start and (end is None or col + cw <= end):
            out.append(ch)
        elif col < start < col + cw or (end is not None and col < end < col + cw):
            out.append(" ")  # the half of a wide character that falls on this side
        col += cw
    if end is not None and col < end:
        out.append(" " * (end - max(col, start)))
    return "".join(out)


def over(lines, cols, rows):
    """The frame dimmed, with the settings in the middle; and (row, first column, end column, target) to
    click: a setting's line turns it on or off or types in it, and anywhere outside the box closes it."""
    inner, targets, _ = body()
    w = min(cols - 4, max(width(line) for line in inner) + 6)  # the borders, and a margin at the right
    title = "Review agent" if SELECT_AGENT[0] else "Edit review prompt" if EDIT[0] == "review_prompt" else TITLE
    box = card(title, TITLE_ICON, BLUE, inner, w)
    top, left = max(0, (rows - len(box)) // 2), max(0, (cols - w) // 2)
    plain = [ANSI.sub("", line) for line in lines] + [""] * max(0, top + len(box) - len(lines))
    out = [paint(DIM, line) for line in plain]
    for i, line in enumerate(box):
        row = plain[top + i]
        out[top + i] = paint(DIM, columns(row, 0, left)) + line + paint(DIM, columns(row, left + w))
    inside = range(top, top + len(box))
    regions = [(top + 1 + li, left, left + w, target) for li, target in targets]  # 1: the top border
    regions += [(r, 0, cols, "close") for r in range(len(out)) if r not in inside]
    regions += [(r, a, b, "close") for r in inside for a, b in ((0, left), (left + w, cols))]
    return out[:rows], regions


def key(k):
    """, opens and closes them. While they're open they take every key: while typing in a setting, the keys
    type; else a setting's own turns it on or off or starts typing in it, and esc closes them."""
    if not OPEN[0]:
        if k != ",":
            return False
        OPEN[0] = True
        if not MY_ORGS:  # the review requests' query is what finds your organisations
            REVIEWS.refresh([reviews_key()], True)
    elif SELECT_AGENT[0]:
        if k == "\033":
            SELECT_AGENT[0] = ""
        elif k in ("\r", "\n"):
            act(f"agent:{SELECT_AGENT[0]}")
        elif k in ("\x1b[A", "\x1b[D"):
            SELECT_AGENT[0] = REVIEW_AGENTS[(REVIEW_AGENTS.index(SELECT_AGENT[0]) - 1) % len(REVIEW_AGENTS)]
        elif k in ("\x1b[B", "\x1b[C"):
            SELECT_AGENT[0] = REVIEW_AGENTS[(REVIEW_AGENTS.index(SELECT_AGENT[0]) + 1) % len(REVIEW_AGENTS)]
        elif k in body()[2]:
            act(body()[2][k])
    elif EDIT[0] == "review_prompt":
        prompt_input(k)
    elif EDIT[0]:
        type_in(k)
    elif k in (",", "\033"):
        OPEN[0] = False
        EDIT[:] = ["", ""]
        SELECT_AGENT[0] = ""
    elif k in body()[2]:
        act(body()[2][k])
    return True


def type_in(k):
    if k == "\033":
        EDIT[:] = ["", ""]
    elif k in ("\r", "\n"):
        why = save(EDIT[0], EDIT[1].strip())
        if why:  # keep typing
            NOTICE[:] = [why, time.time() + 5]
        else:
            EDIT[:] = ["", ""]
    elif k in ("\x7f", "\b"):
        EDIT[1] = EDIT[1][:-1]
    elif k.isprintable():
        EDIT[1] += k


def prompt_input(k):
    """Edit the prompt in the settings modal; arrows move through the existing text."""
    text, cursor = EDIT[1], min(PROMPT_CURSOR[0], len(EDIT[1]))
    if k == "\033":
        EDIT[:] = ["", ""]
    elif k == "\x0f":  # ctrl-o
        why = save("review_prompt", text)
        if why:
            NOTICE[:] = [why, time.time() + 5]
        else:
            EDIT[:] = ["", ""]
    elif k in ("\r", "\n"):
        EDIT[1] = text[:cursor] + "\n" + text[cursor:]
        PROMPT_CURSOR[0] = cursor + 1
    elif k in ("\x7f", "\b"):
        if cursor:
            EDIT[1] = text[:cursor - 1] + text[cursor:]
            PROMPT_CURSOR[0] = cursor - 1
    elif k in ("\x1b[D", "\x02"):
        PROMPT_CURSOR[0] = max(0, cursor - 1)
    elif k in ("\x1b[C", "\x06"):
        PROMPT_CURSOR[0] = min(len(text), cursor + 1)
    elif k in ("\x1b[H", "\x01"):
        PROMPT_CURSOR[0] = text.rfind("\n", 0, cursor) + 1
    elif k in ("\x1b[F", "\x05"):
        end = text.find("\n", cursor)
        PROMPT_CURSOR[0] = len(text) if end < 0 else end
    elif k in ("\x1b[A", "\x10", "\x1b[B", "\x0e"):
        current_line = text.rfind("\n", 0, cursor) + 1
        column = cursor - current_line
        if k in ("\x1b[A", "\x10") and current_line:
            previous_end = current_line - 1
            previous_start = text.rfind("\n", 0, previous_end) + 1
            PROMPT_CURSOR[0] = min(previous_start + column, previous_end)
        elif k in ("\x1b[B", "\x0e"):
            next_start = text.find("\n", cursor)
            if next_start >= 0:
                next_start += 1
                next_end = text.find("\n", next_start)
                PROMPT_CURSOR[0] = min(next_start + column, len(text) if next_end < 0 else next_end)
    elif k.isprintable():
        EDIT[1] = text[:cursor] + k + text[cursor:]
        PROMPT_CURSOR[0] = cursor + len(k)


def save(field, text):
    """Save what was typed in a setting; why not, when it can't be."""
    try:
        if field == "repo":
            if text and not re.fullmatch(r"[\w.-]+/[\w.-]+", text):
                return "a repo is owner/name, as on GitHub"
            save_config({"repo": text or None})
            dash_common.load_repo()
        elif field == "checkout":
            if text and not os.path.isdir(os.path.expanduser(text)):
                return f"there's no folder at {text}"
            save_config({"checkout": text or None})
            dash_common.load_repo()
        elif field in ("review_agent", "review_prompt"):
            review = read_config().get("review")
            review = dict(review) if isinstance(review, dict) else {}
            key = field[len("review_"):]
            if key == "agent" and text:
                text = text.lower()
                if text not in REVIEW_AGENTS:
                    return f"choose a review agent: {', '.join(REVIEW_AGENTS)}"
            if key == "prompt" and text == PROMPT:
                text = ""
            if text:
                review[key] = text
            else:
                review.pop(key, None)
            save_config({"review": review or None})
        elif field == "org" and text:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", text):
                return "an organisation's name on GitHub is letters, digits and dashes"
            if text not in ORGS:
                save_config({"orgs": ORGS + [text]})
                ORGS.append(text)  # ticked: To review keeps to it, and the ones ticked already
    except OSError as e:
        return f"couldn't save {CONFIG}: {e.strerror}"
    return None


def act(target):
    kind, _, what = target.partition(":")
    EDIT[:] = ["", ""]  # a click elsewhere leaves what was being typed
    try:
        if kind == "close":
            OPEN[0] = False
            EDIT[:] = ["", ""]
            SELECT_AGENT[0] = ""
        elif kind == "edit":
            config = read_config()
            if what in ("repo", "checkout"):
                value = config.get(what)
            elif what in ("review_agent", "review_prompt"):
                review = config.get("review")
                review = review if isinstance(review, dict) else {}
                value = review.get(what[len("review_"):])
            else:
                value = ""
            if what == "review_agent":
                SELECT_AGENT[0] = value if value in REVIEW_AGENTS else "claude"
            else:
                if what == "review_prompt" and not value:
                    value = PROMPT
                EDIT[:] = [what, str(value or "")]
                if what == "review_prompt":
                    PROMPT_CURSOR[0] = 0
        elif kind == "agent":
            save("review_agent", what)
            SELECT_AGENT[0] = ""
        elif kind == "card":
            save_cards([(name, not on if name == what else on) for name, on in card_choices()])
            load_cards()
        elif kind == "own_repos":
            mine = read_config().get("my_prs")
            on = not dash_common.OWN_REPOS
            save_config({"my_prs": {**(mine if isinstance(mine, dict) else {}), "own_repos": on}})
            dash_common.OWN_REPOS = on  # My PRs reads that list, or fetches it when it hasn't yet
        elif kind == "org":
            orgs = [org for org in ORGS if org != what] if what in ORGS else ORGS + [what]
            save_config({"orgs": orgs})
            ORGS[:] = orgs  # To review reads that list, or fetches it when it hasn't yet
    except OSError as e:
        NOTICE[:] = [f"couldn't save {CONFIG}: {e.strerror}", time.time() + 5]
