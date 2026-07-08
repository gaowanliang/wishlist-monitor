from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from anomaly import compute_anomaly_for_chart_point, compute_anomaly_for_snapshot, label_to_epoch_secs
from config import AppConfig
from db import Database
from models import AnomalyConfig, ChartPoint, GameTotals, WishlistReport
from steam import SteamClient

log = logging.getLogger(__name__)


def normalize_date_to_iso(value: str) -> str:
    if not value:
        return ""
    if "T" in value:
        return value
    return f"{value}T00:00:00Z"


def parse_bind_address(bind: str) -> tuple[str, int]:
    if bind.startswith("["):
        host, _, port_raw = bind.rpartition(":")
        return host.strip("[]"), int(port_raw)
    host, sep, port_raw = bind.rpartition(":")
    if not sep:
        return bind, 3000
    return host or "0.0.0.0", int(port_raw)


def parse_datetime(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


def resolution_for_span_days(days: int) -> str:
    if days <= 3:
        return "raw"
    if days <= 90:
        return "daily"
    if days <= 365:
        return "weekly"
    return "monthly"


def chart_range(query: dict[str, list[str]]) -> tuple[datetime, datetime, str] | tuple[None, None, str]:
    now = datetime.now(timezone.utc)
    value = (query.get("range") or ["7d"])[0]
    if value == "1d":
        return now - timedelta(days=1), now, "raw"
    if value == "2d":
        return now - timedelta(days=2), now, "raw"
    if value == "3d":
        return now - timedelta(days=3), now, "raw"
    if value == "7d":
        return now - timedelta(days=7), now, "daily"
    if value == "1m":
        return now - timedelta(days=30), now, "daily"
    if value == "3m":
        return now - timedelta(days=90), now, "daily"
    if value == "1y":
        return now - timedelta(days=365), now, "weekly"
    if value == "5y":
        return now - timedelta(days=5 * 365), now, "monthly"
    if value == "all":
        return now - timedelta(days=20 * 365), now, "monthly"
    if value == "custom":
        from_raw = (query.get("from") or [""])[0]
        to_raw = (query.get("to") or [""])[0]
        try:
            start = datetime.strptime(from_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end = datetime.strptime(to_raw, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            return None, None, "custom range requires from=YYYY-MM-DD and to=YYYY-MM-DD"
        if start > end:
            return None, None, "from must be <= to"
        return start, end, resolution_for_span_days((end - start).days)
    return now - timedelta(days=7), now, "daily"


def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AppState:
    def __init__(self, db: Database, steam: SteamClient | None, config: AppConfig) -> None:
        self.db = db
        self._steam = steam
        self.backfill_rate = config.backfill_rate
        self.anomaly = config.anomaly
        self._lock = threading.RLock()
        self._backfill_tokens: dict[int, threading.Event] = {}

    def get_steam(self) -> SteamClient | None:
        with self._lock:
            return self._steam

    def start_backfill(self, app_id: int) -> threading.Event:
        with self._lock:
            old = self._backfill_tokens.pop(app_id, None)
            if old:
                old.set()
            token = threading.Event()
            self._backfill_tokens[app_id] = token
            return token

    def cancel_backfill_token(self, app_id: int) -> None:
        with self._lock:
            self._backfill_tokens.pop(app_id, None)

    def finish_backfill(self, app_id: int) -> None:
        self.cancel_backfill_token(app_id)
        self.db.complete_sync(app_id)

    def is_backfill_running(self, app_id: int) -> bool:
        with self._lock:
            return app_id in self._backfill_tokens

    def get_anomaly_config(self) -> AnomalyConfig:
        return self.anomaly


def _int_or_default(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


class WishlistMonitorHandler(BaseHTTPRequestHandler):
    server_version = "WishlistMonitorPython/0.1"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def static_dir(self) -> Path:
        return self.server.static_dir  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/wishlist":
                self.api_wishlist()
            elif match := re.fullmatch(r"/api/wishlist/(\d+)/detail", path):
                self.api_game_detail(int(match.group(1)))
            elif match := re.fullmatch(r"/api/wishlist/(\d+)/chart", path):
                self.api_game_chart(int(match.group(1)), query)
            elif match := re.fullmatch(r"/api/wishlist/(\d+)/history", path):
                self.api_game_history(int(match.group(1)), query)
            elif match := re.fullmatch(r"/api/wishlist/(\d+)/countries/(\d+)", path):
                self.api_snapshot_countries(int(match.group(1)), int(match.group(2)))
            elif match := re.fullmatch(r"/api/wishlist/(\d+)/countries", path):
                self.api_aggregated_countries(int(match.group(1)), query)
            elif path.startswith("/api/"):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            else:
                self.serve_static(path)
        except Exception as exc:
            log.exception("GET %s failed", path)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def send_json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def api_wishlist(self) -> None:
        snapshots = self.state.db.get_latest_snapshots()
        app_info = self.state.db.get_all_app_info()
        totals = self.state.db.get_all_game_totals()
        games = [self._game_report(report, app_info, totals.get(report.app_id, GameTotals())) for report in snapshots]
        self.send_json(HTTPStatus.OK, {"games": games})

    def _game_report(self, report: WishlistReport, app_info: dict[int, tuple[str, str]], totals: GameTotals) -> dict:
        name, image_url = app_info.get(report.app_id, (f"App {report.app_id}", ""))
        return {
            "app_id": report.app_id,
            "name": name,
            "image_url": image_url,
            "date": normalize_date_to_iso(report.date),
            "adds": report.adds,
            "deletes": report.deletes,
            "purchases": report.purchases,
            "gifts": report.gifts,
            "adds_windows": report.adds_windows,
            "adds_mac": report.adds_mac,
            "adds_linux": report.adds_linux,
            "countries": [c.to_dict() for c in report.countries],
            "changed_at": report.fetched_at,
            "total_adds": totals.adds,
            "total_deletes": totals.deletes,
            "total_purchases": totals.purchases,
            "total_gifts": totals.gifts,
            "current_wishlists": totals.adds - totals.deletes - totals.purchases - totals.gifts,
        }

    def api_game_detail(self, app_id: int) -> None:
        if not self.state.db.is_tracked(app_id):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        app_info = self.state.db.get_all_app_info()
        name, image_url = app_info.get(app_id, (f"App {app_id}", ""))
        latest = self.state.db.get_latest_snapshot(app_id)
        totals = self.state.db.get_game_totals(app_id)
        payload = {
            "app_id": app_id,
            "name": name,
            "image_url": image_url,
            "latest": self._game_report(latest, app_info, totals) if latest else None,
            "total_snapshots": self.state.db.get_snapshot_count(app_id),
        }
        self.send_json(HTTPStatus.OK, payload)

    def api_game_chart(self, app_id: int, query: dict[str, list[str]]) -> None:
        if not self.state.db.is_tracked(app_id):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        since, until, resolution = chart_range(query)
        if since is None or until is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": resolution})
            return

        since_str = dt_to_iso(since)
        until_str = dt_to_iso(until)
        now_str = dt_to_iso(datetime.now(timezone.utc))
        chart_points = self.state.db.get_chart_data(app_id, since_str, until_str, resolution)
        anomaly_config = self.state.get_anomaly_config()

        if resolution == "raw":
            lookback_secs = anomaly_config.lookback_days * 86400
            lookback_since = dt_to_iso(since - timedelta(seconds=lookback_secs))
            context_points = self.state.db.get_raw_snapshots_between(app_id, lookback_since, now_str)
        else:
            secs_per_period = 7 * 86400 if resolution == "weekly" else 30 * 86400 if resolution == "monthly" else 86400
            lookback_secs = anomaly_config.lookback_days * secs_per_period
            lookback_since = dt_to_iso(since - timedelta(seconds=lookback_secs))
            context_points = self.state.db.get_chart_data(app_id, lookback_since, now_str, resolution)

        context = [(label_to_epoch_secs(point.label), point) for point in context_points]
        response_points = []
        for point in chart_points:
            metrics = compute_anomaly_for_chart_point(point, context, anomaly_config, float(lookback_secs))
            is_anomaly = bool(metrics.get("adds") or metrics.get("deletes") or metrics.get("purchases") or metrics.get("gifts"))
            response_points.append(
                {
                    "label": point.label,
                    "adds": point.adds,
                    "deletes": point.deletes,
                    "purchases": point.purchases,
                    "gifts": point.gifts,
                    "adds_windows": point.adds_windows,
                    "adds_mac": point.adds_mac,
                    "adds_linux": point.adds_linux,
                    "is_anomaly": is_anomaly,
                    "anomaly_metrics": metrics,
                }
            )
        self.send_json(HTTPStatus.OK, {"resolution": resolution, "points": response_points})

    def api_game_history(self, app_id: int, query: dict[str, list[str]]) -> None:
        if not self.state.db.is_tracked(app_id):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        page = max(_int_or_default((query.get("page") or ["1"])[0], 1), 1)
        per_page = min(max(_int_or_default((query.get("per_page") or ["24"])[0], 24), 1), 100)
        snapshots, total = self.state.db.get_snapshots_paginated(app_id, page, per_page)

        anomaly_config = self.state.get_anomaly_config()
        lookback_secs = anomaly_config.lookback_days * 86400
        context: list[tuple[float, ChartPoint]] = []
        if snapshots:
            fetched_times = [report.fetched_at for _, report in snapshots if report.fetched_at]
            if fetched_times:
                newest = max(fetched_times)
                oldest = min(fetched_times)
                oldest_dt = parse_datetime(oldest)
                if oldest_dt:
                    since_str = dt_to_iso(oldest_dt - timedelta(seconds=lookback_secs))
                    context_points = self.state.db.get_raw_snapshots_between(app_id, since_str, newest)
                    context = [(label_to_epoch_secs(point.label), point) for point in context_points]

        entries = []
        for snapshot_id, report in snapshots:
            metrics = compute_anomaly_for_snapshot(report, context, anomaly_config, float(lookback_secs))
            is_anomaly = bool(metrics.get("adds") or metrics.get("deletes") or metrics.get("purchases") or metrics.get("gifts"))
            entries.append(
                {
                    "snapshot_id": snapshot_id,
                    "date": normalize_date_to_iso(report.date),
                    "adds": report.adds,
                    "deletes": report.deletes,
                    "purchases": report.purchases,
                    "gifts": report.gifts,
                    "adds_windows": report.adds_windows,
                    "adds_mac": report.adds_mac,
                    "adds_linux": report.adds_linux,
                    "fetched_at": report.fetched_at or "",
                    "is_anomaly": is_anomaly,
                    "anomaly_metrics": metrics,
                }
            )
        self.send_json(HTTPStatus.OK, {"entries": entries, "total": total, "page": page, "per_page": per_page})

    def api_snapshot_countries(self, app_id: int, snapshot_id: int) -> None:
        if not self.state.db.is_tracked(app_id):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        countries = self.state.db.get_snapshot_countries(app_id, snapshot_id)
        if countries is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        self.send_json(HTTPStatus.OK, {"snapshot_id": snapshot_id, "countries": [c.to_dict() for c in countries]})

    def api_aggregated_countries(self, app_id: int, query: dict[str, list[str]]) -> None:
        if not self.state.db.is_tracked(app_id):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        since, until, message = chart_range(query)
        if since is None or until is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": message})
            return
        countries = self.state.db.get_aggregated_countries(app_id, dt_to_iso(since), dt_to_iso(until))
        self.send_json(HTTPStatus.OK, {"countries": [c.to_dict() for c in countries]})

    def serve_static(self, path: str) -> None:
        dist = self.static_dir
        if not dist.exists():
            body = b"<h1>Frontend not built</h1><p>Run <code>npm run build</code> in the <code>web</code> directory.</p>"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        rel = path.lstrip("/") or "index.html"
        candidate = (dist / rel).resolve()
        root = dist.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if candidate.name == "index.html" or candidate.suffix == ".html":
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)


class WishlistMonitorServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: AppState, static_dir: Path) -> None:
        super().__init__(server_address, WishlistMonitorHandler)
        self.state = state
        self.static_dir = static_dir


def run_web(bind_addr: str, state: AppState, repo_root: Path) -> None:
    host, port = parse_bind_address(bind_addr)
    static_dir = repo_root / "web" / "dist"
    server = WishlistMonitorServer((host, port), state, static_dir)
    log.info("Web interface listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        server.server_close()
