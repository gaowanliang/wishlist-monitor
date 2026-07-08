from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CountryReport:
    country_code: str
    adds: int = 0
    deletes: int = 0
    purchases: int = 0
    gifts: int = 0

    def to_dict(self) -> dict:
        return {
            "country_code": self.country_code,
            "adds": self.adds,
            "deletes": self.deletes,
            "purchases": self.purchases,
            "gifts": self.gifts,
        }


@dataclass
class WishlistReport:
    app_id: int
    date: str
    adds: int = 0
    deletes: int = 0
    purchases: int = 0
    gifts: int = 0
    adds_windows: int = 0
    adds_mac: int = 0
    adds_linux: int = 0
    countries: list[CountryReport] = field(default_factory=list)
    fetched_at: str | None = None
    app_min_date: str | None = None


@dataclass
class GameTotals:
    adds: int = 0
    deletes: int = 0
    purchases: int = 0
    gifts: int = 0


@dataclass
class ChartPoint:
    label: str
    date: str
    adds: int = 0
    deletes: int = 0
    purchases: int = 0
    gifts: int = 0
    adds_windows: int = 0
    adds_mac: int = 0
    adds_linux: int = 0


@dataclass
class DailyMax:
    date: str
    adds: int = 0
    deletes: int = 0
    purchases: int = 0
    gifts: int = 0


@dataclass
class CountryDailyMax:
    date: str
    adds: int = 0
    deletes: int = 0


@dataclass
class GameSyncRow:
    app_id: int
    sync_type: str
    status: str
    started_at: str
    completed_at: str | None
    total_dates: int
    requested_by: str | None


@dataclass
class AnomalyConfig:
    lookback_days: int = 21
    sensitivity_up: float = 2.0
    sensitivity_down: float = 1.8
    min_absolute: int = 5
    mad_floor_pct: float = 0.05
