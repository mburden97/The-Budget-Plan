"""Command-line entry point: `budget` / `python -m budgetapp`."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from pathlib import Path

from budgetapp import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="budget", description="Personal encrypted budget tracker (local only)."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("BUDGET_DATA_DIR", "data")),
        help="folder holding budget.vault and backups/ (default: ./data or $BUDGET_DATA_DIR)",
    )
    parser.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument(
        "--idle-minutes", type=int, default=15, help="auto-lock after inactivity (default: 15)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    return serve(args)


def serve(args: argparse.Namespace) -> int:
    import waitress

    from budgetapp.store import Store
    from budgetapp.web import create_app

    data_dir = args.data_dir.resolve()
    store = Store(data_dir / "budget.vault", backup_dir=data_dir / "backups")
    app = create_app(store, port=args.port)

    stop = threading.Event()
    idle_seconds = args.idle_minutes * 60

    def idle_watch() -> None:
        while not stop.wait(20):
            store.lock_if_idle(idle_seconds)

    threading.Thread(target=idle_watch, name="idle-lock", daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Budget {__version__}")
    print(f"  data:  {data_dir}")
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
