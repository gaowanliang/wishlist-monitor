from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models import CountryReport, WishlistReport

WISHLIST_API_URL = "https://partner.steam-api.com/IPartnerFinancialsService/GetAppWishlistReporting/v1/"
STORE_API_URL = "https://store.steampowered.com/api/appdetails"

log = logging.getLogger(__name__)


def pacific_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc)


class SteamError(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_rate
            time.sleep(max(wait, 0.01))


@dataclass
class AppInfo:
    name: str
    image_url: str = ""


def _get_json(url: str, params: dict[str, str], timeout: float = 20.0) -> tuple[int, dict]:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "wishlist-pulse-python"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SteamError(f"Steam API returned {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SteamError(str(exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise SteamError(f"Invalid JSON from Steam: {exc}") from exc


def validate_api_key(key: str) -> None:
    try:
        _get_json(WISHLIST_API_URL, {"key": key, "appid": "480", "date": "2020-01-01"}, timeout=15.0)
    except SteamError as exc:
        text = str(exc)
        if "403" in text:
            raise SteamError("Invalid Steam API key") from exc
        if "401" in text:
            raise SteamError("Steam API key is not authorized") from exc
        raise


class SteamClient:
    def __init__(self, api_key: str, backfill_rate: float) -> None:
        self.api_key = api_key
        self.backfill_rate = backfill_rate
        self._app_info: dict[int, AppInfo] = {}
        self._lock = threading.RLock()
        self.rate_limiter = RateLimiter(10.0, 2.0)
        self.backfill_rate_limiter = RateLimiter(3.0, backfill_rate)

    def set_api_key(self, key: str) -> None:
        with self._lock:
            self.api_key = key

    def app_info(self) -> dict[int, AppInfo]:
        with self._lock:
            return dict(self._app_info)

    def fetch_app_name(self, app_id: int) -> str:
        with self._lock:
            info = self._app_info.get(app_id)
            if info:
                return info.name

        self.rate_limiter.acquire()
        status, body = _get_json(STORE_API_URL, {"appids": str(app_id)})
        if status < 200 or status >= 300:
            raise SteamError(f"Steam store API returned {status} for app {app_id}")

        entry = body.get(str(app_id), {})
        data = entry.get("data")
        if not data:
            raise SteamError(f"App {app_id} not found on the Steam store")

        name = data.get("name")
        if not name:
            raise SteamError(f"App {app_id} has no name")
        image_url = data.get("header_image") or ""

        with self._lock:
            self._app_info[app_id] = AppInfo(name=name, image_url=image_url)
        return name

    def fetch_wishlist(self, app_id: int) -> WishlistReport:
        date = pacific_now().strftime("%Y-%m-%d")
        return self.fetch_wishlist_for_date(app_id, date)

    def fetch_app_min_date(self, app_id: int) -> str | None:
        date = pacific_now().strftime("%Y-%m-%d")
        self.rate_limiter.acquire()
        body = self._fetch_wishlist_body(app_id, date)
        response = body.get("response") or {}
        return response.get("app_min_date")

    def fetch_wishlist_for_date(self, app_id: int, date: str) -> WishlistReport:
        self.rate_limiter.acquire()
        return self._fetch_wishlist_inner(app_id, date)

    def fetch_wishlist_for_backfill(self, app_id: int, date: str) -> WishlistReport:
        self.backfill_rate_limiter.acquire()
        return self._fetch_wishlist_inner(app_id, date)

    def fetch_all(self, app_ids: list[int]) -> list[tuple[WishlistReport | None, Exception | None]]:
        results: list[tuple[WishlistReport | None, Exception | None]] = []
        for app_id in app_ids:
            try:
                results.append((self.fetch_wishlist(app_id), None))
            except Exception as exc:
                results.append((None, exc))
        return results

    def _fetch_wishlist_body(self, app_id: int, date: str) -> dict:
        with self._lock:
            key = self.api_key
        status, body = _get_json(
            WISHLIST_API_URL,
            {"key": key, "appid": str(app_id), "date": date},
            timeout=30.0,
        )
        if status < 200 or status >= 300:
            raise SteamError(f"Steam API returned {status} for app {app_id}")
        return body

    def _fetch_wishlist_inner(self, app_id: int, date: str) -> WishlistReport:
        log.debug("Requesting wishlist: appid=%s date=%s", app_id, date)
        data = self._fetch_wishlist_body(app_id, date)
        response = data.get("response")
        if not response:
            raise SteamError(f"No wishlist data for app {app_id} on {date}")

        summary = response.get("wishlist_summary")
        if not summary:
            min_date = response.get("app_min_date")
            if min_date:
                raise SteamError(f"No data for app {app_id} on {date} (earliest available: {min_date})")
            raise SteamError(f"No wishlist data for app {app_id} on {date}")

        countries: list[CountryReport] = []
        for item in response.get("country_summary") or []:
            actions = item.get("summary_actions") or {}
            countries.append(
                CountryReport(
                    country_code=item.get("country_code") or "",
                    adds=int(actions.get("wishlist_adds") or 0),
                    deletes=int(actions.get("wishlist_deletes") or 0),
                    purchases=int(actions.get("wishlist_purchases") or 0),
                    gifts=int(actions.get("wishlist_gifts") or 0),
                )
            )

        return WishlistReport(
            app_id=app_id,
            date=response.get("date") or date,
            adds=int(summary.get("wishlist_adds") or 0),
            deletes=int(summary.get("wishlist_deletes") or 0),
            purchases=int(summary.get("wishlist_purchases") or 0),
            gifts=int(summary.get("wishlist_gifts") or 0),
            adds_windows=int(summary.get("wishlist_adds_windows") or 0),
            adds_mac=int(summary.get("wishlist_adds_mac") or 0),
            adds_linux=int(summary.get("wishlist_adds_linux") or 0),
            countries=countries,
            app_min_date=response.get("app_min_date"),
        )
