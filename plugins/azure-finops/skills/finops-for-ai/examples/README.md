# FinOps for AI example

Run from the repository root. Foundry `AIServices` resources remain included because
classification uses `consumedService`, not the optional kind label.

The resource IDs below are offline fixture data. A live run retrieves UsageDetails independently
for every dynamically discovered managed scope, de-duplicates and filters the rows, and reports
included, excluded, and unattributed cost coverage.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/azure-finops/skills/finops-for-ai/attribute.py")
spec = importlib.util.spec_from_file_location("attribute", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

foundry = "/subscriptions/000/resourceGroups/ai/providers/Microsoft.CognitiveServices/accounts/chat-prod"
workspace = "/subscriptions/000/resourceGroups/ai/providers/Microsoft.MachineLearningServices/workspaces/ml-prod"
line_items = [
    {
        "cost": 1200.0, "consumedService": "Microsoft.CognitiveServices",
        "meterCategory": "Azure OpenAI", "meterSubCategory": "gpt-4o",
        "meterName": "gpt-4o Inp glbl Tokens", "resourceId": foundry,
        "date": "2026-07-31",
    },
    {
        "cost": 300.0, "consumedService": "Microsoft.CognitiveServices",
        "meterCategory": "Azure OpenAI", "meterSubCategory": "gpt-4o-mini",
        "meterName": "gpt-4o-mini Tokens", "resourceId": foundry,
        "date": "2026-07-31",
    },
    {
        "cost": 200.0, "consumedService": "Microsoft.MachineLearningServices",
        "meterCategory": "Azure Machine Learning", "meterSubCategory": "",
        "meterName": "Managed Online Endpoint vCPU", "resourceId": workspace,
        "date": "2026-07-31",
    },
]

report = module.attribute_ai_costs(
    line_items,
    resource_kinds={foundry: "AIServices", workspace: "Workspace"},
)
```

Representative output (abbreviated):

```json
{
  "as_of": "2026-07-31",
  "total_ai_usd": 1700.0,
  "resource_count": 2,
  "model_count": 2,
  "by_service_family": [
    {"service_family": "Cognitive Services / OpenAI", "monthly_usd": 1500.0, "pct": 88.2},
    {"service_family": "Machine Learning", "monthly_usd": 200.0, "pct": 11.8}
  ],
  "by_meter_type": [
    {"meter_type": "model_token", "monthly_usd": 1500.0, "pct": 88.2},
    {"meter_type": "compute", "monthly_usd": 200.0, "pct": 11.8}
  ],
  "by_resource": [
    {"resourceName": "chat-prod", "kind": "AIServices", "monthly_usd": 1500.0, "top_model": "gpt-4o"},
    {"resourceName": "ml-prod", "kind": "Workspace", "monthly_usd": 200.0, "top_model": ""}
  ],
  "by_model": [
    {"model": "gpt-4o", "monthly_usd": 1200.0, "pct": 80.0, "resource_count": 1},
    {"model": "gpt-4o-mini", "monthly_usd": 300.0, "pct": 20.0, "resource_count": 1}
  ],
  "top_drivers": [{"resourceName": "chat-prod", "model": "gpt-4o", "meter_type": "model_token", "monthly_usd": 1200.0}, "..."],
  "hints": [
    {"type": "commitment_opportunity", "target": "chat-prod", "monthly_usd": 1500.0},
    {"type": "model_concentration", "target": "gpt-4o", "monthly_usd": 1200.0},
    {"type": "compute_no_tokens_verify", "target": "ml-prod", "monthly_usd": 200.0}
  ]
}
```
