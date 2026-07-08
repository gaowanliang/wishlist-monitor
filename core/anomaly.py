from __future__ import annotations

from datetime import datetime, timezone

from models import AnomalyConfig, ChartPoint, WishlistReport

MIN_MAD_FLOOR = 2.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2 == 0:
        return (values[mid - 1] + values[mid]) / 2.0
    return values[mid]


def mad(values: list[float], med: float) -> float:
    if len(values) < 2:
        return 0.0
    return median([abs(v - med) for v in values]) * 1.4826


def apply_mad_floor(value: float, med: float, floor_pct: float) -> float:
    return max(value, max(abs(med) * floor_pct, MIN_MAD_FLOOR))


def label_to_epoch_secs(value: str) -> float:
    if not value:
        return 0.0
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        if "T" in value:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        if "-W" in value:
            year, week = value.split("-W", 1)
            dt = datetime.strptime(f"{year} {week} 1", "%Y %W %w").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        if len(value) == 7 and value[4] == "-":
            return datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc).timestamp()
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


def _weekday(date_value: str) -> int | None:
    try:
        return datetime.strptime(date_value, "%Y-%m-%d").weekday()
    except Exception:
        return None


def _empty_metrics() -> dict:
    return {"adds": False, "deletes": False, "purchases": False, "gifts": False}


def _format_description(metric_name: str, current: float, med: float) -> str:
    if med < 0.5:
        if current < 0.5:
            return f"{metric_name} unusual activity detected"
        return f"{metric_name} spiked to {current:.0f}/day from near-zero baseline"
    direction = "above" if current > med else "below"
    ratio = current / med if current > med else (med / current if current > 0.5 else med)
    if ratio >= 2.0:
        return f"{metric_name} {ratio:.0f}x {direction} normal ({current:.0f}/day vs ~{med:.0f}/day)"
    return f"{metric_name} unusual at {current:.0f}/day ({direction} ~{med:.0f}/day typical)"


def compute_anomaly_inner(
    curr_secs: float,
    curr_date: str,
    adds: int,
    deletes: int,
    purchases: int,
    gifts: int,
    context: list[tuple[float, ChartPoint]],
    config: AnomalyConfig,
    lookback_secs: float,
) -> dict:
    if len(context) < 3 or curr_secs <= 0:
        return _empty_metrics()

    window_start = curr_secs - lookback_secs
    window = [(secs, point) for secs, point in context if window_start <= secs < curr_secs]
    if len(window) < 3:
        return _empty_metrics()

    target_weekday = _weekday(curr_date)

    def build_daily_maxes(attr: str) -> list[float]:
        day_map: dict[str, int] = {}
        for _, point in window:
            if point.date == curr_date:
                continue
            value = int(getattr(point, attr))
            day_map[point.date] = max(day_map.get(point.date, value), value)
        if target_weekday is not None:
            same_weekday = [float(v) for d, v in day_map.items() if _weekday(d) == target_weekday]
            if len(same_weekday) >= 3:
                return same_weekday
        return [float(v) for v in day_map.values()]

    def max_current_day(curr_value: int, attr: str) -> float:
        prior_values = [int(getattr(point, attr)) for secs, point in context if point.date == curr_date and secs < curr_secs]
        return float(max([curr_value, *prior_values] if prior_values else [curr_value]))

    def check_metric(name: str, curr_value: int, attr: str) -> tuple[bool, str | None]:
        daily_values = build_daily_maxes(attr)
        if len(daily_values) < 3:
            return False, None
        current_daily = max_current_day(curr_value, attr)
        med = median(daily_values)
        effective_mad = apply_mad_floor(mad(daily_values, med), med, config.mad_floor_pct)
        deviation = current_daily - med
        if abs(deviation) < float(config.min_absolute):
            return False, None
        if effective_mad == 0.0:
            is_anomalous = abs(deviation) > 0.0
        else:
            z_score = abs(deviation) / effective_mad
            is_anomalous = z_score > (config.sensitivity_up if deviation >= 0 else config.sensitivity_down)
        if is_anomalous:
            return True, _format_description(name, current_daily, med)
        return False, None

    checks = {
        "adds": check_metric("Adds", adds, "adds"),
        "deletes": check_metric("Deletes", deletes, "deletes"),
        "purchases": check_metric("Purchases", purchases, "purchases"),
        "gifts": check_metric("Gifts", gifts, "gifts"),
    }
    metrics = {key: flag for key, (flag, _) in checks.items()}
    descriptions = [desc for _, desc in checks.values() if desc]
    if descriptions:
        metrics["descriptions"] = descriptions
    return metrics


def compute_anomaly_for_chart_point(
    point: ChartPoint,
    context: list[tuple[float, ChartPoint]],
    config: AnomalyConfig,
    lookback_secs: float,
) -> dict:
    return compute_anomaly_inner(
        label_to_epoch_secs(point.label),
        point.date,
        point.adds,
        point.deletes,
        point.purchases,
        point.gifts,
        context,
        config,
        lookback_secs,
    )


def compute_anomaly_for_snapshot(
    snapshot: WishlistReport,
    context: list[tuple[float, ChartPoint]],
    config: AnomalyConfig,
    lookback_secs: float,
) -> dict:
    return compute_anomaly_inner(
        label_to_epoch_secs(snapshot.fetched_at or ""),
        snapshot.date,
        snapshot.adds,
        snapshot.deletes,
        snapshot.purchases,
        snapshot.gifts,
        context,
        config,
        lookback_secs,
    )
