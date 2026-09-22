"""Command-line entry point: `budget` / `python -m budgetapp`."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from pathlib import Path

from budgetapp import __version__

PORT = 8766
DEV_PORT = 8767
APP_DIR = "TheBudgetPlan" if os.name == "nt" else "thebudgetplan"


def user_data_dir() -> Path:
    """Where an installed copy keeps its data: XDG on Linux, %APPDATA% on Windows."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIR


def default_data_dir(*, dev: bool = False) -> Path:
    """The folder holding budget.vault and backups/.

    A source checkout keeps its data beside the code, which is what the launchers use. An
    installed `budget`, run from wherever, uses the user's data folder instead of dropping a
    data/ folder in the current directory. $BUDGET_DATA_DIR overrides both, except in dev
    mode, which always stays on its own data-dev folder.
    """
    name = "data-dev" if dev else "data"
    env = os.environ.get("BUDGET_DATA_DIR")
    if env and not dev:
        return Path(env)
    if Path("pyproject.toml").is_file() or Path(name).is_dir():
        return Path(name)
    return user_data_dir() / name


def main(argv: list[str] | None = None) -> int:
    return serve(parse_args(argv))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="budget", description="Personal encrypted budget tracker (local only)."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="folder holding budget.vault and backups/ (default: ./data in a checkout or "
        "$BUDGET_DATA_DIR, else your user data folder; data-dev with --dev)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help=f"localhost port (default: {PORT}; {DEV_PORT} with --dev)",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="for developing the app: a separate data-dev vault that opens with no passphrase. "
        "Use made-up data only.",
    )
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument(
        "--idle-minutes", type=int, default=15, help="auto-lock after inactivity (default: 15)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.port is None:
        args.port = DEV_PORT if args.dev else PORT
    if args.data_dir is None:
        args.data_dir = default_data_dir(dev=args.dev)
    return args


def prepare_dev_vault(store) -> bool:
    """Dev mode: create a passphrase-less vault with the starter lines, if there isn't one."""
    from budgetapp import planning

    if store.exists:
        return False
    store.create("")
    with store.write() as conn:
        planning.add_starter_lines(conn)
    store.lock()  # the first page view opens it again, the normal way
    return True


def serve(args: argparse.Namespace) -> int:
    import waitress

    from budgetapp.store import Store
    from budgetapp.web import create_app

    data_dir = args.data_dir.resolve()
    store = Store(data_dir / "budget.vault", backup_dir=data_dir / "backups")
    if args.dev:
        prepare_dev_vault(store)
    app = create_app(store, port=args.port, dev=args.dev)

    stop = threading.Event()
    idle_seconds = args.idle_minutes * 60

    def idle_watch() -> None:
        while not stop.wait(20):
            store.lock_if_idle(idle_seconds)

    threading.Thread(target=idle_watch, name="idle-lock", daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Budget {__version__}")
    print(f"  data:  {data_dir}")
    if args.dev:
        print("  mode:  DEV. This vault has no passphrase: use made-up data only.")
    print(f"  open:  {url}")
    print("  Ctrl+C to stop (the vault is saved after every change).")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        # Bind to loopback only; never expose on the network.
        waitress.serve(app, host="127.0.0.1", port=args.port, threads=4, ident="budget")
    except OSError as exc:
        print(f"Could not start on port {args.port}: {exc}")
        return 1
    finally:
        stop.set()
        store.lock()
    return 0
