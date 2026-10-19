"""Layer-1 unit tests for the budget-governance skill (offline, deterministic)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-budget-governance"
    / "budget.py"
)
_spec = importlib.util.spec_from_file_location("budget", _PATH)
budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget)

evaluate_budgets = budget.evaluate_budgets

# Anchor every date-sensitive test so run-rate math is deterministic (day 15 of a 30-day window).
AS_OF = date(2026, 7, 15)


def _budget(name, amount, current, *, forecast=None, notifications=None,
            time_grain="Monthly", scope_id=None):
    props = {
        "amount": amount,
        "category": "Cost",
        "timeGrain": time_grain,
        "currentSpend": {"amount": current, "unit": "USD"},
    }
    if forecast is not None:
        props["forecastSpend"] = {"amount": forecast, "unit": "USD"}
    if notifications is not None:
        props["notifications"] = notifications
    return {
        "name": name,
        "id": (scope_id or f"/subscriptions/s/providers/Microsoft.Consumption/budgets/{name}"),
        "properties": props,
    }


def test_empty_budgets_is_a_first_class_finding():
    out = evaluate_budgets([], as_of=AS_OF)
    assert out["no_budgets"] is True
    assert out["budget_count"] == 0
    assert out["budgets"] == []
    assert out["gates"] == []


def test_none_input_is_treated_as_empty():
    out = evaluate_budgets(as_of=AS_OF)
    assert out["no_budgets"] is True


def test_over_budget_status_and_gate():
    b = _budget("m", 1000.0, 1100.0)
    out = evaluate_budgets([b], as_of=AS_OF)
    e = out["budgets"][0]
    assert e["status"] == "over_budget"
    assert e["pct_used"] == 110.0
    assert out["summary"]["over_budget"] == 1
    assert out["gates"][0]["name"] == "m"
    assert "1100.0 USD spent vs 1000.0" in out["gates"][0]["reason"]


def test_native_forecast_preferred_over_run_rate():
    # currentSpend 400 at day 15/30 would run-rate to ~800, but Azure says 1300 -> forecast_over.
    b = _budget("m", 1000.0, 400.0, forecast=1300.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_source"] == "azure"
    assert e["forecast_spend"] == 1300.0
    assert e["status"] == "forecast_over"
    assert e["pct_forecast"] == 130.0


def test_run_rate_fallback_when_forecast_absent():
    # No forecastSpend: day 15 of a 30-day month, 600 spent -> forecast ~1200 -> forecast_over.
    b = _budget("m", 1000.0, 600.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_source"] == "run-rate"
    assert e["forecast_spend"] == 1200.0
    assert e["status"] == "forecast_over"


def test_at_risk_band_by_actual_pct():
    # 85% spent, run-rate forecast still under budget on an already-late window -> at_risk.
    b = _budget("m", 1000.0, 850.0, forecast=950.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["status"] == "at_risk"
    assert out_gate_names(evaluate_budgets([b], as_of=AS_OF)) == []  # at_risk does not gate


def test_on_track_low_spend():
    b = _budget("m", 1000.0, 100.0, forecast=300.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["status"] == "on_track"


def test_budget_own_notification_thresholds_evaluated():
    notifications = {
        "actual_80": {"enabled": True, "threshold": 80, "thresholdType": "Actual"},
        "fcst_100": {"enabled": True, "threshold": 100, "thresholdType": "Forecasted"},
        "actual_50": {"enabled": True, "threshold": 50, "thresholdType": "Actual"},
    }
    b = _budget("m", 1000.0, 850.0, forecast=1200.0, notifications=notifications)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    names = [n["name"] for n in e["breached_notifications"]]
    # 85% actual breaches actual_80 and actual_50; 120% forecast breaches fcst_100.
    assert set(names) == {"actual_80", "fcst_100", "actual_50"}
    # sorted by threshold descending
    assert e["breached_notifications"][0]["threshold"] == 100


def test_disabled_notification_ignored():
    notifications = {"off": {"enabled": False, "threshold": 10, "thresholdType": "Actual"}}
    b = _budget("m", 1000.0, 900.0, forecast=950.0, notifications=notifications)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["breached_notifications"] == []


def test_mtd_crosscheck_flags_discrepancy():
    b = _budget("m", 1000.0, 500.0, forecast=900.0)
    out = evaluate_budgets([b], as_of=AS_OF, mtd_spend={"m": 650.0})
    cc = out["budgets"][0]["mtd_crosscheck"]
    assert cc["azure_current"] == 500.0
    assert cc["agent_mtd"] == 650.0
    assert cc["delta"] == 150.0
    assert cc["delta_pct"] == 30.0


def test_mtd_crosscheck_absent_when_not_provided():
    b = _budget("m", 1000.0, 500.0, forecast=900.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["mtd_crosscheck"] is None


def test_budgets_sorted_by_severity():
    ok = _budget("ok", 1000.0, 100.0, forecast=200.0)
    over = _budget("over", 1000.0, 1200.0)
    fcst = _budget("fcst", 1000.0, 400.0, forecast=1100.0)
    out = evaluate_budgets([ok, fcst, over], as_of=AS_OF)
    assert [e["name"] for e in out["budgets"]] == ["over", "fcst", "ok"]
    assert len(out["gates"]) == 2


def test_non_numeric_amount_does_not_crash():
    b = _budget("m", None, 500.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["amount"] is None
    assert e["pct_used"] is None
    assert e["status"] == "on_track"


def test_resource_group_scope_extracted():
    scope_id = ("/subscriptions/s/resourceGroups/rg1/providers/"
                "Microsoft.Consumption/budgets/m")
    b = _budget("m", 1000.0, 100.0, forecast=200.0, scope_id=scope_id)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["scope"] == "/subscriptions/s/resourceGroups/rg1"


def out_gate_names(out):
    return [g["name"] for g in out["gates"]]
