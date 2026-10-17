#!/bin/zsh
# Links the deck's commands into ~/.tmux, where tmux, the tmuxp layout and the shell look for them

deck_dir="$(cd "$(dirname "$0")" && pwd)"

# Check every destination first: don't replace personal commands or links from another install.
for script in dash.py agents-history.py pr_review.py branch_diff.py; do
    destination="$HOME/.tmux/$script"
    if [ -L "$destination" ]; then
        if [ "$(readlink "$destination")" != "$deck_dir/$script" ]; then
            echo "install: $destination already links elsewhere; move it aside before installing deck" >&2
            exit 1
        fi
    elif [ -e "$destination" ]; then
        echo "install: $destination already exists; move it aside before installing deck" >&2
        exit 1
    fi
done

mkdir -p "$HOME/.tmux"
for script in dash.py agents-history.py pr_review.py branch_diff.py; do
    ln -sfn "$deck_dir/$script" "$HOME/.tmux/$script"
done
echo "Linked the deck into ~/.tmux"

# Check what's committed here with gitleaks (.githooks/pre-commit), once it's a git checkout
if git -C "$deck_dir" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$deck_dir" config core.hooksPath .githooks
fi
