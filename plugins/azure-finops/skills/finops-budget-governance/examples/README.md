# Budget governance example

Run from the repository root. `mtd_spend` demonstrates an optional independently supplied
cross-check keyed by budget name; the scheduled skill relies on the budgets GET and does not
perform a separate UsageDetails pull for this value.

The subscription ID below is fixture data. A live run first discovers the current managed
subscription, resource-group, and management-group scopes from `AGENT_RESOURCE_ID` and performs the
budgets GET separately for each effective scope. Scheduled discovery failure stops the run.

```python
import importlib.util
from datetime import date
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-budget-governance/budget.py")
spec = importlib.util.spec_from_file_location("budget", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

budgets = [{
    "name": "prod-monthly",
    "id": "/subscriptions/000/providers/Microsoft.Consumption/budgets/prod-monthly",
    "properties": {
        "amount": 1000.0,
        "timeGrain": "Monthly",
        "currentSpend": {"amount": 800.0, "unit": "USD"},
        "notifications": {
            "actual_80": {
                "enabled": True, "threshold": 80, "thresholdType": "Actual"
            },
            "forecast_100": {
                "enabled": True, "threshold": 100, "thresholdType": "Forecasted"
            },
        },
    },
}]

report = module.evaluate_budgets(
    budgets,
    as_of=date(2026, 7, 15),
    mtd_spend={"prod-monthly": 825.0},
)
```

Representative output (abbreviated):

```json
{
  "as_of": "2026-07-15",
  "budget_count": 1,
  "budgets": [{
    "name": "prod-monthly",
    "amount": 1000.0,
    "current_spend": 800.0,
    "pct_used": 80.0,
    "forecast_spend": 1600.0,
    "forecast_source": "run-rate",
    "pct_forecast": 160.0,
    "status": "forecast_over",
    "breached_notifications": [
      {"name": "forecast_100", "threshold": 100.0, "type": "forecasted"},
      {"name": "actual_80", "threshold": 80.0, "type": "actual"}
    ],
    "mtd_crosscheck": {
      "agent_mtd": 825.0, "azure_current": 800.0, "delta": 25.0, "delta_pct": 3.1
    }
  }],
  "summary": {"forecast_over": 1, "...": "abbreviated"},
  "gates": [{
    "name": "prod-monthly",
    "status": "forecast_over",
    "reason": "forecast 1600.0 USD vs 1000.0 budget (160.0%, run-rate)"
  }],
  "no_budgets": false
}
```
