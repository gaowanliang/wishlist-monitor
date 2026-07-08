from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

for relative_path in ("app", "core", "storage", "integrations"):
    module_path = PROJECT_ROOT / relative_path
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from config import load_config
from db import Database
from server import AppState, run_web
from steam import SteamClient
from worker import backfill_all_games, polling_loop, start_thread

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(argv)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if config.config_path:
        log.info("Config file: %s", config.config_path)
    else:
        log.warning(
            "No config file found; starting with defaults. Create %s/config.yaml to configure.",
            PROJECT_ROOT,
        )
    log.info("Database: %s", config.database_path)
    log.info("Web interface: %s", config.bind_web_interface)
    log.info("Poll interval: %s minutes", config.poll_interval_minutes)
    log.info("Backfill rate: %s req/sec", config.backfill_rate)
    log.info("Tracked games: %s", config.games or "none")

    db = Database(config.database_path)
    added, removed = db.reconcile_tracked_games(config.games)
    if added:
        log.info("Added %s game(s) from config: %s", len(added), added)
    if removed:
        log.info("Removed %s game(s) not in config (history preserved): %s", len(removed), removed)

    steam = SteamClient(config.steam_api_key, config.backfill_rate) if config.steam_api_key else None
    if steam is None:
        log.warning("No steam_api_key configured; wishlist data will not be fetched.")
    state = AppState(db, steam, config)

    stop_event = threading.Event()
    start_thread("startup-backfill", backfill_all_games, state)
    start_thread("polling-loop", polling_loop, state, config.poll_interval_minutes, stop_event)

    run_web(config.bind_web_interface, state, PROJECT_ROOT)
    stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
