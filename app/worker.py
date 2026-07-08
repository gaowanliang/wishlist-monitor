from __future__ import annotations

import logging
import time
from datetime import timedelta
from threading import Event, Thread

from steam import SteamClient, pacific_now

log = logging.getLogger(__name__)


def start_thread(name: str, target, *args) -> Thread:
    def run_with_logging() -> None:
        try:
            target(*args)
        except Exception:
            log.exception("Background thread %s crashed", name)

    thread = Thread(name=name, target=run_with_logging, daemon=True)
    thread.start()
    return thread


def backfill_all_games(state) -> None:
    steam = state.get_steam()
    if not steam:
        log.debug("No Steam API key configured, skipping backfill.")
        return
    app_ids = state.db.get_tracked_game_ids()
    if not app_ids:
        return
    log.info("Starting full history backfill for %s game(s)...", len(app_ids))
    for app_id in app_ids:
        if state.is_backfill_running(app_id):
            continue
        cancel = state.start_backfill(app_id)
        backfill_game_history(state, steam, app_id, cancel, "auto", "system")
    log.info("History backfill complete.")


def backfill_game_history(state, steam: SteamClient, app_id: int, cancel: Event, sync_type: str, requested_by: str) -> None:
    force = sync_type == "full"
    try:
        cached_min_date = None if force else state.db.get_app_min_date(app_id)
        if cached_min_date:
            min_date_str = cached_min_date
        else:
            min_date_str = steam.fetch_app_min_date(app_id)
            if not min_date_str:
                state.cancel_backfill_token(app_id)
                return
            state.db.store_app_min_date(app_id, min_date_str)

        from datetime import datetime

        min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
        yesterday = pacific_now().date() - timedelta(days=1)
        if min_date > yesterday:
            state.cancel_backfill_token(app_id)
            return

        if force:
            state.db.clear_sync_progress(app_id)

        crawled = state.db.get_crawled_dates_for_game(app_id, include_snapshots=not force)
        failed = state.db.get_failed_dates(app_id)

        dates_to_fetch: list[str] = []
        current = yesterday
        while current >= min_date:
            date_str = current.strftime("%Y-%m-%d")
            if date_str not in crawled or date_str in failed:
                dates_to_fetch.append(date_str)
            current -= timedelta(days=1)

        if not dates_to_fetch:
            state.cancel_backfill_token(app_id)
            return

        total = (yesterday - min_date).days + 1
        state.db.start_sync(app_id, sync_type, requested_by, total)
        log.info("app %s: backfilling %s day(s) from %s", app_id, len(dates_to_fetch), min_date_str)

        backfilled = 0
        consecutive_failures = 0
        for index, date_str in enumerate(dates_to_fetch, start=1):
            if cancel.is_set():
                state.db.fail_sync(app_id)
                return
            try:
                report = steam.fetch_wishlist_for_backfill(app_id, date_str)
                state.db.insert_backfill_snapshot(report, f"{date_str}T23:59:59Z", force)
                state.db.mark_date_crawled(app_id, date_str)
                if date_str in failed:
                    state.db.clear_failed_date(app_id, date_str)
                backfilled += 1
                consecutive_failures = 0
            except Exception as exc:
                text = str(exc)
                if "No data for app" in text or "No wishlist data" in text:
                    state.db.mark_date_crawled(app_id, date_str)
                    if date_str in failed:
                        state.db.clear_failed_date(app_id, date_str)
                    consecutive_failures = 0
                else:
                    state.db.mark_date_failed(app_id, date_str)
                    consecutive_failures += 1
                    log.warning("Backfill failed for app %s on %s: %s", app_id, date_str, exc)
                    if consecutive_failures >= 5:
                        time.sleep(60)
                        if cancel.is_set():
                            state.db.fail_sync(app_id)
                            return
                        try:
                            report = steam.fetch_wishlist_for_backfill(app_id, date_str)
                            state.db.insert_backfill_snapshot(report, f"{date_str}T23:59:59Z", force)
                            state.db.mark_date_crawled(app_id, date_str)
                            state.db.clear_failed_date(app_id, date_str)
                            backfilled += 1
                            consecutive_failures = 0
                        except Exception:
                            log.warning("app %s: still failing after pause, aborting backfill", app_id)
                            state.db.fail_sync(app_id)
                            state.cancel_backfill_token(app_id)
                            return
            if index % 50 == 0:
                log.info("app %s: backfilled %s/%s dates", app_id, index, len(dates_to_fetch))

        log.info("app %s: backfill complete, %s new day(s)", app_id, backfilled)
        state.finish_backfill(app_id)
    except Exception:
        log.exception("Backfill crashed for app %s", app_id)
        try:
            state.db.fail_sync(app_id)
        finally:
            state.cancel_backfill_token(app_id)


def polling_loop(state, poll_interval_minutes: int, stop_event: Event) -> None:
    while not stop_event.is_set():
        try:
            _poll_once(state)
        except Exception:
            log.exception("Polling loop failed")
        stop_event.wait(max(60, poll_interval_minutes * 60))


def _poll_once(state) -> None:
    steam = state.get_steam()
    if not steam:
        log.debug("No Steam API key configured, skipping poll.")
        return
    app_ids = state.db.get_tracked_game_ids()
    if not app_ids:
        return

    log.info("Polling Steam wishlist data...")
    for app_id in app_ids:
        try:
            name = steam.fetch_app_name(app_id)
            info = steam.app_info().get(app_id)
            state.db.upsert_app_info(app_id, name, info.image_url if info else "")
        except Exception as exc:
            log.debug("Failed to refresh app info for %s: %s", app_id, exc)

    results = steam.fetch_all(app_ids)
    failed_indices = [i for i, (_, err) in enumerate(results) if err is not None]
    if failed_indices:
        time.sleep(3)
        retry_ids = [app_ids[i] for i in failed_indices]
        retry_results = steam.fetch_all(retry_ids)
        for original_index, retry in zip(failed_indices, retry_results):
            results[original_index] = retry

    for report, err in results:
        if err:
            log.warning("Poll fetch error: %s", err)
            continue
        assert report is not None
        try:
            kind, _previous = state.db.insert_snapshot_if_changed(report)
            if kind == "changed":
                log.info("Data changed for app %s on %s, snapshot saved", report.app_id, report.date)
                # Telegram and Discord notifications are intentionally omitted in the Python backend.
            elif kind == "first_snapshot":
                log.info("First snapshot for app %s on %s saved", report.app_id, report.date)
        except Exception as exc:
            log.error("Failed to store snapshot for app %s: %s", report.app_id, exc)
