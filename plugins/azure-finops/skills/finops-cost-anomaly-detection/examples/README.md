# Cost anomaly detection example

Run from the repository root. The explicit `today` keeps this example
deterministic; production callers may let the function select the latest complete
day.

The resource ID below is offline fixture data. A live run retrieves and filters UsageDetails for
the dynamically discovered managed scopes before calling this helper, then reports included,
excluded, and unattributed cost coverage.

```python
import importlib.util
from datetime import date, timedelta
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-cost-anomaly-detection/detect.py")
spec = importlib.util.spec_from_file_location("detect", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

today = date(2026, 7, 14)
resource_id = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.App/containerApps/api"
line_items = [{
    "date": (today - timedelta(days=offset)).isoformat(),
    "cost": 10.0,
    "meterCategory": "Container Apps",
    "resourceGroup": "prod",
    "resourceId": resource_id,
    "tags": {"env": "prod"},
} for offset in range(1, 29)]
line_items.append({
    "date": today.isoformat(),
    "cost": 100.0,
    "meterCategory": "Container Apps",
    "resourceGroup": "prod",
    "resourceId": resource_id,
    "tags": {"env": "prod"},
})

report = module.detect_anomalies(
    line_items,
    today=today,
    dimensions=("meterCategory",),
)
```

Representative output:

```json
[{
  "dimension": "meterCategory",
  "value": "Container Apps",
  "date": "2026-07-14",
  "kind": "spike",
  "triggers": ["baseline", "wow"],
  "current_usd": 100.0,
  "baseline_mean_usd": 10.0,
  "baseline_std_usd": 0.0,
  "dod_delta_usd": 90.0,
  "dod_delta_pct": 900.0,
  "wow_delta_usd": 90.0,
  "impact_usd": 90.0
}]
```
