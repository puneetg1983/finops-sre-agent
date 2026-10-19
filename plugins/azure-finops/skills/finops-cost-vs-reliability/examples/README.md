# Cost versus reliability example

Run from the repository root. Inputs are flattened results from read-only cost and
reliability APIs.

The resource ID below is offline fixture data. Live cost and reliability inputs are queried per
dynamically discovered managed scope, de-duplicated, and filtered before analysis; the report must
also disclose included, excluded, and unattributed cost coverage.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-cost-vs-reliability/reliability.py")
spec = importlib.util.spec_from_file_location("reliability", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

resource_id = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01"
line_items = [{
    "cost": 1200.0, "resourceId": resource_id,
    "consumedService": "Microsoft.Compute", "date": "2026-07-31",
}]
alerts = [{
    "name": "API latency",
    "properties": {"essentials": {
        "severity": "Sev1",
        "alertTargetIDs": [resource_id],
        "startDateTime": "2026-07-30",
    }},
}]
health_events = [{
    "resourceId": resource_id,
    "properties": {
        "availabilityState": "Degraded",
        "occurredTime": "2026-07-29",
    },
}]

report = module.analyze_cost_vs_reliability(
    line_items,
    alerts=alerts,
    health_events=health_events,
)
```

Representative output (abbreviated):

```json
{
  "as_of": "2026-07-31",
  "total_usd": 1200.0,
  "resource_count": 1,
  "reliability_signal_count": 2,
  "coverage": {
    "cost_resource_count": 1,
    "reliability_resource_count": 1,
    "joined_resource_count": 1,
    "unmatched_reliability_count": 0,
    "subscription_level_event_count": 0
  },
  "by_resource": [{
    "resourceName": "api-01",
    "service": "Microsoft.Compute",
    "monthly_usd": 1200.0,
    "alert_count": 1,
    "sev1": 1,
    "health_event_count": 1,
    "reliability_score": 9.0,
    "pain_per_1000_usd": 7.5,
    "risk_band": "medium",
    "primary_signal": "alert"
  }],
  "by_service": [{"service": "Microsoft.Compute", "reliability_score": 9.0, "...": "abbreviated"}],
  "top_drivers": [{"target": "api-01", "monthly_usd": 1200.0, "reliability_score": 9.0}],
  "hints": [],
  "unmatched_reliability": [],
  "data_quality": {"sources_used": {"usage_details": true, "alerts": true, "resource_health": true, "advisor_highavailability": false}, "...": "abbreviated"}
}
```
