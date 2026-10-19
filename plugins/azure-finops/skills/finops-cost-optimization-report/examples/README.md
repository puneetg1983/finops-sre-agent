# Cost optimization report example

This aggregator accepts the already-computed outputs of the anomaly, rightsizing,
allocation, and budget functions.

In a live run, those upstream analyses are executed separately for every dynamically discovered
managed scope and their UsageDetails coverage includes included, excluded, and unattributed cost.
The placeholder subscription below does not select or broaden scope.

```python
import importlib.util
from datetime import date
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-cost-optimization-report/summarize.py")
spec = importlib.util.spec_from_file_location("summarize", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

anomalies = [{
    "dimension": "meterCategory", "value": "Container Apps", "kind": "spike",
    "current_usd": 100.0, "baseline_mean_usd": 10.0, "impact_usd": 90.0,
}]
rightsizing = [{
    "resourceId": "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01",
    "kind": "oversized", "recommendedAction": "Rightsize down one tier.",
    "estMonthlySavingsUsd": 100.0, "validated": True,
}]
allocation = {
    "total_usd": 250.0, "unallocated_usd": 50.0, "unallocated_pct": 20.0,
    "untagged_usd": 0.0, "untagged_resources": [], "tag_hygiene": [],
}
budgets = {
    "budgets": [{
        "name": "prod-monthly", "amount": 1000.0, "current_spend": 800.0,
        "forecast_spend": 1600.0, "currency": "USD", "status": "forecast_over",
    }],
    "summary": {"over_budget": 0, "forecast_over": 1, "at_risk": 0, "on_track": 0},
    "no_budgets": False,
}

report = module.summarize_optimization(
    anomalies=anomalies,
    rightsizing=rightsizing,
    allocation=allocation,
    budgets=budgets,
    as_of=date(2026, 7, 15),
)
```

Representative output (abbreviated):

```json
{
  "as_of": "2026-07-15",
  "headline": {
    "total_monthly_spend": 250.0,
    "potential_monthly_savings": 100.0,
    "anomaly_count": 1,
    "top_anomaly_impact_usd": 90.0,
    "budgets_over": 0,
    "budgets_forecast_over": 1,
    "budgets_at_risk": 0,
    "untagged_usd": 0.0,
    "unallocated_pct": 20.0
  },
  "priorities": [
    {"rank": 1, "category": "budget", "impact_type": "overrun", "impact_usd": 600.0},
    {"rank": 2, "category": "rightsizing", "impact_type": "savings", "impact_usd": 100.0},
    {"rank": 3, "category": "anomaly", "impact_type": "spike", "impact_usd": 90.0}
  ],
  "rightsizing": {"potential_monthly_savings": 100.0, "count": 1, "top": ["..."]},
  "anomalies": {"count": 1, "top_impact_usd": 90.0, "top": ["..."]},
  "budgets": {"forecast_over": ["..."], "...": "abbreviated"},
  "governance": {"unallocated_usd": 50.0, "unallocated_pct": 20.0, "...": "abbreviated"}
}
```
