# Cost allocation example

Run from the repository root. Resource IDs are matched case-insensitively.

These resource IDs are offline fixture data. Live UsageDetails rows are retrieved per dynamically
discovered managed scope, de-duplicated, and filtered before allocation; the final report also
discloses included, excluded, and unattributed cost coverage.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-cost-allocation/allocate.py")
spec = importlib.util.spec_from_file_location("allocate", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

vm = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01"
cache = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Cache/Redis/cache-01"
logs = "/subscriptions/000/resourceGroups/shared/providers/Microsoft.Storage/storageAccounts/logs01"

costs = {vm: 140.0, cache: 60.0, logs: 50.0}
tags = {
    vm: {"team": "Payments", "env": "prod"},
    cache: {"team": "payments", "env": "prod"},
    logs: {"env": "prod"},
}

report = module.allocate_costs(costs, tags, dimension="team")
```

Representative output (abbreviated):

```json
{
  "dimension": "team",
  "total_usd": 250.0,
  "allocated_usd": 200.0,
  "unallocated_usd": 50.0,
  "unallocated_pct": 20.0,
  "groups": [{
    "owner": "Payments",
    "monthly_usd": 200.0,
    "pct": 80.0,
    "resource_count": 2
  }],
  "unallocated": {
    "monthly_usd": 50.0,
    "pct": 20.0,
    "resource_count": 1,
    "resources": [{"resourceId": ".../storageAccounts/logs01", "monthly_usd": 50.0}]
  },
  "tag_inventory": [
    {"key": "env", "resource_count": 3, "cost_usd": 250.0, "pct": 100.0},
    {"key": "team", "resource_count": 2, "cost_usd": 200.0, "pct": 80.0}
  ],
  "missing_recommended": ["service", "costCenter", "app", "owner"],
  "tag_hygiene": [{
    "canonical": "Payments",
    "variants": {"Payments": 140.0, "payments": 60.0},
    "cost_affected": 200.0
  }]
}
```
