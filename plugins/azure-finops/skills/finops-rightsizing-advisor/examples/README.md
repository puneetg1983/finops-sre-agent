# Rightsizing advisor example

Run from the repository root. Utilization, cost, inventory, activity, and Advisor
records are joined by case-insensitive resource ID.

The resource ID below is offline fixture data. Live sources are queried separately for every
dynamically discovered managed scope; UsageDetails rows are de-duplicated and filtered before this
helper runs, with included, excluded, and unattributed cost coverage reported.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-rightsizing-advisor/rightsize.py")
spec = importlib.util.spec_from_file_location("rightsize", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

vm = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01"
resources = [{
    "resourceId": vm,
    "type": "microsoft.compute/virtualmachines",
    "sku": "Standard_D4s_v5",
}]
utilization = {
    vm: {"cpu_p95": 12.0, "cpu_avg": 5.0, "sample_days": 30},
}
costs = {vm: 200.0}

report = module.recommend_rightsizing(
    resources=resources,
    utilization=utilization,
    costs=costs,
)
```

Representative output:

```json
[{
  "resourceId": "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01",
  "resourceType": "microsoft.compute/virtualmachines",
  "kind": "oversized",
  "currentSku": "Standard_D4s_v5",
  "recommendedAction": "Rightsize down one tier.",
  "currentMonthlyUsd": 200.0,
  "estMonthlySavingsUsd": 100.0,
  "validated": true,
  "evidence": ["p95 CPU 12.0% over 30d (underutilized)."],
  "sources": ["azure-monitor"]
}]
```
