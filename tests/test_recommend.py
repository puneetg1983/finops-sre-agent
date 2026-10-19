"""Layer-1 unit tests for the budget-editor skill (offline, deterministic)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-budget-editor"
    / "recommend.py"
)
_spec = importlib.util.spec_from_file_location("recommend", _PATH)
recommend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recommend)

recommend_budgets = recommend.recommend_budgets

# Anchor date-sensitive tests so run-rate math is deterministic (day 15 of a 30-day window).
AS_OF = date(2026, 7, 15)


def _budget(name, amount, current, *, forecast=None, notifications=None,
            time_grain="Monthly", scope_id=None,
            time_period=None):
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
    if time_period is not None:
        props["timePeriod"] = time_period
    return {
        "name": name,
        "id": (scope_id or f"/subscriptions/s/providers/Microsoft.Consumption/budgets/{name}"),
        "eTag": '"etag-1"',
        "properties": props,
    }


def _only(out):
    return out["recommendations"][0]


def test_empty_budgets_is_a_first_class_finding():
    out = recommend_budgets([], as_of=AS_OF)
    assert out["no_budgets"] is True
    assert out["budget_count"] == 0
    assert out["recommendations"] == []


def test_none_input_is_treated_as_empty():
    out = recommend_budgets(as_of=AS_OF)
    assert out["no_budgets"] is True


def test_raise_when_forecast_exceeds_amount():
    # 800 spent at day 15/30 -> run-rate forecast 1600; 1600 * 1.15 = 1840 -> round up to 1900.
    b = _budget("m", 1000.0, 800.0)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["forecast_source"] == "run-rate"
    assert r["forecast_spend"] == 1600.0
    assert r["action"] == "raise"
    assert r["recommended_amount"] == 1900.0
    assert r["current_amount"] == 1000.0


def test_azure_forecast_preferred_over_run_rate():
    # currentSpend 400 -> run-rate ~800, but Azure forecast 1300 -> basis 1300 * 1.15 = 1495 -> 1500.
    b = _budget("m", 1000.0, 400.0, forecast=1300.0)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["forecast_source"] == "azure"
    assert r["recommended_amount"] == 1500.0
    assert r["action"] == "raise"


def test_tighten_when_budget_far_above_forecast():
    # Big budget, tiny forecast: 100 spent -> run-rate 200; 200 * 1.15 = 230 -> 250 << 10000 -> tighten.
    b = _budget("m", 10000.0, 100.0)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["action"] == "tighten"
    assert r["recommended_amount"] == 250.0


def test_keep_when_already_right_sized():
    # Azure forecast 850; 850 * 1.15 = 977.5 -> round up (step 50) to 1000; current 1000 -> keep.
    b = _budget("m", 1000.0, 450.0, forecast=850.0)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["recommended_amount"] == 1000.0
    assert r["action"] == "keep"


def test_set_when_no_current_amount():
    b = _budget("m", None, 600.0)  # run-rate 1200 -> 1380 -> 1400
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["action"] == "set"
    assert r["recommended_amount"] == 1400.0


def test_insufficient_data_when_no_spend_signal():
    # currentSpend 0 (unsynced) and no forecast -> cannot size.
    b = _budget("m", 1000.0, 0.0)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["action"] == "insufficient_data"
    assert r["recommended_amount"] is None
    assert r["command"] is None
    assert r["put_body"] is None


def test_buffer_pct_override_changes_amount():
    b = _budget("m", 1000.0, 500.0)  # run-rate 1000
    out = recommend_budgets([b], as_of=AS_OF, buffer_pct=10)
    # 1000 * 1.10 = 1100 -> round up to 1100.
    assert _only(out)["recommended_amount"] == 1100.0
    assert out["buffer_pct"] == 10


def test_command_is_a_put_with_recommended_amount():
    b = _budget("m", 1000.0, 800.0)
    r = _only(recommend_budgets([b], as_of=AS_OF, contacts=["team@example.org"]))
    assert "az rest --method put" in r["command"]
    assert "api-version=2023-05-01" in r["command"]
    assert r["put_body"]["properties"]["amount"] == r["recommended_amount"]
    assert r["put_url"].endswith("?api-version=2023-05-01")


def test_existing_notifications_carried_through_not_added():
    notifications = {
        "actual_80": {"enabled": True, "threshold": 80, "thresholdType": "Actual",
                      "contactEmails": ["team@example.com"]},
    }
    b = _budget("m", 1000.0, 800.0, notifications=notifications)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["notifications_added"] is False
    assert r["put_body"]["properties"]["notifications"] == notifications


def test_missing_notifications_never_emit_placeholder_payload():
    b = _budget("m", 1000.0, 800.0)  # no notifications
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["notifications_added"] is True
    assert r["requires_contacts"] is True
    assert r["put_body"] is None
    assert r["command"] is None


def test_real_contacts_enable_default_notification_payload():
    b = _budget("m", 1000.0, 800.0)
    r = _only(recommend_budgets(
        [b], as_of=AS_OF, contacts=["team@example.org"],
    ))
    notifs = r["put_body"]["properties"]["notifications"]
    assert set(notifs) == {"actual_80", "forecasted_100"}
    assert notifs["actual_80"]["contactEmails"] == ["team@example.org"]


def test_time_period_carried_into_body_when_present():
    tp = {"startDate": "2026-07-01T00:00:00Z", "endDate": "2027-07-01T00:00:00Z"}
    b = _budget("m", 1000.0, 800.0, time_period=tp)
    r = _only(recommend_budgets([b], as_of=AS_OF, contacts=["team@example.org"]))
    assert r["put_body"]["properties"]["timePeriod"]["startDate"] == "2026-07-01T00:00:00Z"


def test_recommendations_sorted_actionable_first():
    keep = _budget("keep", 1000.0, 450.0, forecast=850.0)   # keep
    raise_b = _budget("raise", 1000.0, 800.0)               # raise
    tighten = _budget("tighten", 10000.0, 100.0)            # tighten
    out = recommend_budgets([keep, tighten, raise_b], as_of=AS_OF)
    names = [r["name"] for r in out["recommendations"]]
    assert names[0] == "raise"
    assert names[-1] == "keep"
    assert out["summary"]["raise"] == 1
    assert out["summary"]["tighten"] == 1
    assert out["summary"]["keep"] == 1


def test_resource_group_scope_extracted():
    scope_id = ("/subscriptions/s/resourceGroups/rg1/providers/"
                "Microsoft.Consumption/budgets/m")
    b = _budget("m", 1000.0, 800.0, scope_id=scope_id)
    r = _only(recommend_budgets([b], as_of=AS_OF))
    assert r["scope"] == "/subscriptions/s/resourceGroups/rg1"
