"""FinOps cost-optimization-report — pure, offline, deterministic (no Azure calls).

Rolls the four existing read-only FinOps analyses up into ONE executive summary:

  * cost anomalies        (finops-cost-anomaly-detection -> detect_anomalies -> list)
  * rightsizing savings   (finops-rightsizing-advisor    -> recommend_rightsizing -> list)
  * cost allocation       (finops-cost-allocation        -> allocate_costs -> dict)
  * budget status         (finops-budget-governance      -> evaluate_budgets -> dict)

This module does no data pulling of its own — the skill runs each existing core (each of
which pulls and shapes its own data), then hands their *already-computed* outputs here.
That keeps every analysis independently tested and keeps this a pure aggregation layer.

"Policy findings" here are governance signals derived from the existing outputs — not a new
data source: tag-hygiene + untagged/unallocated spend (from allocation) and budget gates
(from budget governance). No write, no new RBAC.

Output (see summarize_optimization): an executive `headline`, a blended, dollar-ranked
`priorities` list (each item labelled with an `impact_type` so savings, overruns, spikes and
governance exposure are never silently conflated), and per-section detail for the dashboard.
"""

from datetime import date

# How many rows each detail section and the blended priorities list keep.
_TOP_PRIORITIES = 10
_TOP_PER_SECTION = 8


