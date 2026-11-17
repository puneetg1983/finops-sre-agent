# Azure SRE Agent FinOps plugin

A standard [Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/) plugin marketplace with one
plugin, the read-only **azure-finops** plugin. It contains 11 skills and an `.mcp.json` that declares
the Azure Resource Manager (ARM) MCP server used for cost data: no installer script, no hooks, no
out-of-band API calls.

Converted from [nirmash/finops-sre-agent-pack](https://github.com/nirmash/finops-sre-agent-pack) (MIT).
The analysis skills now retrieve cost and budgets through ARM MCP (`query_costs`, `list_budgets`) to
avoid Consumption API throttling, with UsageDetails as a disclosed fallback. The pack's installer is
replaced by the in-chat `finops-setup` skill.

## Install (end user)

1. **Builder → Plugins → Add marketplace** → `https://github.com/puneetg1983/finops-sre-agent`,
   or install from the official marketplace once it is published there.
2. Open **azure-finops** → **Install**.
3. On the plugin page, under **Connectors**, select **Configure** for `azure-resource-manager`; choose
   **OAuth** with scope `api://22bfbae3-f4e7-485f-be43-8cee15065084/.default`, sign in, and select only
   its read tools. See [plugins/azure-finops/README.md](plugins/azure-finops/README.md#arm-mcp-connector).
4. In chat: **"Set up FinOps for me"**. Choose **Managed resources** or **Everything** when asked,
   then approve any required OBO permission prompt.

The agent grants missing read-only cost roles on your behalf and builds **one consolidated FinOps
dashboard** with 17 sections, local search, charts and print/PDF. One task refreshes it daily; a
separate managed-resource anomaly task handles alerts. Everything means all resources in the agent's
Azure tenant, discovered from its root management group, including unmanaged resources. It requires
Cost Management Reader at that root, approved via OBO. Other skills remain managed-resource-only.

Optional inputs (such as namespace allocation, business volumes, carbon data and billing exports)
show explicit unavailable/partial states until connected, never made-up metrics.
See the [Cloudability reporting research and coverage matrix](docs/CLOUDABILITY-COVERAGE.md).
Say the setup sentence again to repair or update; old schedules are retired only after the new
dashboard saves successfully. Historical reports are not deleted.

Details: [plugins/azure-finops/README.md](plugins/azure-finops/README.md) · Design: [docs/DESIGN.md](docs/DESIGN.md).

## Repository layout

```
.github/plugin/marketplace.json     marketplace manifest
plugins/azure-finops/plugin.json    plugin manifest  { "skills": ["skills/"] }
plugins/azure-finops/.mcp.json      ARM MCP server requirement (connector configured by the user)
plugins/azure-finops/skills/        11 skills (finops-setup + 10 analysis skills)
tests/                              offline pytest suite
evals/                              offline eval harness from the source pack
docs/DESIGN.md                      one-page design spec
```

## Test

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Contributing to Azure/sre-agent-plugins

Copy `plugins/azure-finops/` into `Azure/sre-agent-plugins/plugins/` and add its entry to that repo's
marketplace manifest. Keep the description of `finops_intelligence` (in `azure-proactive-operations`)
focused on quick assessments, so the two skills don't compete for the same requests.
