#!/usr/bin/env python3
"""finops-setup helper: dependency-free renderers used by the finops-setup skill.

All commands are offline. This script makes NO Azure calls and NO SRE Agent API calls;
the agent runs the az commands it prints through its built-in Azure CLI tools.

  discover      --agent-json FILE --identity-info TEXT
      From `az resource show --ids <agent> -o json` and
      `az account show --query user.assignedIdentityInfo -o tsv`, resolve the principal ID of
      the identity the agent's Azure tools run as, the managed scopes, the Cost Management
      transport scopes, and one role-assignment check command per required grant.

  grant-plan    --discovery FILE [--checks-dir DIR]
      Compare the saved check results (DIR/<key>.json, the JSON output of each check command)
      with the required roles and print the exact `az role assignment create` commands still
      needed. Missing or unreadable results count as "grant needed"; granting is idempotent.

  rbac-script   --agent-resource-id ID --client-id GUID
      Fallback only: a reviewable, idempotent Cloud Shell script for an Owner to run when the
      chatting user cannot grant roles through on-behalf-of (OBO).

  subagent-yaml --agent-resource-id ID [--python-tool RunInTerminal|ExecutePythonCode]
      Read-only finops-investigator ExtendedAgent YAML (only when the user asks for it).

  task-plan     --agent-resource-id ID [--alert-email E] [--github-repo owner/repo]
                [--existing FILE] [--only id,id] [--cron id=EXPR ...]
      JSON plan for the seven FinOps scheduled tasks (create | update | unchanged).

  scopes        --managed-json FILE
      Canonicalize managedResources and print the Cost Management transport scopes.
"""

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
TASKS_DIR = ASSETS / "tasks"
MANIFEST = TASKS_DIR / "task-manifest.json"
SCOPE_PROMPT = TASKS_DIR / "prompts" / "_managed-scope.txt"
AGENT_TEMPLATE = ASSETS / "finops-investigator.json"

REQUIRED_SKILLS = (
    "finops-managed-scope",
    "finops-cost-anomaly-detection",
    "finops-rightsizing-advisor",
    "finops-cost-allocation",
    "finops-budget-governance",
    "finops-budget-editor",
    "finops-cost-optimization-report",
    "finops-for-ai",
    "finops-cost-vs-reliability",
    "finops-report-renderer",
)
PYTHON_TOOLS = ("RunInTerminal", "ExecutePythonCode")

# Roles that already satisfy each requirement (assignments inherited from a parent scope count).
AGENT_READ_ROLES = ("Reader", "Contributor", "Owner")
COST_READ_ROLES = ("Cost Management Reader", "Cost Management Contributor", "Reader", "Contributor", "Owner")
AGENT_GRANT_ROLE = "Reader"
COST_GRANT_ROLE = "Cost Management Reader"

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_AGENT_ID = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}"
    r"/providers/Microsoft\.App/agents/[A-Za-z0-9._-]{1,128}$",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_CRON = re.compile(r"^[0-9*/,\- ]{9,64}$")
_KEY = re.compile(r"^[a-z0-9-]{1,40}$")
_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_SUB = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)
_RG = re.compile(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)", re.IGNORECASE)
_MG = re.compile(r"/providers/Microsoft\.Management/managementGroups/([^/]+)", re.IGNORECASE)


class SetupError(ValueError):
    """Invalid input; message is safe to show to the user."""


# --------------------------------------------------------------------------- validation
def agent_resource_id(value):
    value = (value or "").strip().rstrip("/")
    if not _AGENT_ID.fullmatch(value):
        raise SetupError(
            "agent resource ID must look like /subscriptions/<guid>/resourceGroups/<rg>"
            "/providers/Microsoft.App/agents/<name>"
        )
    return value


def guid(value, what):
    value = (value or "").strip()
    if not _GUID.fullmatch(value):
        raise SetupError(f"{what} must be a GUID")
    return value.lower()


def client_id(value):
    value = (value or "").strip()
    if value.lower().startswith("msiclient-"):
        value = value[len("msiclient-"):]
    return guid(value, "client ID (from `az account show --query user.assignedIdentityInfo`)")


def optional_email(value):
    value = (value or "").strip()
    if value and not _EMAIL.fullmatch(value):
        raise SetupError("alert email must be a single email address")
    return value


