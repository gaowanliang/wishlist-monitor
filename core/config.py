from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from models import AnomalyConfig

DEFAULT_BIND_ADDRESS = "0.0.0.0:14520"
DEFAULT_POLL_INTERVAL_MINUTES = 5
DEFAULT_BACKFILL_RATE = 1.0
DEFAULT_CONFIG_FILENAME = "config.yaml"


@dataclass
class AppConfig:
    bind_web_interface: str
    database_path: Path
    steam_api_key: str | None
    games: list[int]
    poll_interval_minutes: int
    backfill_rate: float
    anomaly: AnomalyConfig
    config_path: Path | None = None


def default_db_path() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "wishlist-pulse" / "data.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wishlist-pulse" / "data.db"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "wishlist-pulse" / "data.db"


def _parse_app_id(value: str) -> int:
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    match = re.search(r"/app/(\d+)", raw)
    if match:
        return int(match.group(1))
    raise ValueError(f'could not parse an app ID from "{raw}"; use a numeric ID or a Steam store URL')


def _resolve_config_path(cli_path: str | None) -> Path | None:
    candidate = cli_path or os.environ.get("CONFIG_PATH")
    if candidate:
        path = Path(candidate).expanduser()
        if not path.exists():
            raise ValueError(f"config file not found: {path}")
        return path
    default = Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME
    return default if default.exists() else None


def _load_yaml(path: Path | None) -> dict:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a mapping at the top level")
    return data


def _parse_games(raw) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("'games' must be a list of Steam app IDs or store URLs")
    games: list[int] = []
    seen: set[int] = set()
    for item in raw:
        app_id = _parse_app_id(str(item))
        if app_id not in seen:
            seen.add(app_id)
            games.append(app_id)
    return games


def _parse_anomaly(raw) -> AnomalyConfig:
    data = raw or {}
    if not isinstance(data, dict):
        raise ValueError("'anomaly' must be a mapping")
    defaults = AnomalyConfig()
    return AnomalyConfig(
        lookback_days=int(data.get("lookback_days", defaults.lookback_days)),
        sensitivity_up=float(data.get("sensitivity_up", defaults.sensitivity_up)),
        sensitivity_down=float(data.get("sensitivity_down", defaults.sensitivity_down)),
        min_absolute=int(data.get("min_absolute", defaults.min_absolute)),
        mad_floor_pct=float(data.get("mad_floor_pct", defaults.mad_floor_pct)),
    )


def load_config(argv: list[str] | None = None) -> AppConfig:
    parser = argparse.ArgumentParser(
        prog="wishlist-pulse-py",
        description="Track Steam wishlist pulse from a YAML config file.",
    )
    parser.add_argument(
        "--config",
        help=f"Path to the YAML config file (default: {DEFAULT_CONFIG_FILENAME} next to main.py, or $CONFIG_PATH)",
    )
    args = parser.parse_args(argv)

    config_path = _resolve_config_path(args.config)
    data = _load_yaml(config_path)

    bind = str(data.get("bind") or DEFAULT_BIND_ADDRESS)

    database_path_raw = data.get("database_path")
    database_path = Path(str(database_path_raw)).expanduser() if database_path_raw else default_db_path()

    poll_interval = int(data.get("poll_interval_minutes") or DEFAULT_POLL_INTERVAL_MINUTES)
    if poll_interval <= 0:
        raise ValueError("poll_interval_minutes must be at least 1")

    backfill_rate = float(data.get("backfill_rate") or DEFAULT_BACKFILL_RATE)
    if backfill_rate <= 0:
        raise ValueError("backfill_rate must be greater than 0")

    steam_api_key = str(data.get("steam_api_key") or "").strip() or None

    return AppConfig(
        bind_web_interface=bind,
        database_path=database_path,
        steam_api_key=steam_api_key,
        games=_parse_games(data.get("games")),
        poll_interval_minutes=poll_interval,
        backfill_rate=backfill_rate,
        anomaly=_parse_anomaly(data.get("anomaly")),
        config_path=config_path,
    )
