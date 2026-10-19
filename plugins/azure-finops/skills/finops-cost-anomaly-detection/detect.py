"""Cost anomaly detector for the FinOps SRE Agent pack (skill: finops-cost-anomaly-detection).

Pure, dependency-free logic so it is unit-testable offline. The agent reads this file
and runs `detect_anomalies(...)` with the runtime's sandbox Python tool after loading
Cost Management UsageDetails line items. No Azure calls happen here.

Line-item shape (one dict per UsageDetails row, already flattened by the skill):
    {
        "date": "2026-07-12",        # properties.date (day granularity)
        "cost": 172.89,               # properties.costInUSD
        "meterCategory": "Container Apps",
        "resourceGroup": "sre-agent-demo-2",
        "resourceId": "/subscriptions/.../containerApps/foo",
        "tags": {"env": "prod"}       # optional; may be {} or missing
    }
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

DEFAULT_DIMENSIONS = ("meterCategory", "resourceGroup", "resourceId")


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _dimension_value(item: dict, dimension: str):
    """Resolve a dimension value. `tag:<key>` reads from the item's tags bag."""
    if dimension.startswith("tag:"):
        return (item.get("tags") or {}).get(dimension[len("tag:"):])
    return item.get(dimension)


def resolve_today(dates, assume_last_partial: bool = True) -> date:
    """Pick the latest COMPLETE day.

    Cost Management billing lags 1-2 days, so the newest day is usually partial and
    must not be treated as a spike or drop. With `assume_last_partial`, the newest
    distinct day is dropped and the day before it is used as "today".
    """
    distinct = sorted({_parse_date(d) for d in dates})
    if not distinct:
        raise ValueError("no dates provided")
    if assume_last_partial and len(distinct) >= 2:
        return distinct[-2]
    return distinct[-1]


def _daily_totals(items, dimension, value) -> dict:
    totals: dict = defaultdict(float)
    for item in items:
        if _dimension_value(item, dimension) == value:
            totals[_parse_date(item["date"])] += float(item.get("cost", 0.0) or 0.0)
    return totals


def _detect_for_dimension(
    items, dimension, today, baseline_days, k, min_delta_usd, wow_ratio
):
    anomalies = []
    values = {
        _dimension_value(i, dimension)
        for i in items
        if _dimension_value(i, dimension) is not None
    }
    for value in values:
        totals = _daily_totals(items, dimension, value)
        current = totals.get(today, 0.0)
        if current <= 0:
            continue

        baseline = [
            totals.get(today - timedelta(days=offset), 0.0)
            for offset in range(1, baseline_days + 1)
        ]
        has_history = any(v > 0 for v in baseline)
        mean = statistics.fmean(baseline) if baseline else 0.0
        std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0

        wow_prev = totals.get(today - timedelta(days=7))

        triggers = []
        if not has_history:
            # First time this dimension shows meaningful spend.
            if current >= min_delta_usd:
                triggers.append("new_spend")
        else:
            baseline_hit = current > mean + k * std and (current - mean) >= min_delta_usd
            wow_hit = (
                wow_prev is not None
                and wow_prev > 0
                and current >= wow_prev * wow_ratio
                and (current - wow_prev) >= min_delta_usd
            )
            if baseline_hit:
                triggers.append("baseline")
            if wow_hit:
                triggers.append("wow")

        if not triggers:
            continue

        kind = "new_spend" if triggers == ["new_spend"] else "spike"
        dod_delta = current - mean
        wow_delta = (current - wow_prev) if wow_prev is not None else None
        impact = max(dod_delta, wow_delta if wow_delta is not None else dod_delta)

        anomalies.append(
            {
                "dimension": dimension,
                "value": value,
                "date": today.isoformat(),
                "kind": kind,
                "triggers": triggers,
                "current_usd": round(current, 2),
                "baseline_mean_usd": round(mean, 2),
                "baseline_std_usd": round(std, 2),
                "dod_delta_usd": round(dod_delta, 2),
                "dod_delta_pct": round((dod_delta / mean * 100) if mean > 0 else float("inf"), 1),
                "wow_delta_usd": round(wow_delta, 2) if wow_delta is not None else None,
                "impact_usd": round(impact, 2),
            }
        )
    return anomalies


def detect_anomalies(
    line_items,
    *,
    today=None,
    assume_last_partial: bool = True,
    dimensions=DEFAULT_DIMENSIONS,
    baseline_days: int = 28,
    k: float = 3.0,
    min_delta_usd: float = 5.0,
    wow_ratio: float = 1.5,
):
    """Detect cost anomalies across the given dimensions.

    Returns a list of anomaly dicts sorted by absolute dollar impact (descending).
    A spike fires when the current day exceeds the trailing baseline (mean + k*std)
    OR is at least `wow_ratio`x the same day last week -- in both cases only if the
    absolute change clears `min_delta_usd`. Dimensions with no prior history are
    labelled `new_spend` instead of `spike`.
    """
    if not line_items:
        return []

    if today is None:
        today = resolve_today((i["date"] for i in line_items), assume_last_partial)
    else:
        today = _parse_date(today)

    anomalies = []
    for dimension in dimensions:
        anomalies.extend(
            _detect_for_dimension(
                line_items, dimension, today, baseline_days, k, min_delta_usd, wow_ratio
            )
        )

    anomalies.sort(key=lambda a: a["impact_usd"], reverse=True)
    return anomalies