def optional_repo(value):
    value = (value or "").strip()
    if value and not _REPO.fullmatch(value):
        raise SetupError("GitHub repo must use owner/repo format")
    return value


# --------------------------------------------------------------------------- scopes
def _segment(value):
    if not value or value != value.strip() or any(ord(c) < 32 or c in "\\?#'\"`$ " for c in value):
        raise SetupError(f"unsafe scope segment: {value!r}")
    return value


def canonical_scope(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise SetupError("every managedResources entry must be a non-empty ARM scope ID")
    raw = raw.strip().rstrip("/")
    m = _SUB.fullmatch(raw)
    if m:
        return f"/subscriptions/{_segment(m.group(1))}"
    m = _RG.fullmatch(raw)
    if m:
        return f"/subscriptions/{_segment(m.group(1))}/resourceGroups/{_segment(m.group(2))}"
    m = _MG.fullmatch(raw)
    if m:
        return f"/providers/Microsoft.Management/managementGroups/{_segment(m.group(1))}"
    raise SetupError(f"unsupported managedResources scope: {raw}")


def managed_scopes(managed):
    if isinstance(managed, dict):
        managed = managed.get("managedResources", managed.get("value"))
    if not isinstance(managed, list) or not managed:
        raise SetupError(
            "the agent has no managed resources. Add subscriptions or resource groups to the "
            "agent first; FinOps tasks fail closed without them"
        )
    out, seen = [], set()
    for item in managed:
        if isinstance(item, dict):
            lowered = {str(k).casefold(): v for k, v in item.items()}
            item = next((lowered[k] for k in ("id", "resourceid", "scope") if lowered.get(k)), None)
        scope = canonical_scope(item)
        if scope.casefold() not in seen:
            seen.add(scope.casefold())
            out.append(scope)
    return out


def cost_reader_scopes(scopes):
    """UsageDetails is only exposed at subscription (for sub/RG boundaries) or MG scope."""
    out, seen = [], set()
    for scope in scopes:
        m = _RG.fullmatch(scope) or _SUB.fullmatch(scope)
        transport = f"/subscriptions/{m.group(1)}" if m else scope
        if transport.casefold() not in seen:
            seen.add(transport.casefold())
            out.append(transport)
    return out


# --------------------------------------------------------------------------- discovery
def resolve_principal(agent_doc, identity_info):
    """Map `az account show` user.assignedIdentityInfo to the identity's principal (object) ID."""
    identity = agent_doc.get("identity") or {}
    info = (identity_info or "").strip()
    if info.lower().startswith("msiclient-"):
        wanted = client_id(info)
        for resource_id, details in (identity.get("userAssignedIdentities") or {}).items():
            details = details or {}
            if str(details.get("clientId", "")).lower() == wanted:
                return {
                    "kind": "UserAssigned",
                    "clientId": wanted,
                    "principalId": guid(details.get("principalId"), "identity principalId"),
                    "identityResourceId": resource_id,
                }
        raise SetupError(
            f"the Azure tools run as client {wanted}, but no user-assigned identity with that "
            "client ID is attached to the agent resource"
        )
    if info.lower() in ("msi", "systemassignedidentity", "") and identity.get("principalId"):
        return {
            "kind": "SystemAssigned",
            "clientId": "",
            "principalId": guid(identity.get("principalId"), "system identity principalId"),
            "identityResourceId": "",
        }
    raise SetupError(f"cannot resolve the agent identity from assignedIdentityInfo={info!r}")


def _sub_of(scope, fallback):
    m = _SUB.match(scope)
    return m.group(1) if m else fallback


def _check_command(scope, principal, sub):
    return (
        f'az role assignment list --scope "{scope}" --include-inherited '
        "--fill-principal-name false "
        f"--query \"[?principalId=='{principal}'].roleDefinitionName\" -o json "
        f"--subscription {sub}"
    )


def discover(agent_doc, identity_info):
    if not isinstance(agent_doc, dict):
        raise SetupError("agent JSON must be the object returned by `az resource show`")
    agent_id = agent_resource_id(agent_doc.get("id"))
    agent_sub = _SUB.match(agent_id).group(1)
    principal = resolve_principal(agent_doc, identity_info)
    props = agent_doc.get("properties") or {}
    scopes = managed_scopes((props.get("knowledgeGraphConfiguration") or {}).get("managedResources"))
    cost_scopes = cost_reader_scopes(scopes)
    pid = principal["principalId"]

    requirements = [{
        "key": "agent-reader",
        "scope": agent_id,
        "subscription": agent_sub,
        "grantRole": AGENT_GRANT_ROLE,
        "satisfiedBy": list(AGENT_READ_ROLES),
        "why": "read managedResources on every run",
    }]
    for index, scope in enumerate(cost_scopes, start=1):
        requirements.append({
            "key": f"cost-{index}",
            "scope": scope,
            "subscription": _sub_of(scope, agent_sub),
            "grantRole": COST_GRANT_ROLE,
            "satisfiedBy": list(COST_READ_ROLES),
            "why": "read UsageDetails and budgets",
        })
    for req in requirements:
        req["checkCommand"] = _check_command(req["scope"], pid, req["subscription"])
    return {
        "agentResourceId": agent_id,
        "identity": principal,
        "managedScopes": scopes,
        "costReaderScopes": cost_scopes,
        "requirements": requirements,
    }


def _read_check(path):
    """Return the list of role names from a saved check result, or None if unknown."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):  # tolerate wrappers such as {"output": "[...]"}
        for key in ("output", "result", "value"):
            if key in data:
                data = data[key]
                break
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return None
    if not isinstance(data, list):
        return None
    roles = []
    for item in data:
        if isinstance(item, str):
            roles.append(item)
        elif isinstance(item, dict) and item.get("roleDefinitionName"):
            roles.append(str(item["roleDefinitionName"]))
    return roles


def grant_plan(discovery, checks_dir=None):
    principal = guid((discovery.get("identity") or {}).get("principalId"), "identity principalId")
    grants, satisfied, unknown = [], [], []
    for req in discovery.get("requirements") or []:
        if not _KEY.fullmatch(str(req.get("key", ""))):
            raise SetupError("malformed requirement key")
        scope = req["scope"]
        if not (_AGENT_ID.fullmatch(scope) or canonical_scope(scope) == scope):
            raise SetupError(f"malformed requirement scope: {scope}")
        sub = guid(req.get("subscription"), "subscription")
        if req["grantRole"] not in (AGENT_GRANT_ROLE, COST_GRANT_ROLE):
            raise SetupError("only read-only roles may be granted")
        roles = _read_check(Path(checks_dir) / f"{req['key']}.json") if checks_dir else None
        accepted = {r.casefold() for r in req.get("satisfiedBy") or []}
        if roles is not None and any(r.casefold() in accepted for r in roles):
            satisfied.append({"key": req["key"], "scope": scope, "existingRoles": roles})
            continue
        if roles is None:
            unknown.append(req["key"])
        grants.append({
            "key": req["key"],
            "role": req["grantRole"],
            "scope": scope,
            "why": req.get("why", ""),
            "command": (
                f"az role assignment create --assignee-object-id {principal} "
                f'--assignee-principal-type ServicePrincipal --role "{req["grantRole"]}" '
                f'--scope "{scope}" --subscription {sub}'
            ),
        })
    return {"principalId": principal, "grants": grants, "satisfied": satisfied, "uncheckedKeys": unknown}


# --------------------------------------------------------------------------- rbac script (fallback)
_RBAC_TEMPLATE = r"""#!/usr/bin/env bash
# FinOps pack - one-time RBAC for Azure SRE Agent (generated by the finops-setup skill).
# FALLBACK ONLY: use when the person chatting with the agent cannot grant roles via OBO.
#
# WHO RUNS THIS: someone with Owner or User Access Administrator on the agent resource and on
# the subscriptions / management groups the agent manages. Run it in Azure Cloud Shell (Bash).
# WHAT IT DOES (read-only roles only, idempotent, never removes anything):
#   1. Reader                 on the agent resource  -> lets each run read managedResources
#   2. Cost Management Reader on each cost scope     -> lets each run read UsageDetails/budgets
# Preview first:  DRY_RUN=1 bash finops-rbac.sh
set -euo pipefail

AGENT_RESOURCE_ID='__AGENT_RESOURCE_ID__'
CLIENT_ID='__CLIENT_ID__'
DRY_RUN="${DRY_RUN:-0}"

command -v az >/dev/null || { echo "az CLI not found (use Azure Cloud Shell)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found (use Azure Cloud Shell)"; exit 1; }
az account show >/dev/null || { echo "Run 'az login' first"; exit 1; }

echo "==> Resolving the agent identity (client ID $CLIENT_ID)"
PRINCIPAL_ID="$(az ad sp show --id "$CLIENT_ID" --query id -o tsv 2>/dev/null || true)"
if [ -z "$PRINCIPAL_ID" ]; then
  AGENT_SUB="$(printf '%s' "$AGENT_RESOURCE_ID" | cut -d/ -f3)"
  PRINCIPAL_ID="$(az identity list --subscription "$AGENT_SUB" \
    --query "[?clientId=='$CLIENT_ID'].principalId | [0]" -o tsv 2>/dev/null || true)"
fi
[ -n "$PRINCIPAL_ID" ] || { echo "Could not resolve principal ID for client $CLIENT_ID"; exit 1; }
echo "    principalId: $PRINCIPAL_ID"

echo "==> Reading the agent's managed resources"
MANAGED_JSON="$(az resource show --ids "$AGENT_RESOURCE_ID" \
  --query properties.knowledgeGraphConfiguration.managedResources -o json)"
COST_SCOPES="$(printf '%s' "$MANAGED_JSON" | python3 -c '
import json, re, sys
items = json.load(sys.stdin) or []
sub = re.compile(r"^/subscriptions/([^/]+)(?:/resourceGroups/[^/]+)?$", re.I)
mg = re.compile(r"^/providers/Microsoft\.Management/managementGroups/[^/]+$", re.I)
seen, out = set(), []
for item in items:
    if isinstance(item, dict):
        low = {str(k).casefold(): v for k, v in item.items()}
        item = next((low[k] for k in ("id", "resourceid", "scope") if low.get(k)), "")
    item = str(item).strip().rstrip("/")
    m = sub.match(item)
    scope = "/subscriptions/" + m.group(1) if m else (item if mg.match(item) else "")
    if not scope:
        sys.exit("Unsupported managedResources entry: " + item)
    if scope.casefold() not in seen:
        seen.add(scope.casefold()); out.append(scope)
if not out:
    sys.exit("The agent has no managedResources configured.")
print("\n".join(out))
')"
echo "$COST_SCOPES" | sed 's/^/    cost scope: /'

has_role() { # principal role scope -> 0 if an assignment exists at exactly that scope
  az role assignment list --assignee-object-id "$1" --role "$2" --scope "$3" -o json 2>/dev/null |
    ROLE="$2" SCOPE="$3" python3 -c '
import json, os, sys
items = json.load(sys.stdin)
ok = any(str(i.get("roleDefinitionName","")).casefold() == os.environ["ROLE"].casefold()
         and str(i.get("scope","")).casefold() == os.environ["SCOPE"].casefold() for i in items)
sys.exit(0 if ok else 1)'
}

ensure_role() { # role scope
  if has_role "$PRINCIPAL_ID" "$1" "$2"; then
    echo "    = $1 already on $2"; return
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "    + would grant $1 on $2"; return
  fi
  az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal --role "$1" --scope "$2" -o none
  echo "    + granted $1 on $2"
}

echo "==> Ensuring read-only roles"
ensure_role "Reader" "$AGENT_RESOURCE_ID"
while IFS= read -r scope; do
  [ -n "$scope" ] && ensure_role "Cost Management Reader" "$scope"
done <<< "$COST_SCOPES"

echo
echo "Done. Role assignments can take up to ~5 minutes to apply."
echo "Return to the SRE Agent chat and reply: done"
"""


def rbac_script(agent_id, client):
    script = _RBAC_TEMPLATE.replace("__AGENT_RESOURCE_ID__", agent_resource_id(agent_id))
    return script.replace("__CLIENT_ID__", client_id(client))


# --------------------------------------------------------------------------- subagent yaml
def subagent_yaml(agent_id, python_tool="RunInTerminal", name="finops-investigator"):
    agent_id = agent_resource_id(agent_id)
    if python_tool not in PYTHON_TOOLS:
        raise SetupError("python tool must be RunInTerminal or ExecutePythonCode")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or ""):
        raise SetupError("subagent name may only contain letters, digits, dot, underscore, hyphen")
    props = json.loads(AGENT_TEMPLATE.read_text())["properties"]
    instructions = props["instructions"]
    for placeholder in ("{{AGENT_RESOURCE_ID}}", "__AGENT_RESOURCE_ID__", "${AGENT_RESOURCE_ID}"):
        instructions = instructions.replace(placeholder, agent_id)
    if python_tool == "RunInTerminal":
        instructions = instructions.replace(
            "use RunAzCliReadCommands and sandbox Python for analysis",
            "use RunAzCliReadCommands and RunInTerminal for sandbox Python analysis",
        )
    if "{{" in instructions or agent_id not in instructions:
        raise SetupError("agent template is missing the AGENT_RESOURCE_ID placeholder")
    tools = ["RunAzCliReadCommands", python_tool, "ListReports", "GetReport", "SaveReport"]
    skills = list(props.get("allowedSkills") or [])
    missing = [s for s in REQUIRED_SKILLS if s not in skills]
    if missing:
        raise SetupError("agent template allowedSkills is missing: " + ", ".join(missing))
    lines = [
        "api_version: azuresre.ai/v2",
        "kind: ExtendedAgent",
        "metadata:",
        f"  name: {name}",
        "  tags:",
        "    - finops",
        "spec:",
        "  instructions: |",
        *[f"    {line}" if line else "" for line in instructions.splitlines()],
        f"  handoffDescription: {json.dumps(props['handoffDescription'], ensure_ascii=False)}",
        "  tools:",
        *[f"    - {tool}" for tool in tools],
        f"  allowParallelToolCalls: {str(bool(props.get('allowParallelToolCalls', True))).lower()}",
        f"  maxReflectionCount: {int(props.get('maxReflectionCount', 2))}",
        "  enableSkills: true",
        f"  addSystemSkills: {str(bool(props.get('addSystemSkills', True))).lower()}",
        "  allowedSkills:",
        *[f"    - {skill}" for skill in skills],
        f"  outputType: {props.get('outputType', 'markdown')}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- task plan
def _load_manifest():
    data = json.loads(MANIFEST.read_text())
    if data.get("schemaVersion") != 1 or len(data.get("tasks", [])) != 7:
        raise SetupError("unsupported task manifest")
    return data["tasks"], data.get("retiredTasks", [])


def _substitute(text, values):
    tokens = set(_TOKEN.findall(text))
    unknown = tokens - values.keys()
    if unknown:
        raise SetupError("unresolved prompt variables: " + ", ".join(sorted(unknown)))
    for key in tokens:
        text = text.replace("{{" + key + "}}", values[key])
    if "{{" in text or "}}" in text:
        raise SetupError("malformed prompt placeholder")
    return text.strip()


def _values(agent_id, email, repo):
    correlation = (
        "Correlate each anomaly only within its effective scope to deployments, Activity "
        f"Log writes, and commits plus merged PRs from `{repo}`."
        if repo
        else "Correlate each anomaly only within its effective scope to deployments and "
        "Activity Log writes. GitHub correlation is disabled because no repository was "
        "configured during FinOps setup."
    )

    def delivery(subject, importance):
        if email:
            return f"email `{email}` with subject `{subject} — <date>` and {importance} importance."
        return ("do not send email; no alert email was configured during FinOps setup, so "
                "return it only in the scheduled-task result.")

    return {
        "AGENT_RESOURCE_ID": agent_id,
        "ANOMALY_CORRELATION": correlation,
        "ANOMALY_DELIVERY": delivery("Cost anomaly detected", "High"),
        "RIGHTSIZE_DELIVERY": delivery("Weekly rightsizing review", "Normal"),
    }


def _existing_tasks(data):
    """Accept the scheduled-task list in any of the shapes the chat tools / API return."""
    if isinstance(data, dict):
        for key in ("value", "tasks", "items", "scheduledTasks"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        low = {str(k).casefold(): v for k, v in item.items()}
        name = low.get("name") or low.get("taskname")
        if name:
            out.append({
                "name": str(name),
                "id": str(low.get("id") or low.get("taskid") or ""),
                "cron": str(low.get("cronexpression") or low.get("cron") or ""),
                "prompt": str(low.get("agentprompt") or low.get("prompt") or ""),
            })
    return out


def task_plan(agent_id, email="", repo="", existing=None, only=None, cron_overrides=None):
    agent_id = agent_resource_id(agent_id)
    email, repo = optional_email(email), optional_repo(repo)
    tasks, retired = _load_manifest()
    ids = [t["id"] for t in tasks]
    only = [o.strip() for o in (only or []) if o.strip()]
    bad = [o for o in only if o not in ids]
    if bad:
        raise SetupError("unknown task id(s): " + ", ".join(bad) + "; valid: " + ", ".join(ids))
    cron_overrides = dict(cron_overrides or {})
    for key, expr in cron_overrides.items():
        if key not in ids or not _CRON.fullmatch(expr) or len(expr.split()) != 5:
            raise SetupError(f"invalid cron override {key}={expr}")
    current = _existing_tasks(existing or [])
    values = _values(agent_id, email, repo)
    scope_text = SCOPE_PROMPT.read_text()

    plan = []
    for task in tasks:
        if only and task["id"] not in only:
            continue
        task_values = dict(values)
        if task.get("defaultReportName"):
            task_values["REPORT_NAME"] = task["defaultReportName"]
        prompt = _substitute(scope_text, task_values) + "\n\n" + _substitute(
            (TASKS_DIR / task["prompt"]).read_text(), task_values)
        cron = cron_overrides.get(task["id"], task["defaultCron"])
        matches = [c for c in current if c["name"] == task["defaultName"]]
        if len(matches) > 1:
            raise SetupError(f"multiple existing tasks named {task['defaultName']!r}; cancel duplicates first")
        match = matches[0] if matches else None
        if match is None:
            action = "create"
        elif match["cron"] == cron and match["prompt"].strip() == prompt.strip():
            action = "unchanged"
        else:
            action = "update"
        plan.append({
            "id": task["id"],
            "action": action,
            "existingTaskId": match["id"] if match else "",
            "taskName": task["defaultName"],
            "description": task["description"],
            "cronExpression": cron,
            "agentPrompt": prompt,
            "liveReport": task.get("defaultReportName", ""),
        })

    retire = [
        {"taskName": r["defaultName"], "existingTaskId": c["id"], "reason": r.get("reason", "")}
        for r in retired for c in current if c["name"] == r["defaultName"]
    ]
    return {"agentResourceId": agent_id, "alertEmail": email, "githubRepo": repo,
            "tasks": plan, "retire": retire}


# --------------------------------------------------------------------------- cli
def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read JSON from {path}: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover")
    p.add_argument("--agent-json", type=Path, required=True)
    p.add_argument("--identity-info", required=True)

    p = sub.add_parser("grant-plan")
    p.add_argument("--discovery", type=Path, required=True)
    p.add_argument("--checks-dir", type=Path)

    p = sub.add_parser("scopes")
    p.add_argument("--managed-json", type=Path, required=True)

    p = sub.add_parser("rbac-script")
    p.add_argument("--agent-resource-id", required=True)
    p.add_argument("--client-id", required=True)

    p = sub.add_parser("subagent-yaml")
    p.add_argument("--agent-resource-id", required=True)
    p.add_argument("--python-tool", default="RunInTerminal", choices=PYTHON_TOOLS)
    p.add_argument("--name", default="finops-investigator")

    p = sub.add_parser("task-plan")
    p.add_argument("--agent-resource-id", required=True)
    p.add_argument("--alert-email", default="")
    p.add_argument("--github-repo", default="")
    p.add_argument("--existing", type=Path)
    p.add_argument("--only", default="")
    p.add_argument("--cron", action="append", default=[], metavar="ID=EXPR")

    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            out = discover(_load_json(args.agent_json), args.identity_info)
        elif args.command == "grant-plan":
            out = grant_plan(_load_json(args.discovery), args.checks_dir)
        elif args.command == "scopes":
            scopes = managed_scopes(_load_json(args.managed_json))
            out = {"managedScopes": scopes, "costReaderScopes": cost_reader_scopes(scopes)}
        elif args.command == "rbac-script":
            sys.stdout.write(rbac_script(args.agent_resource_id, args.client_id))
            return 0
        elif args.command == "subagent-yaml":
            sys.stdout.write(subagent_yaml(args.agent_resource_id, args.python_tool, args.name))
            return 0
        else:
            existing = _load_json(args.existing) if args.existing else []
            overrides = {}
            for item in args.cron:
                key, _, expr = item.partition("=")
                overrides[key.strip()] = expr.strip()
            out = task_plan(args.agent_resource_id, args.alert_email, args.github_repo,
                            existing, args.only.split(",") if args.only else None, overrides)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except SetupError as exc:
        print(f"finops-setup: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
