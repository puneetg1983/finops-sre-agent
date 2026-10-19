"""Layer-1 unit tests for the cost-vs-reliability skill (offline, deterministic)."""

import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-cost-vs-reliability"
    / "reliability.py"
)
_spec = importlib.util.spec_from_file_location("reliability", _PATH)
reliability = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reliability)

analyze_cost_vs_reliability = reliability.analyze_cost_vs_reliability

A = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/a"
B = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/b"
C = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Cache/Redis/c"
D = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/d"
E = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/e"


def _line(cost, *, rid=A, date="2026-07-15", service="Microsoft.Compute", cost_key="cost"):
    return {
        cost_key: cost,
        "resourceId": rid,
        "consumedService": service,
        "resourceGroup": "rg",
        "date": date,
    }


def _alert(rid=A, *, sev="Sev1", date="2026-07-16", name="cpu high"):
    return {
        "name": name,
        "properties": {
            "essentials": {
                "severity": sev,
                "alertTargetIDs": [rid] if rid else [],
                "startDateTime": date,
                "alertRule": name,
            }
        },
    }


def _health(rid=A, *, state="Unavailable", date="2026-07-14"):
    return {"resourceId": rid, "properties": {"availabilityState": state, "occurredTime": date}}


def _advisor(rid=A, *, category="HighAvailability", date="2026-07-13"):
    return {
        "category": category,
        "resourceMetadata": {"resourceId": rid},
        "shortDescription": {"problem": "availability risk"},
        "lastUpdated": date,
    }


def test_empty_and_none_inputs_are_safe():
    out = analyze_cost_vs_reliability()
    assert out["total_usd"] == 0.0
    assert out["resource_count"] == 0
    assert out["reliability_signal_count"] == 0
    assert out["by_resource"] == []
    assert out["by_service"] == []
    assert out["as_of"] is None


def test_cost_fallback_keys_are_counted():
    out = analyze_cost_vs_reliability([
        _line(10.0, rid=A, cost_key="cost"),
        _line(20.0, rid=B, cost_key="costInUSD"),
        _line(30.0, rid=C, cost_key="pretaxCost"),
    ])
    assert out["total_usd"] == 60.0
    assert out["resource_count"] == 3


def test_case_insensitive_resource_id_join():
    out = analyze_cost_vs_reliability([_line(100.0, rid=A.upper())], alerts=[_alert(A.lower())])
    assert out["coverage"]["joined_resource_count"] == 1
    assert out["by_resource"][0]["resourceId"] == A.upper()
    assert out["by_resource"][0]["alert_count"] == 1


def test_alerts_aggregate_by_resource_and_severity_weighting():
    out = analyze_cost_vs_reliability([
        _line(100.0, rid=A),
        _line(100.0, rid=B),
    ], alerts=[_alert(A, sev="Sev1"), _alert(A, sev="Sev3"), _alert(B, sev="Sev3")])
    rows = {r["resourceName"]: r for r in out["by_resource"]}
    assert rows["a"]["alert_count"] == 2
    assert rows["a"]["sev1"] == 1
    assert rows["a"]["sev3"] == 1
    assert rows["a"]["reliability_score"] == reliability.SEV_WEIGHTS["Sev1"] + reliability.SEV_WEIGHTS["Sev3"]
    assert rows["a"]["reliability_score"] > rows["b"]["reliability_score"]


def test_health_events_add_score_only_for_unavailable_or_degraded():
    out = analyze_cost_vs_reliability([_line(100.0, rid=A)], health_events=[
        _health(A, state="Unavailable"),
        _health(A, state="Degraded"),
        _health(A, state="Available"),
    ])
    row = out["by_resource"][0]
    assert row["health_event_count"] == 2
    assert row["reliability_score"] == reliability.HEALTH_EVENT_WEIGHT * 2


def test_advisor_highavailability_joins_and_non_ha_is_ignored():
    out = analyze_cost_vs_reliability([_line(100.0, rid=A)], advisor_recommendations=[
        _advisor(A, category="HighAvailability"),
        _advisor(A, category="Cost"),
    ])
    row = out["by_resource"][0]
    assert row["advisor_reliability_count"] == 1
    assert row["reliability_score"] == reliability.ADVISOR_HA_WEIGHT


