"""Layer-1 unit tests for the cost anomaly detector (offline, deterministic, no Azure)."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

# Load detect.py directly from the skill folder (no package install needed).
_DETECT_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-cost-anomaly-detection"
    / "detect.py"
)
_spec = importlib.util.spec_from_file_location("detect", _DETECT_PATH)
detect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect)


TODAY = date(2026, 7, 14)


def _day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def _series(meter, daily_cost, days, resource_id="/r/1", rg="rg1", start_offset=1):
    """Flat history of `daily_cost` for `days` days, oldest first, ending yesterday."""
    return [
        {
            "date": (TODAY - timedelta(days=off)).isoformat(),
            "cost": daily_cost,
            "meterCategory": meter,
            "resourceGroup": rg,
            "resourceId": resource_id,
            "tags": {},
        }
        for off in range(start_offset, start_offset + days)
    ]


def _point(meter, cost, offset=0, resource_id="/r/1", rg="rg1"):
    return {
        "date": (TODAY - timedelta(days=offset)).isoformat(),
        "cost": cost,
        "meterCategory": meter,
        "resourceGroup": rg,
        "resourceId": resource_id,
        "tags": {},
    }


def _by_value(anomalies, value):
    return [a for a in anomalies if a["value"] == value]


def test_known_spike_is_flagged_with_correct_delta():
    items = _series("Container Apps", 10.0, 28) + [_point("Container Apps", 100.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Container Apps")
    assert len(hits) == 1
    a = hits[0]
    assert a["kind"] == "spike"
    assert "baseline" in a["triggers"]
    assert a["current_usd"] == 100.0
    assert a["baseline_mean_usd"] == 10.0
    assert a["dod_delta_usd"] == 90.0


def test_tiny_meter_noise_is_not_flagged():
    # $0.50 baseline -> $2.00 today: big % but below the $5 absolute floor.
    items = _series("Tiny Meter", 0.5, 28) + [_point("Tiny Meter", 2.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    assert _by_value(result, "Tiny Meter") == []


def test_new_resource_is_new_spend_not_spike():
    # No baseline history at all, only today.
    items = [_point("Fresh Service", 40.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Fresh Service")
    assert len(hits) == 1
    assert hits[0]["kind"] == "new_spend"
    assert hits[0]["triggers"] == ["new_spend"]


def test_flat_history_produces_no_anomalies():
    # Perfectly flat, including today -> zero false positives.
    items = _series("Steady", 25.0, 28) + [_point("Steady", 25.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    assert result == []


def test_gradual_creep_caught_by_week_over_week():
    # Handcrafted so the trailing-baseline test misses but WoW fires.
    # Baseline days (1..28 ago) hover around ~13 with enough spread that
    # mean + 3*std is well above today's 16; but same-day-last-week was 10.
    totals = {}
    # 28 days of low-ish values that ramp gently, same-day-last-week = 10.
    values = [8, 9, 9, 10, 11, 12, 10, 12, 13, 14, 13, 15, 16, 14,
              15, 17, 18, 16, 17, 19, 20, 18, 19, 21, 22, 20, 21, 23]
    items = []
    for off, v in zip(range(1, 29), values):
        items.append(_point("Creeper", float(v), offset=off))
    # Force same-day-last-week (offset 7) to a clearly lower value.
    items = [i for i in items if i["date"] != _day(7)]
    items.append(_point("Creeper", 10.0, offset=7))
    # Today: only 16 -> below baseline threshold, but >= 1.5x last week's 10.
    items.append(_point("Creeper", 16.0, offset=0))

    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Creeper")
    assert len(hits) == 1
    assert "wow" in hits[0]["triggers"]
    assert "baseline" not in hits[0]["triggers"]


def test_anomalies_ranked_by_absolute_dollars():
    big = _series("Big", 100.0, 28, resource_id="/r/big") + [
        _point("Big", 300.0, resource_id="/r/big")
    ]
    small = _series("Small", 5.0, 28, resource_id="/r/small") + [
        _point("Small", 60.0, resource_id="/r/small")
    ]
    result = detect.detect_anomalies(
        big + small, today=TODAY, dimensions=("meterCategory",)
    )
    # Small has a bigger %, Big has a bigger absolute impact -> Big ranks first.
    assert result[0]["value"] == "Big"
    assert result[0]["impact_usd"] > result[1]["impact_usd"]


def test_partial_last_day_is_excluded_by_resolve_today():
    # Newest distinct day is partial; resolve_today should pick the day before.
    dates = [_day(2), _day(1), _day(0)]
    resolved = detect.resolve_today(dates, assume_last_partial=True)
    assert resolved == TODAY - timedelta(days=1)


def test_partial_last_day_not_flagged_end_to_end():
    # A low partial value on the newest day must not register as a drop/spike,
    # and yesterday's real spike is what gets evaluated.
    items = _series("WithPartial", 10.0, 28, start_offset=2)  # days 2..29 ago
    items.append(_point("WithPartial", 100.0, offset=1))       # yesterday: real spike
    items.append(_point("WithPartial", 3.0, offset=0))         # today: partial, ignore
    result = detect.detect_anomalies(
        items, assume_last_partial=True, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "WithPartial")
    assert len(hits) == 1
    assert hits[0]["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert hits[0]["current_usd"] == 100.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
