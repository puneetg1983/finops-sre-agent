"""FinOps budget-governance — pure, offline, deterministic (no Azure calls).

Given the budgets returned by the read-only `GET Microsoft.Consumption/budgets` API,
evaluate each one against its amount: how much is spent month-to-date, where it is
forecast to land, which of the budget's own notification thresholds are breached, and
whether spend warrants a process gate (a human review before more is spent).

Design decisions (see SKILL.md):
  * Read-only. This never creates or edits a budget (that is the separate, planned
    `budget-editor` write skill). It only reads and reports.
  * Azure's own `forecastSpend` is used when present, but it is frequently absent on the
    GET response. When it is missing we compute a **linear run-rate** forecast in-sandbox
    (spend-so-far extrapolated across the current time-grain window) and label the source,
    so a budget is never left without a projection.
  * The budget's own `notifications` (each with a percent `threshold` and a `thresholdType`
    of Actual or Forecasted) are the customer's declared governance intent — we evaluate
    those directly rather than inventing thresholds, and fall back to sensible defaults only
    when a budget has none.
  * Empty input is a first-class result: "no budgets defined" is itself the finding (with a
    pointer to create one), not an error.

Input shape (the `value[]` array straight from the budgets GET; only these fields are read):

    [
      {
        "name": "monthly-sub",
        "id": ".../providers/Microsoft.Consumption/budgets/monthly-sub",
        "properties": {
          "amount": 1000.0,
          "category": "Cost",
          "timeGrain": "Monthly",
          "timePeriod": {"startDate": "2026-01-01T00:00:00Z", "endDate": "2027-01-01T00:00:00Z"},
          "currentSpend": {"amount": 640.0, "unit": "USD"},
          "forecastSpend": {"amount": 1120.0, "unit": "USD"},   # often absent
          "notifications": {
            "actual_80": {"enabled": true, "threshold": 80, "thresholdType": "Actual", "operator": "GreaterThan"},
            "fcst_100":  {"enabled": true, "threshold": 100, "thresholdType": "Forecasted", "operator": "GreaterThan"}
          }
        }
      }
    ]

Output: a single dict (see evaluate_budgets) — a per-budget evaluation, a portfolio summary,
and a `gates` list of the budgets that need a human decision. Feed it to a table / report.
"""

from collections import defaultdict
from datetime import date

# Fallback thresholds used ONLY when a budget declares no notifications of its own.
DEFAULT_ACTUAL_THRESHOLD = 100.0     # spent >= budget
DEFAULT_FORECAST_THRESHOLD = 100.0   # forecast to exceed budget
AT_RISK_PCT = 80.0                   # portfolio "at risk" band

# Approximate number of days per time-grain window (used only if timePeriod can't bound it).
_GRAIN_DAYS = {
    "monthly": 30,
    "billingmonth": 30,
    "quarterly": 91,
    "billingquarter": 91,
    "annually": 365,
    "billingannual": 365,
}


