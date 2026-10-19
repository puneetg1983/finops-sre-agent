# Azure SRE Agent FinOps plugin

A standard [Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/) plugin marketplace with one
plugin, the read-only **azure-finops** plugin. It contains 11 skills and nothing else: no installer
script, no hooks, no out-of-band API calls.

Converted from [nirmash/finops-sre-agent-pack](https://github.com/nirmash/finops-sre-agent-pack) (MIT).
The ten analysis skills are carried over unchanged except for one wording update. The pack's installer
is replaced by the in-chat `finops-setup` skill.

## Install (end user)

1. **Builder → Plugins → Add marketplace** → `https://github.com/puneetg1983/sre-agent-finops-plugin`,
   or install from the official marketplace once it is published there.
2. Open **azure-finops** → **Install**.
3. In chat: **"Set up FinOps for me"**. Approve the permission prompt if one appears.

The agent grants its own read-only cost roles on your behalf, creates the seven scheduled tasks, and
produces the first Live Report. Say the same sentence again at any time to repair or update the setup.

Details: [plugins/azure-finops/README.md](plugins/azure-finops/README.md).

## Repository layout

```
.github/plugin/marketplace.json     marketplace manifest
plugins/azure-finops/plugin.json    plugin manifest  { "skills": ["skills/"] }
plugins/azure-finops/skills/        11 skills (finops-setup + 10 analysis skills)
tests/                              offline pytest suite
evals/                              offline eval harness from the source pack
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
