#!/usr/bin/python3
"""What a checkout changes from main, in diffnav as a PR's diff is shown: the branch's commits plus
what isn't committed yet, new files included. Read-only: it never touches the index or the branch.

Usage: branch_diff.py                      (in the checkout: shows its diff)
       branch_diff.py --window PATH NAME   (opens a tmux window called NAME in PATH that shows it,
                                            or goes to it: a card can open one)
"""

import os
import shlex
import shutil
import subprocess
import sys

import pr_review  # next to this file: its tmux helpers and diffnav's config

BASES = ("origin/main", "origin/master", "main", "master")
DIM, RESET = pr_review.DIM, pr_review.RESET


def git(*args):
    """git's output, whatever its exit code (diff --no-index exits 1 when the files differ)."""
    # --no-optional-locks: agents may be working in this checkout, so don't refresh its index
    p = subprocess.run(["git", "--no-optional-locks", *args], capture_output=True, text=True)
    return p.stdout


def base():
    """Where the branch left main: the diff starts there, as a PR's does."""
    for ref in BASES:
        if git("rev-parse", "--verify", "-q", ref).strip():
            return git("merge-base", ref, "HEAD").strip() or None, ref
    return None, None


def diff(color):
    start, ref = base()
    if not start:
        return None, ref
    flag = "--color=always" if color else "--color=never"
    text = git("diff", flag, start)  # from where it left main to the files as they are now
    for path in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if path:  # new files git doesn't track yet
            text += git("diff", flag, "--no-index", "--", "/dev/null", path)
    return text, ref


def show():
    root = git("rev-parse", "--show-toplevel").strip()
    if root:
        os.chdir(root)  # so new files are found, and named, across the whole checkout
    viewer = shutil.which("diffnav")
    text, ref = diff(color=not viewer)
    if text is None:
        print("This isn't a git checkout, or it has no main branch to compare with")
        return 1
    if not text.strip():
        print(f"{DIM}Nothing changed from {ref}{RESET}")
        return 0
    if viewer:
        subprocess.run([viewer], input=text, text=True, env=dict(os.environ, DIFFNAV_CONFIG_DIR=pr_review.DIFFNAV_CONFIG))
    else:  # n / N jump between files
        subprocess.run(["less", "-R", "-p", "^diff --git"], input=text, text=True)
    return 0


def window(path, name):
    """Go to the window called name, or open it in path showing the diff (after q, ↑ Enter reloads it)."""
    session = pr_review.tmux("display", "-p", "#{session_name}")
    if session is None:
        return "This needs to run inside tmux"
    for line in (pr_review.tmux("list-windows", "-t", f"{session}:", "-F", "#{window_id}\t#{window_name}") or "").splitlines():
        win, _, wname = line.partition("\t")
        if wname == name:
            pr_review.tmux("select-window", "-t", win)
            return None
    if not os.path.isdir(path):
        return f"{path} doesn't exist"
    pane = pr_review.tmux("new-window", "-t", f"{session}:", "-n", name, "-c", path, "-P", "-F", "#{pane_id}")
    if not pane:
        return "Couldn't open a tmux window"
    pr_review.tmux("set-option", "-w", "-t", pane, "automatic-rename", "off")
    pr_review.type_in(pane, shlex.quote(os.path.realpath(__file__)))
    return None


def main():
    if sys.argv[1:2] == ["--window"] and len(sys.argv) == 4:
        error = window(sys.argv[2], sys.argv[3])
        if error:
            pr_review.tmux("display-message", error)  # the dashboard starts it in the background
            sys.exit(1)
        return
    if len(sys.argv) > 1:
        sys.exit(__doc__.split("\n\n")[-1])
    sys.exit(show())


if __name__ == "__main__":
    main()
