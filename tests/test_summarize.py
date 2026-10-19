"""Layer-1 unit tests for the cost-optimization-report skill (offline, deterministic)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-cost-optimization-report"
    / "summarize.py"
)
_spec = importlib.util.spec_from_file_location("summarize", _PATH)
summarize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize)

summarize_optimization = summarize.summarize_optimization

AS_OF = date(2026, 7, 15)


def _anomaly(dimension, value, impact, *, kind="spike", current=100.0, baseline=40.0):
    return {
        "dimension": dimension, "value": value, "kind": kind,
        "current_usd": current, "baseline_mean_usd": baseline, "impact_usd": impact,
    }


def _finding(rid, savings, *, kind="idle", action="Delete idle resource", validated=True):
    return {
        "resourceId": rid, "kind": kind, "recommendedAction": action,
        "estMonthlySavingsUsd": savings, "validated": validated,
    }


def _budget(name, amount, current, forecast, status, currency="USD"):
    return {
        "name": name, "amount": amount, "current_spend": current,
        "forecast_spend": forecast, "currency": currency, "status": status,
    }


def _budgets_result(budgets):
    summary = {"over_budget": 0, "forecast_over": 0, "at_risk": 0, "on_track": 0}
    for b in budgets:
        key = {"over_budget": "over_budget", "forecast_over": "forecast_over",
               "at_risk": "at_risk", "on_track": "on_track"}[b["status"]]
        summary[key] += 1
    return {"budgets": budgets, "summary": summary, "no_budgets": not budgets}


def _allocation(total, untagged_usd, unallocated_usd, *, hygiene=None, untagged_resources=None):
    return {
        "total_usd": total, "unallocated_usd": unallocated_usd,
        "unallocated_pct": round(100.0 * unallocated_usd / total, 1) if total else 0.0,
        "untagged_usd": untagged_usd,
        "untagged_resources": untagged_resources or [],
        "tag_hygiene": hygiene or [],
    }


def test_all_empty_is_a_clean_report():
    out = summarize_optimization(as_of=AS_OF)
    assert out["priorities"] == []
    assert out["headline"]["potential_monthly_savings"] == 0.0
    assert out["headline"]["total_monthly_spend"] is None
    assert out["headline"]["anomaly_count"] == 0
    assert out["budgets"]["no_budgets"] is False  # None budgets -> empty, not "no_budgets defined"
    assert out["rightsizing"]["top"] == []


def test_rightsizing_rollup_totals_and_top():
    findings = [_finding("a", 300.0), _finding("b", 120.0), _finding("c", None)]
    out = summarize_optimization(rightsizing=findings, as_of=AS_OF)
    assert out["rightsizing"]["potential_monthly_savings"] == 420.0
    assert out["rightsizing"]["count"] == 3
    assert out["headline"]["potential_monthly_savings"] == 420.0


def test_anomaly_rollup_count_and_top_impact():
    anomalies = [_anomaly("service", "AOAI", 500.0), _anomaly("rg", "prod", 200.0)]
    out = summarize_optimization(anomalies=anomalies, as_of=AS_OF)
    assert out["anomalies"]["count"] == 2
    assert out["anomalies"]["top_impact_usd"] == 500.0
    assert out["headline"]["top_anomaly_impact_usd"] == 500.0


def test_budget_buckets_and_overrun_math():
    budgets = _budgets_result([
        _budget("over", 1000.0, 1200.0, 1300.0, "over_budget"),
        _budget("fcst", 1000.0, 400.0, 1150.0, "forecast_over"),
        _budget("risk", 1000.0, 850.0, 950.0, "at_risk"),
    ])
    out = summarize_optimization(budgets=budgets, as_of=AS_OF)
    assert [b["name"] for b in out["budgets"]["over"]] == ["over"]
    assert [b["name"] for b in out["budgets"]["forecast_over"]] == ["fcst"]
    assert [b["name"] for b in out["budgets"]["at_risk"]] == ["risk"]
    gates = {g["name"]: g["overrun_usd"] for g in out["governance"]["budget_gates"]}
    assert gates["over"] == 200.0        # 1200 - 1000 (actual over)
    assert gates["fcst"] == 150.0        # 1150 - 1000 (forecast over)
    assert out["headline"]["budgets_over"] == 1
    assert out["headline"]["budgets_forecast_over"] == 1
    assert out["headline"]["budgets_at_risk"] == 1


def test_governance_from_allocation_and_gates():
    hygiene = [{"canonical": "TeamA", "variants": {"TeamA": 90.0, "team-a": 60.0}, "cost_affected": 150.0}]
    alloc = _allocation(1000.0, untagged_usd=250.0, unallocated_usd=250.0,
                        hygiene=hygiene, untagged_resources=[{"resourceId": "x", "monthly_usd": 250.0}])
    out = summarize_optimization(allocation=alloc, as_of=AS_OF)
    gov = out["governance"]
    assert gov["untagged_usd"] == 250.0
    assert gov["unallocated_pct"] == 25.0
    assert gov["tag_hygiene"][0]["cost_affected"] == 150.0
    assert out["headline"]["untagged_usd"] == 250.0


def test_priorities_blended_ranked_and_labelled():
    findings = [_finding("save", 300.0)]
    anomalies = [_anomaly("service", "AOAI", 500.0)]
    budgets = _budgets_result([_budget("over", 1000.0, 1200.0, 1300.0, "over_budget")])
    alloc = _allocation(1000.0, untagged_usd=250.0, unallocated_usd=250.0)
    out = summarize_optimization(anomalies=anomalies, rightsizing=findings,
                                 allocation=alloc, budgets=budgets, as_of=AS_OF)
    prio = out["priorities"]
    # ranked by dollar impact desc: anomaly 500 > rightsizing 300 > untagged 250 > budget overrun 200
    assert [p["impact_type"] for p in prio] == ["spike", "savings", "governance", "overrun"]
    assert [p["impact_usd"] for p in prio] == [500.0, 300.0, 250.0, 200.0]
    assert [p["rank"] for p in prio] == [1, 2, 3, 4]
    # savings and overrun are distinct types, never merged into one number
    assert {p["category"] for p in prio} == {"anomaly", "rightsizing", "governance", "budget"}


def test_unknown_impact_items_kept_and_sorted_last():
    findings = [_finding("known", 300.0), _finding("unknown", None)]
    out = summarize_optimization(rightsizing=findings, as_of=AS_OF)
    prio = out["priorities"]
    assert prio[0]["impact_usd"] == 300.0
    assert prio[-1]["impact_usd"] is None
    assert len(prio) == 2


def test_missing_inputs_become_empty_sections():
    out = summarize_optimization(rightsizing=[_finding("a", 100.0)], as_of=AS_OF)
    assert out["anomalies"]["count"] == 0
    assert out["governance"]["untagged_usd"] == 0.0
    assert out["budgets"]["over"] == []
    assert out["headline"]["total_monthly_spend"] is None


def test_priorities_capped_at_ten():
    # 8 rightsizing (top-per-section cap) + 8 anomalies = 16 candidates -> priorities cap at 10.
    findings = [_finding(f"r{i}", float(1000 - i)) for i in range(8)]
    anomalies = [_anomaly("service", f"s{i}", float(100 - i)) for i in range(8)]
    out = summarize_optimization(rightsizing=findings, anomalies=anomalies, as_of=AS_OF)
    assert len(out["priorities"]) == 10
    assert out["priorities"][0]["impact_usd"] == 1000.0
    assert out["priorities"][0]["impact_type"] == "savings"