def test_subscription_level_event_counted_but_not_joined():
    out = analyze_cost_vs_reliability([_line(100.0, rid=A)], alerts=[_alert("", sev="Sev0")])
    assert out["reliability_signal_count"] == 1
    assert out["coverage"]["subscription_level_event_count"] == 1
    assert out["coverage"]["joined_resource_count"] == 0
    assert out["unmatched_reliability"] == []


def test_high_incident_low_spend_hint_fires():
    out = analyze_cost_vs_reliability([_line(50.0, rid=A)], alerts=[_alert(A, sev="Sev0"), _alert(A, sev="Sev2")])
    assert {h["type"] for h in out["hints"]} >= {"high_incident_low_spend"}


def test_high_spend_no_pain_hint_fires():
    out = analyze_cost_vs_reliability([_line(1500.0, rid=A)])
    assert {h["type"] for h in out["hints"]} == {"high_spend_no_pain"}


def test_unmatched_reliability_hint_fires():
    out = analyze_cost_vs_reliability([_line(100.0, rid=A)], alerts=[_alert(B, sev="Sev1")])
    assert out["coverage"]["unmatched_reliability_count"] == 1
    assert out["unmatched_reliability"][0]["resourceId"] == B
    assert "unmatched_reliability" in {h["type"] for h in out["hints"]}


def test_by_service_rollup_aggregates_resource_rows():
    out = analyze_cost_vs_reliability([
        _line(100.0, rid=A, service="Microsoft.Compute"),
        _line(50.0, rid=B, service="Microsoft.Compute"),
        _line(25.0, rid=C, service="Microsoft.Cache"),
    ], alerts=[_alert(A, sev="Sev2"), _alert(B, sev="Sev3")])
    services = {s["service"]: s for s in out["by_service"]}
    assert services["Microsoft.Compute"]["monthly_usd"] == 150.0
    assert services["Microsoft.Compute"]["resource_count"] == 2
    assert services["Microsoft.Compute"]["alert_count"] == 2
    assert services["Microsoft.Compute"]["reliability_score"] == reliability.SEV_WEIGHTS["Sev2"] + reliability.SEV_WEIGHTS["Sev3"]


def test_top_n_caps_are_enforced():
    items = [_line(float(i), rid=f"{A}{i}") for i in range(1, 8)]
    alerts = [_alert(f"{A}{i}", sev="Sev3") for i in range(1, 8)]
    out = analyze_cost_vs_reliability(items, alerts=alerts, top_n_resources=3, top_n_services=1)
    assert len(out["by_resource"]) == 3
    assert len(out["by_service"]) == 1


def test_as_of_is_max_date_across_cost_and_events():
    out = analyze_cost_vs_reliability([
        _line(10.0, rid=A, date="2026-07-10"),
        _line(10.0, rid=B, date="2026-07-12"),
    ], alerts=[_alert(A, date="2026-07-16")], health_events=[_health(B, date="2026-07-14")])
    assert out["as_of"] == "2026-07-16"


def test_pain_per_1000_usd_divide_by_zero_guard():
    out = analyze_cost_vs_reliability([_line(0.0, rid=A)], alerts=[_alert(A, sev="Sev1")])
    row = out["by_resource"][0]
    assert row["pain_per_1000_usd"] is None
    assert row["reliability_score"] == reliability.SEV_WEIGHTS["Sev1"]


def test_risk_band_thresholds():
    out = analyze_cost_vs_reliability([
        _line(10.0, rid=A),
        _line(10.0, rid=B),
        _line(10.0, rid=C),
        _line(10.0, rid=D),
    ], alerts=[
        _alert(A, sev="Sev0"), _alert(A, sev="Sev2"),  # 11 high
        _alert(B, sev="Sev1"),                         # 5 medium
        _alert(C, sev="Sev3"),                         # 1 low
    ])
    bands = {r["resourceName"]: r["risk_band"] for r in out["by_resource"]}
    assert bands["a"] == "high"
    assert bands["b"] == "medium"
    assert bands["c"] == "low"
    assert bands["d"] == "none"


def test_inputs_are_not_mutated():
    items = [_line(10.0, rid=A)]
    alerts = [_alert(A)]
    before_items = [dict(items[0])]
    before_alert = [dict(alerts[0])]
    analyze_cost_vs_reliability(items, alerts=alerts)
    assert items == before_items
    assert alerts == before_alert
