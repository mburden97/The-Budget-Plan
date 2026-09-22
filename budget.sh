#!/bin/sh
# Launcher for Linux and macOS (Windows has Budget.cmd, Arch users can also install the
# PKGBUILD and run `budget`).
#
# Keeps a virtual environment in ${XDG_DATA_HOME:-~/.local/share}/thebudgetplan/venv,
# reinstalls it whenever pyproject.toml changes, then starts the app. A file named DEV_MODE
# next to this script starts dev mode (see README.md).
set -eu

CDPATH='' cd -- "$(dirname -- "$0")"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/thebudgetplan/venv"
STAMP="$VENV/pyproject.installed"
PYTHON="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
    printf 'First run: creating a Python environment in %s ...\n' "$VENV"
    "$PYTHON" -m venv "$VENV" || {
        echo 'Setup failed. Install Python 3.12+ (on Arch: pacman -S python).' >&2
        exit 1
    }
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
fi

if ! cmp -s pyproject.toml "$STAMP"; then
    echo 'Installing Python packages ...'
    "$VENV/bin/python" -m pip install --quiet -e . || exit 1
    cp pyproject.toml "$STAMP"
fi

if [ -e DEV_MODE ]; then
    exec "$VENV/bin/python" -m budgetapp --dev "$@"
fi
exec "$VENV/bin/python" -m budgetapp "$@"
