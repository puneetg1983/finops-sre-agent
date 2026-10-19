# Budget planning script example

Run from the repository root. Building a proposal and application script is pure and offline. The
agent returns the script for a human to review/save/run and never executes it.

The subscription ID below is placeholder input for the offline helper. In an interactive agent run,
the requested subscription, resource group, or management group is checked against the current
managed resources first. An outside scope requires explicit confirmation on a subsequent turn.

```python
import importlib.util
from datetime import date
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-budget-editor/recommend.py")
spec = importlib.util.spec_from_file_location("recommend", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

proposal = module.build_budget_proposal(
    scope="/subscriptions/000",
    name="prod-monthly",
    exact_budget=None,
    period_totals={
        "current_period_total": 800.0,
        "prior_complete_period_totals": [900.0, 950.0, 1000.0],
    },
    as_of=date(2026, 7, 15),
    headroom_pct=15,
    time_grain="Monthly",
    time_period={
        "startDate": "2026-07-01T00:00:00Z",
        "endDate": "2027-07-01T00:00:00Z",
    },
    contacts=["finops@contoso.com"],
)
application_script = proposal["application_script"]
```

Lead with `application_script` in the response. Representative proposal output (abbreviated):

```json
{
  "application_script": "#!/usr/bin/env bash\nset -euo pipefail\n...\naz rest --method put ...\n...\n",
  "operation": "create",
  "scope": "/subscriptions/000",
  "name": "prod-monthly",
  "before": null,
  "after": {"properties": {"amount": 2000.0}},
  "derivation": {"method": "usageDetailsActualCost", "rounded_amount": 2000.0},
  "warnings": [],
  "put_url": "https://management.azure.com/subscriptions/000/providers/Microsoft.Consumption/budgets/prod-monthly?api-version=2023-05-01",
  "put_body": {"properties": {"amount": 2000.0}},
  "command": "az rest --method put ...",
  "script": "#!/usr/bin/env bash\nset -euo pipefail\n...\n",
  "post_write_get_url": "https://management.azure.com/subscriptions/000/providers/Microsoft.Consumption/budgets/prod-monthly?api-version=2023-05-01"
}
```