def _num(value):
    """Return value as float, or None if it isn't a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _short_id(resource_id):
    """Last path segment of a resource id, for compact titles."""
    if not resource_id:
        return "(unknown)"
    return str(resource_id).rstrip("/").split("/")[-1]


def _rightsizing_rollup(findings):
    """Total quantified savings, count, and the top findings from recommend_rightsizing()."""
    findings = findings or []
    total = 0.0
    for f in findings:
        savings = _num(f.get("estMonthlySavingsUsd"))
        if savings:
            total += savings
    top = findings[:_TOP_PER_SECTION]  # already sorted by savings desc by the core
    return {
        "potential_monthly_savings": round(total, 2),
        "count": len(findings),
        "top": top,
    }


def _anomaly_rollup(anomalies):
    """Count, largest impact, and the top anomalies from detect_anomalies()."""
    anomalies = anomalies or []
    top_impact = _num(anomalies[0].get("impact_usd")) if anomalies else None
    return {
        "count": len(anomalies),
        "top_impact_usd": round(top_impact, 2) if top_impact is not None else None,
        "top": anomalies[:_TOP_PER_SECTION],  # already sorted by impact desc by the core
    }


def _budget_overrun_usd(b):
    """Dollar overage for a gated budget: spend-over-amount, else forecast-over-amount."""
    amount = _num(b.get("amount"))
    if amount is None:
        return None
    if b.get("status") == "over_budget":
        current = _num(b.get("current_spend"))
        return round(current - amount, 2) if current is not None else None
    if b.get("status") == "forecast_over":
        forecast = _num(b.get("forecast_spend"))
        return round(forecast - amount, 2) if forecast is not None else None
    return None


def _budget_rollup(budgets_result):
    """Split evaluated budgets into over / forecast_over / at_risk buckets for the report."""
    result = budgets_result or {}
    evaluated = result.get("budgets") or []
    buckets = {"over": [], "forecast_over": [], "at_risk": []}
    for b in evaluated:
        status = b.get("status")
        if status == "over_budget":
            buckets["over"].append(b)
        elif status == "forecast_over":
            buckets["forecast_over"].append(b)
        elif status == "at_risk":
            buckets["at_risk"].append(b)
    return {
        "no_budgets": bool(result.get("no_budgets")),
        "summary": result.get("summary") or {},
        "over": buckets["over"],
        "forecast_over": buckets["forecast_over"],
        "at_risk": buckets["at_risk"],
    }


def _governance_rollup(allocation, budget_roll):
    """Governance/policy findings from existing signals: tagging + budget gates."""
    alloc = allocation or {}
    gates = list(budget_roll["over"]) + list(budget_roll["forecast_over"])
    return {
        "untagged_usd": _num(alloc.get("untagged_usd")) or 0.0,
        "unallocated_usd": _num(alloc.get("unallocated_usd")) or 0.0,
        "unallocated_pct": alloc.get("unallocated_pct"),
        "untagged_top": (alloc.get("untagged_resources") or [])[:_TOP_PER_SECTION],
        "tag_hygiene": (alloc.get("tag_hygiene") or [])[:_TOP_PER_SECTION],
        "budget_gates": [
            {"name": b.get("name"), "status": b.get("status"),
             "overrun_usd": _budget_overrun_usd(b), "currency": b.get("currency")}
            for b in gates
        ],
    }


def _priorities(rightsizing, anomalies, budget_roll, governance):
    """One blended, dollar-ranked action list. Each item is labelled with an impact_type so
    savings, overruns, spikes, and governance exposure are comparable but never conflated."""
    items = []

    for f in rightsizing["top"]:
        items.append({
            "category": "rightsizing",
            "impact_type": "savings",
            "impact_usd": _num(f.get("estMonthlySavingsUsd")),
            "title": f"{f.get('recommendedAction') or 'Rightsize'} — {_short_id(f.get('resourceId'))}",
            "detail": f.get("kind"),
            "action": f.get("recommendedAction"),
            "validated": f.get("validated"),
        })

    for b in budget_roll["over"] + budget_roll["forecast_over"]:
        items.append({
            "category": "budget",
            "impact_type": "overrun",
            "impact_usd": _budget_overrun_usd(b),
            "title": f"Budget {b.get('name')} — {b.get('status')}",
            "detail": f"{b.get('current_spend')}/{b.get('amount')} {b.get('currency')} "
                      f"(forecast {b.get('forecast_spend')})",
            "action": "Review and adjust the budget or the spend driving it",
            "validated": None,
        })

    for a in anomalies["top"]:
        items.append({
            "category": "anomaly",
            "impact_type": "spike",
            "impact_usd": _num(a.get("impact_usd")),
            "title": f"{a.get('kind') or 'spike'}: {a.get('dimension')}={a.get('value')}",
            "detail": f"current {a.get('current_usd')} vs baseline {a.get('baseline_mean_usd')}",
            "action": "Investigate the spike and correlate to a change",
            "validated": None,
        })

    if governance["untagged_usd"] > 0:
        items.append({
            "category": "governance",
            "impact_type": "governance",
            "impact_usd": round(governance["untagged_usd"], 2),
            "title": f"Untagged spend — {len(governance['untagged_top'])}+ resources",
            "detail": f"{governance['unallocated_pct']}% of spend unallocated"
                      if governance["unallocated_pct"] is not None else "ownership tags missing",
            "action": "Tag resources so spend is attributable",
            "validated": None,
        })
    for h in governance["tag_hygiene"]:
        items.append({
            "category": "governance",
            "impact_type": "governance",
            "impact_usd": _num(h.get("cost_affected")),
            "title": f"Tag hygiene — '{h.get('canonical')}' split across {len(h.get('variants') or {})} spellings",
            "detail": "spend divided across inconsistent tag values",
            "action": "Consolidate the tag values",
            "validated": None,
        })

    # Rank by dollar impact; unknown-impact items sort last but are retained.
    items.sort(key=lambda i: (i["impact_usd"] is not None, i["impact_usd"] or 0.0), reverse=True)
    for rank, item in enumerate(items[:_TOP_PRIORITIES], start=1):
        item["rank"] = rank
    return items[:_TOP_PRIORITIES]


def summarize_optimization(*, anomalies=None, rightsizing=None, allocation=None,
                           budgets=None, as_of=None):
    """Roll the four FinOps analyses up into one executive optimization summary.

    anomalies    the list returned by detect_anomalies()          (or None)
    rightsizing  the list returned by recommend_rightsizing()     (or None)
    allocation   the dict returned by allocate_costs()            (or None)
    budgets      the dict returned by evaluate_budgets()          (or None)
    as_of        date the report is anchored to (defaults to today)

    Every input is optional; a missing analysis becomes an empty section rather than an error.

    Returns a dict:
      {
        "as_of",
        "headline": {total_monthly_spend, potential_monthly_savings, anomaly_count,
                     top_anomaly_impact_usd, budgets_over, budgets_forecast_over,
                     budgets_at_risk, untagged_usd, unallocated_pct},
        "priorities": [ {rank, category, impact_type, impact_usd, title, detail, action, validated} ],
        "rightsizing": {potential_monthly_savings, count, top:[...]},
        "anomalies": {count, top_impact_usd, top:[...]},
        "budgets": {no_budgets, summary, over:[...], forecast_over:[...], at_risk:[...]},
        "governance": {untagged_usd, unallocated_usd, unallocated_pct, untagged_top:[...],
                       tag_hygiene:[...], budget_gates:[...]},
      }
    """
    today = as_of or date.today()
    alloc = allocation or {}

    rightsizing_roll = _rightsizing_rollup(rightsizing)
    anomaly_roll = _anomaly_rollup(anomalies)
    budget_roll = _budget_rollup(budgets)
    governance_roll = _governance_rollup(alloc, budget_roll)
    priorities = _priorities(rightsizing_roll, anomaly_roll, budget_roll, governance_roll)

    summary = budget_roll["summary"]
    headline = {
        "total_monthly_spend": _num(alloc.get("total_usd")),
        "potential_monthly_savings": rightsizing_roll["potential_monthly_savings"],
        "anomaly_count": anomaly_roll["count"],
        "top_anomaly_impact_usd": anomaly_roll["top_impact_usd"],
        "budgets_over": summary.get("over_budget", 0),
        "budgets_forecast_over": summary.get("forecast_over", 0),
        "budgets_at_risk": summary.get("at_risk", 0),
        "untagged_usd": round(governance_roll["untagged_usd"], 2),
        "unallocated_pct": governance_roll["unallocated_pct"],
    }

    return {
        "as_of": today.isoformat(),
        "headline": headline,
        "priorities": priorities,
        "rightsizing": rightsizing_roll,
        "anomalies": anomaly_roll,
        "budgets": budget_roll,
        "governance": governance_roll,
    }