def _num(value):
    """Return value as float, or None if it isn't a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _spend_amount(block):
    """Read a {'amount': .., 'unit': ..} spend block; return (amount|None, unit|None)."""
    if not isinstance(block, dict):
        return None, None
    return _num(block.get("amount")), block.get("unit")


def _pct(part, whole):
    """part/whole as a rounded percentage; None when whole is missing/zero."""
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


def _parse_date(text):
    """Parse an ISO-ish date ('YYYY-MM-DD' or full timestamp) to date; None on failure."""
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _grain_fraction_elapsed(time_grain, time_period, as_of):
    """Fraction (0, 1] of the current time-grain window that has elapsed as of `as_of`.

    Prefers the budget's own timePeriod when it bounds a single window; otherwise falls
    back to an approximate day count for the grain. Returns None if it can't be computed.
    """
    grain = str(time_grain or "monthly").strip().lower()
    days_in_window = _GRAIN_DAYS.get(grain, 30)

    start = _parse_date((time_period or {}).get("startDate"))
    end = _parse_date((time_period or {}).get("endDate"))
    # Only trust timePeriod when it spans one grain window (not the multi-year active range
    # Azure returns for a recurring Monthly budget).
    if start and end and 0 < (end - start).days <= days_in_window + 2:
        days_in_window = (end - start).days
        elapsed = (as_of - start).days + 1
    elif grain in ("monthly", "billingmonth"):
        elapsed = min(as_of.day, days_in_window)
    else:
        elapsed = None

    if elapsed is None or days_in_window <= 0:
        return None
    elapsed = max(1, min(elapsed, days_in_window))
    return elapsed / days_in_window


def _notifications(props):
    """Yield (name, threshold_pct, thresholdType_lower, enabled) for each notification."""
    for name, n in (props.get("notifications") or {}).items():
        if not isinstance(n, dict):
            continue
        threshold = _num(n.get("threshold"))
        if threshold is None:
            continue
        ttype = str(n.get("thresholdType") or "Actual").strip().lower()
        enabled = n.get("enabled", True) is not False
        yield name, threshold, ttype, enabled


def evaluate_budgets(budgets=None, *, as_of=None, mtd_spend=None):
    """Evaluate read-only Azure budgets against amount, forecast, and their own thresholds.

    budgets    the `value[]` list from GET Microsoft.Consumption/budgets
    as_of      date the evaluation is anchored to (defaults to today) — drives run-rate forecast
    mtd_spend  optional {budgetName: actual_month_to_date_usd} from an independent UsageDetails
               pull, used only to cross-check Azure's currentSpend and flag large discrepancies

    Returns a dict:
      {
        "as_of", "budget_count",
        "budgets": [ {name, scope, amount, currency, current_spend, pct_used,
                      forecast_spend, forecast_source, pct_forecast, status,
                      breached_notifications: [{name, threshold, type}],
                      mtd_crosscheck: {agent_mtd, azure_current, delta, delta_pct} | None } ... ],
        "summary": {total_amount, total_current, total_forecast,
                    over_budget, forecast_over, at_risk, on_track},
        "gates": [ {name, reason, status} ... ],   # budgets needing a human decision
        "no_budgets": bool,                          # True + guidance when nothing is defined
      }
    """
    budgets = budgets or []
    mtd_spend = mtd_spend or {}
    today = as_of or date.today()
    # index the MTD cross-check case-insensitively by budget name
    mtd_by_name = {str(k).strip().lower(): _num(v) for k, v in mtd_spend.items()}

    if not budgets:
        return {
            "as_of": today.isoformat(),
            "budget_count": 0,
            "budgets": [],
            "summary": {
                "total_amount": 0.0, "total_current": 0.0, "total_forecast": 0.0,
                "over_budget": 0, "forecast_over": 0, "at_risk": 0, "on_track": 0,
            },
            "gates": [],
            "no_budgets": True,
        }

    evaluated = []
    tally = defaultdict(int)
    total_amount = total_current = total_forecast = 0.0

    for b in budgets:
        props = b.get("properties") or {}
        name = b.get("name") or props.get("name") or "(unnamed)"
        amount = _num(props.get("amount"))
        current, currency = _spend_amount(props.get("currentSpend"))
        current = current or 0.0
        currency = currency or "USD"

        # Forecast: prefer Azure's, else linear run-rate extrapolation of current spend.
        azure_forecast, _ = _spend_amount(props.get("forecastSpend"))
        if azure_forecast is not None:
            forecast, forecast_source = azure_forecast, "azure"
        else:
            frac = _grain_fraction_elapsed(props.get("timeGrain"), props.get("timePeriod"), today)
            if frac:
                forecast, forecast_source = round(current / frac, 2), "run-rate"
            else:
                forecast, forecast_source = None, "unavailable"

        pct_used = _pct(current, amount)
        pct_forecast = _pct(forecast, amount) if forecast is not None else None

        breached = _breached_notifications(props, pct_used, pct_forecast)
        status = _classify(amount, current, forecast, pct_used, breached)

        crosscheck = _mtd_crosscheck(mtd_by_name.get(str(name).strip().lower()), current)

        evaluated.append({
            "name": name,
            "scope": _scope_of(b, props),
            "amount": round(amount, 2) if amount is not None else None,
            "currency": currency,
            "current_spend": round(current, 2),
            "pct_used": pct_used,
            "forecast_spend": forecast,
            "forecast_source": forecast_source,
            "pct_forecast": pct_forecast,
            "status": status,
            "breached_notifications": breached,
            "mtd_crosscheck": crosscheck,
        })

        tally[status] += 1
        total_amount += amount or 0.0
        total_current += current
        total_forecast += forecast or 0.0

    evaluated.sort(key=lambda e: _severity(e["status"]), reverse=True)
    gates = [
        {"name": e["name"], "reason": _gate_reason(e), "status": e["status"]}
        for e in evaluated if e["status"] in ("over_budget", "forecast_over")
    ]

    return {
        "as_of": today.isoformat(),
        "budget_count": len(evaluated),
        "budgets": evaluated,
        "summary": {
            "total_amount": round(total_amount, 2),
            "total_current": round(total_current, 2),
            "total_forecast": round(total_forecast, 2),
            "over_budget": tally["over_budget"],
            "forecast_over": tally["forecast_over"],
            "at_risk": tally["at_risk"],
            "on_track": tally["on_track"],
        },
        "gates": gates,
        "no_budgets": False,
    }


def _breached_notifications(props, pct_used, pct_forecast):
    """Which of the budget's own notification thresholds are currently breached.

    Falls back to default Actual/Forecast thresholds only when the budget declares none.
    A Forecasted-type threshold with no available forecast is skipped (cannot evaluate).
    """
    declared = list(_notifications(props))
    if not declared:
        declared = [
            ("default_actual", DEFAULT_ACTUAL_THRESHOLD, "actual", True),
            ("default_forecast", DEFAULT_FORECAST_THRESHOLD, "forecasted", True),
        ]

    breached = []
    for name, threshold, ttype, enabled in declared:
        if not enabled:
            continue
        measured = pct_forecast if ttype == "forecasted" else pct_used
        if measured is None:
            continue
        if measured >= threshold:
            breached.append({"name": name, "threshold": threshold, "type": ttype})
    breached.sort(key=lambda x: x["threshold"], reverse=True)
    return breached


def _classify(amount, current, forecast, pct_used, breached):
    """Bucket a budget: over_budget > forecast_over > at_risk > on_track."""
    if amount is None:
        return "on_track"
    if current >= amount:
        return "over_budget"
    if forecast is not None and forecast >= amount:
        return "forecast_over"
    if pct_used is not None and pct_used >= AT_RISK_PCT:
        return "at_risk"
    if any(b["type"] == "actual" for b in breached):
        return "at_risk"
    return "on_track"


def _mtd_crosscheck(agent_mtd, azure_current):
    """Compare an independent month-to-date sum to Azure's currentSpend; None if not provided."""
    if agent_mtd is None:
        return None
    delta = round(agent_mtd - azure_current, 2)
    return {
        "agent_mtd": round(agent_mtd, 2),
        "azure_current": round(azure_current, 2),
        "delta": delta,
        "delta_pct": _pct(abs(delta), azure_current),
    }


def _scope_of(budget, props):
    """Best-effort human scope: the budget's resource-group/subscription id path, else category."""
    bid = str(budget.get("id") or "")
    if "/providers/Microsoft.Consumption" in bid:
        return bid.split("/providers/Microsoft.Consumption")[0]
    return props.get("category") or "subscription"


def _gate_reason(e):
    """One-line reason a budget is gated."""
    if e["status"] == "over_budget":
        return f"{e['current_spend']} {e['currency']} spent vs {e['amount']} budget ({e['pct_used']}%)"
    return f"forecast {e['forecast_spend']} {e['currency']} vs {e['amount']} budget ({e['pct_forecast']}%, {e['forecast_source']})"


_SEVERITY = {"over_budget": 3, "forecast_over": 2, "at_risk": 1, "on_track": 0}


def _severity(status):
    return _SEVERITY.get(status, 0)
