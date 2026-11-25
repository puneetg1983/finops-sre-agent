"""Offline tests for the finops-setup helper (setup.py)."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "azure-finops"
SETUP_DIR = PLUGIN / "skills" / "finops-setup"
_spec = importlib.util.spec_from_file_location("finops_setup", SETUP_DIR / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

SUB = "11111111-2222-3333-4444-555555555555"
SUB2 = "66666666-7777-8888-9999-000000000000"
AGENT = f"/subscriptions/{SUB}/resourceGroups/rg-ops/providers/Microsoft.App/agents/my-agent"
CLIENT = "c08a2446-8155-4371-b458-7821eb33c88b"
UAMI_PRINCIPAL = "83237663-cbb6-4484-a7c0-d4013761fdab"
SAMI_PRINCIPAL = "8f4cdad7-20af-4b2f-a5d4-6f53a2e83eef"


def agent_doc(managed=None):
    return {
        "id": AGENT,
        "identity": {
            "tenantId": SUB2,
            "principalId": SAMI_PRINCIPAL,
            "type": "SystemAssigned, UserAssigned",
            "userAssignedIdentities": {
                f"/subscriptions/{SUB}/resourcegroups/rg-ops/providers/Microsoft.ManagedIdentity/userAssignedIdentities/a": {
                    "clientId": CLIENT, "principalId": UAMI_PRINCIPAL},
                f"/subscriptions/{SUB}/resourcegroups/rg-ops/providers/Microsoft.ManagedIdentity/userAssignedIdentities/b": {
                    "clientId": "ab7fdd27-53b5-47dc-89b8-9b2bce8016a3", "principalId": "73f73976-4a08-4a66-8878-e8497d1f1dfb"},
            },
        },
        "properties": {"knowledgeGraphConfiguration": {"managedResources": managed if managed is not None else [
            f"/subscriptions/{SUB}/resourceGroups/rg1",
            f"/subscriptions/{SUB}/resourceGroups/rg2",
            f"/subscriptions/{SUB2}",
            "/providers/Microsoft.Management/managementGroups/mg1",
        ]}},
    }


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("bad", ["", "/subscriptions/x", AGENT + "'; rm -rf /", AGENT.replace("agents", "sites")])
def test_rejects_bad_agent_ids(bad):
    with pytest.raises(setup.SetupError):
        setup.agent_resource_id(bad)


def test_client_id_accepts_msiclient_prefix():
    assert setup.client_id("MSIClient-" + CLIENT.upper()) == CLIENT


@pytest.mark.parametrize("bad", ["", "not-a-guid", CLIENT + "$(id)"])
def test_rejects_bad_client_ids(bad):
    with pytest.raises(setup.SetupError):
        setup.client_id(bad)


# ------------------------------------------------------------------ discovery
def test_discover_picks_the_identity_the_azure_tools_run_as():
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    assert disc["identity"]["principalId"] == UAMI_PRINCIPAL
    assert disc["identity"]["kind"] == "UserAssigned"
    assert disc["costReaderScopes"] == [
        f"/subscriptions/{SUB}",
        f"/subscriptions/{SUB2}",
        "/providers/Microsoft.Management/managementGroups/mg1",
    ]
    keys = [r["key"] for r in disc["requirements"]]
    assert keys == ["agent-reader", "cost-1", "cost-2", "cost-3"]
    for req in disc["requirements"]:
        cmd = req["checkCommand"]
        assert cmd.startswith("az role assignment list ")
        assert "--include-inherited" in cmd and "--fill-principal-name false" in cmd
        assert UAMI_PRINCIPAL in cmd and "--subscription " in cmd


def test_discover_system_assigned_identity():
    disc = setup.discover(agent_doc(), "MSI")
    assert disc["identity"] == {"kind": "SystemAssigned", "clientId": "",
                                "principalId": SAMI_PRINCIPAL, "identityResourceId": ""}


def test_discover_fails_closed():
    with pytest.raises(setup.SetupError, match="no managed resources"):
        setup.discover(agent_doc(managed=[]), "MSIClient-" + CLIENT)
    with pytest.raises(setup.SetupError, match="no user-assigned identity"):
        setup.discover(agent_doc(), "MSIClient-00000000-0000-0000-0000-000000000000")
    with pytest.raises(setup.SetupError):
        setup.discover(agent_doc(managed=["/subscriptions/a b"]), "MSI")


# ------------------------------------------------------------------ grant plan
def _write_checks(tmp_path, results):
    d = tmp_path / "checks"
    d.mkdir()
    for key, value in results.items():
        (d / f"{key}.json").write_text(value if isinstance(value, str) else json.dumps(value))
    return d


def test_grant_plan_only_grants_what_is_missing(tmp_path):
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    checks = _write_checks(tmp_path, {
        "agent-reader": ["Contributor"],          # inherited from RG -> satisfied
        "cost-1": [],                             # nothing -> grant
        "cost-2": ["Monitoring Reader"],          # wrong role -> grant
        # cost-3 missing -> unknown -> grant (idempotent)
    })
    plan = setup.grant_plan(disc, checks)
    assert [s["key"] for s in plan["satisfied"]] == ["agent-reader"]
    assert [g["key"] for g in plan["grants"]] == ["cost-1", "cost-2", "cost-3"]
    assert plan["uncheckedKeys"] == ["cost-3"]
    for grant in plan["grants"]:
        assert grant["role"] == "Cost Management Reader"
        assert grant["command"].startswith(
            f"az role assignment create --assignee-object-id {UAMI_PRINCIPAL} "
            "--assignee-principal-type ServicePrincipal --role \"Cost Management Reader\" --scope ")
        assert "--subscription " in grant["command"]
    assert f"--subscription {SUB2}" in plan["grants"][1]["command"]


def test_grant_plan_everything_in_place(tmp_path):
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    checks = _write_checks(tmp_path, {
        "agent-reader": ["Reader"], "cost-1": ["Cost Management Reader"],
        "cost-2": '{"output": "[\\"Reader\\"]"}',  # wrapped tool output is tolerated
        "cost-3": [{"roleDefinitionName": "Cost Management Contributor"}],
    })
    plan = setup.grant_plan(disc, checks)
    assert plan["grants"] == [] and len(plan["satisfied"]) == 4


def test_grant_plan_without_checks_grants_all_and_never_writes_roles():
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    plan = setup.grant_plan(disc)
    assert len(plan["grants"]) == 4
    assert plan["grants"][0]["role"] == "Reader" and plan["grants"][0]["scope"] == AGENT
    text = json.dumps(plan).lower()
    for forbidden in ("contributor\" --", "owner", "user access administrator", "delete"):
        assert forbidden not in text


def test_grant_plan_rejects_tampered_discovery():
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    disc["requirements"][1]["grantRole"] = "Owner"
    with pytest.raises(setup.SetupError, match="read-only"):
        setup.grant_plan(disc)
    disc = setup.discover(agent_doc(), "MSIClient-" + CLIENT)
    disc["requirements"][1]["scope"] = f"/subscriptions/{SUB}\" ; rm -rf / \""
    with pytest.raises(setup.SetupError):
        setup.grant_plan(disc)


# ------------------------------------------------------------------ rbac fallback script
def test_rbac_script_is_read_only_idempotent_and_valid_bash():
    script = setup.rbac_script(AGENT, CLIENT)
    assert f"AGENT_RESOURCE_ID='{AGENT}'" in script
    assert f"CLIENT_ID='{CLIENT}'" in script
    assert "set -euo pipefail" in script and "DRY_RUN" in script
    assert 'ensure_role "Reader" "$AGENT_RESOURCE_ID"' in script
    assert 'ensure_role "Cost Management Reader"' in script
    assert "role assignment delete" not in script
    if sys.platform != "win32" and shutil.which("bash"):
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


# ------------------------------------------------------------------ subagent yaml
def test_subagent_yaml_is_read_only_and_bound_to_agent():
    text = setup.subagent_yaml(AGENT)
    assert text.startswith("api_version: azuresre.ai/v2\nkind: ExtendedAgent\n")
    assert AGENT in text and "{{" not in text
    assert "RunAzCliWriteCommands" not in text
    assert "    - RunInTerminal" in text
    for skill in setup.REQUIRED_SKILLS:
        assert f"    - {skill}" in text
    alt = setup.subagent_yaml(AGENT, python_tool="ExecutePythonCode")
    assert "    - ExecutePythonCode" in alt and "    - RunInTerminal" not in alt


def test_subagent_yaml_assigns_only_read_only_arm_mcp_tools():
    text = setup.subagent_yaml(AGENT)
    assert "  mcpTools:\n    - azure-resource-manager_query_costs\n" in text
    assert "    - azure-resource-manager_list_budgets" in text
    for write_tool in ("create_budget", "create_or_update_resource", "create_deployment",
                       "execute_resource_action", "cancel_deployment", "/*"):
        assert write_tool not in text.split("  mcpTools:")[1]
    renamed = setup.subagent_yaml(AGENT, arm_mcp_connection="arm-mcp")
    assert "    - arm-mcp_query_costs" in renamed
    assert "mcpTools" not in setup.subagent_yaml(AGENT, arm_mcp_connection="")
    with pytest.raises(setup.SetupError):
        setup.subagent_yaml(AGENT, arm_mcp_connection="bad name")


# ------------------------------------------------------------------ task plan
def test_task_plan_creates_one_dashboard_and_anomaly_task():
    plan = setup.task_plan(AGENT)
    assert len(plan["tasks"]) == 2
    assert {t["action"] for t in plan["tasks"]} == {"create"}
    for task in plan["tasks"]:
        prompt = task["agentPrompt"]
        assert "{{" not in prompt and AGENT in prompt
        assert prompt.startswith(("STRICT MANAGED-SCOPE PREAMBLE", "DASHBOARD SCOPE POLICY"))
        assert "RunAzCliWriteCommands" not in prompt and "finops-budget-editor" not in prompt
        assert len(task["cronExpression"].split()) == 5
    assert [t["liveReport"] for t in plan["tasks"] if t["liveReport"]] == ["FinOps: Dashboard"]


def test_task_plan_defaults_have_no_email_or_github():
    off = {t["id"]: t["agentPrompt"] for t in setup.task_plan(AGENT)["tasks"]}
    assert "do not send email" in off["cost-anomaly"]
    assert "GitHub correlation is disabled" in off["cost-anomaly"]
    on = {t["id"]: t["agentPrompt"] for t in setup.task_plan(
        AGENT, email="finops@contoso.com", repo="contoso/app")["tasks"]}
    assert "email `finops@contoso.com`" in on["cost-anomaly"]
    assert "must not send duplicate anomaly emails" in on["dashboard-report"]
    assert "`contoso/app`" in on["cost-anomaly"]


def test_task_plan_is_idempotent_against_existing_tasks():
    first = setup.task_plan(AGENT)["tasks"]
    existing = {"value": [
        {"id": "t1", "name": first[0]["taskName"], "cronExpression": first[0]["cronExpression"],
         "agentPrompt": first[0]["agentPrompt"]},
        {"taskId": "t2", "taskName": first[1]["taskName"], "cronExpression": "0 0 * * *",
         "agentPrompt": first[1]["agentPrompt"]},
        {"id": "old", "name": "FinOps: Rightsizing Review (Weekly)"},
    ]}
    plan = setup.task_plan(AGENT, existing=existing)
    actions = {t["id"]: (t["action"], t["existingTaskId"]) for t in plan["tasks"]}
    assert actions[first[0]["id"]] == ("unchanged", "t1")
    assert actions[first[1]["id"]] == ("update", "t2")
    assert [r["existingTaskId"] for r in plan["retire"]] == ["old"]


def test_task_plan_rejects_duplicates_and_bad_input():
    name = setup.task_plan(AGENT)["tasks"][0]["taskName"]
    with pytest.raises(setup.SetupError):
        setup.task_plan(AGENT, existing=[{"id": "a", "name": name}, {"id": "b", "name": name}])
    with pytest.raises(setup.SetupError):
        setup.task_plan(AGENT, only=["nope"])
    with pytest.raises(setup.SetupError):
        setup.task_plan(AGENT, cron_overrides={"cost-anomaly": "0 14 * * * ; rm"})
    with pytest.raises(setup.SetupError):
        setup.task_plan(AGENT, email="a@b.com\nX")


def test_only_and_cron_override():
    plan = setup.task_plan(AGENT, only=["cost-anomaly"], cron_overrides={"cost-anomaly": "0 6 * * *"})
    assert [t["id"] for t in plan["tasks"]] == ["cost-anomaly"]
    assert plan["tasks"][0]["cronExpression"] == "0 6 * * *"


# ------------------------------------------------------------------ cli
def test_cli_end_to_end(tmp_path):
    agent_json = tmp_path / "agent.json"
    agent_json.write_text(json.dumps(agent_doc()))
    run = lambda *args: subprocess.run([sys.executable, str(SETUP_DIR / "setup.py"), *args],
                                       capture_output=True, text=True)
    out = run("discover", "--agent-json", str(agent_json), "--identity-info", "MSIClient-" + CLIENT)
    assert out.returncode == 0, out.stderr
    disc = tmp_path / "disc.json"
    disc.write_text(out.stdout)
    out = run("grant-plan", "--discovery", str(disc))
    assert out.returncode == 0 and len(json.loads(out.stdout)["grants"]) == 4
    bad = run("task-plan", "--agent-resource-id", "nope")
    assert bad.returncode == 2 and "finops-setup:" in bad.stderr


# ------------------------------------------------------------------ packaging / skill contract
def test_plugin_metadata_is_standard_and_read_only():
    plugin = json.loads((PLUGIN / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".github" / "plugin" / "marketplace.json").read_text())
    assert plugin["skills"] == ["skills/"]
    assert set(plugin) <= {"name", "version", "description", "license", "skills"}
    assert marketplace["plugins"][0]["source"] == "./plugins/azure-finops"
    assert marketplace["plugins"][0]["name"] == plugin["name"]
    for text in (plugin["description"], marketplace["plugins"][0]["description"]):
        assert "read-only" in text.lower()
        assert "RunAzCliWriteCommands" not in text


def test_every_skill_has_frontmatter_matching_folder():
    skills = sorted(p for p in (PLUGIN / "skills").iterdir() if p.is_dir())
    assert len(skills) == 12
    for folder in skills:
        head = (folder / "SKILL.md").read_text().split("---")[1]
        assert f"name: {folder.name}" in head
        assert "description:" in head


def test_setup_skill_contract():
    text = (SETUP_DIR / "SKILL.md").read_text()
    assert "MUST ask the end user" in text
    assert "**Managed resources**, **Everything**, and **Selected subscriptions**" in text
    assert "on behalf of the user (OBO)" in text
    assert "Never use the terminal for `az`" in text
    update = (PLUGIN / "skills" / "finops-update" / "SKILL.md").read_text()
    assert '"update the FinOps setup"' in update
    assert "--mark-applied" in update and "SaveReport" in update
    assert "Never broaden" in update and "task selection and schedules" in update
    assert "FINOPS_SETUP_STATE_JSON" in update
    assert "Check roles by listing assignments" in text
    assert "rbac-script" in text  # fallback documented
    assert "Build the single dashboard now" in text
    plan_reports = [t["id"] for t in setup.task_plan(AGENT)["tasks"] if t["liveReport"]]
    for task_id in plan_reports:  # every report task is built during setup
        assert f"`{task_id}`" in text
    assert "RunAzCliWriteCommands" not in text  # refer to the tool generically, not by internal name


ROOT_MG = f"/providers/Microsoft.Management/managementGroups/{SUB2}"


def test_everything_grants_exact_identity_at_root_and_supports_no_managed_resources():
    disc = setup.discover(agent_doc(managed=[]), "MSI", "everything", [ROOT_MG])
    assert disc["dashboardScopes"] == [ROOT_MG]
    assert disc["managedScopes"] == []
    assert disc["costReaderScopes"] == [ROOT_MG]
    grants = setup.grant_plan(disc)["grants"]
    assert grants[1]["role"] == "Cost Management Reader"
    assert f'--scope "{ROOT_MG}"' in grants[1]["command"]
    assert SAMI_PRINCIPAL in grants[1]["command"]
    fallback = setup.grant_script(disc)
    assert all(grant["command"] in fallback for grant in grants)
    assert "az ad sp show" not in fallback
    with_managed = setup.discover(agent_doc(), "MSI", "everything", [ROOT_MG])
    assert with_managed["costReaderScopes"] == [ROOT_MG]


@pytest.mark.parametrize("mode,scopes", [
    ("invalid", []), ("managed", [ROOT_MG]), ("everything", []),
    ("everything", [f"/subscriptions/{SUB}"]),
    ("everything", ["/providers/Microsoft.Management/managementGroups/not-tenant-root"]),
])
def test_dashboard_policy_rejects_ambiguous_or_unsafe_scope(mode, scopes):
    with pytest.raises(setup.SetupError):
        setup.dashboard_policy(mode, scopes)


def test_dashboard_scope_persists_but_never_changes_anomaly_boundary():
    first = setup.task_plan(AGENT, scope_mode="everything", dashboard_scopes=[ROOT_MG])
    anomaly, dashboard = first["tasks"]
    assert "STRICT MANAGED-SCOPE PREAMBLE" in anomaly["agentPrompt"]
    assert ROOT_MG not in anomaly["agentPrompt"]
    assert ROOT_MG in dashboard["agentPrompt"]
    existing = [
        dict(id="anomaly", name=anomaly["taskName"], cronExpression=anomaly["cronExpression"],
             agentPrompt=anomaly["agentPrompt"]),
        dict(id="dashboard", name=dashboard["taskName"],
             cronExpression=dashboard["cronExpression"], agentPrompt=dashboard["agentPrompt"]),
    ]
    repeat = setup.task_plan(AGENT, existing=existing)
    assert repeat["dashboardPolicy"] == first["dashboardPolicy"]
    assert repeat["tasks"][1]["action"] == "unchanged"
    edited = setup.task_plan(AGENT, existing=existing, email="a@contoso.com")
    assert edited["dashboardPolicy"] == first["dashboardPolicy"]
    managed = setup.task_plan(AGENT, existing=existing, scope_mode="managed")
    assert managed["dashboardPolicy"] == {"mode": "managed", "scopes": []}
    existing[1]["agentPrompt"] = "policy missing"
    with pytest.raises(setup.SetupError, match="ask for scope"):
        setup.task_plan(AGENT, existing=existing)


def test_everything_rejects_wrong_tenant_before_generating_grants():
    other_root = f"/providers/Microsoft.Management/managementGroups/{SUB}"
    with pytest.raises(setup.SetupError, match="root must match"):
        setup.discover(agent_doc(), "MSI", "everything", [other_root])
    with pytest.raises(setup.SetupError, match="does not match"):
        setup.discover(agent_doc(), "MSI", "everything", [ROOT_MG], tenant_id=SUB)
    doc = agent_doc()
    del doc["identity"]["tenantId"]
    with pytest.raises(setup.SetupError, match="tenant ID"):
        setup.discover(doc, "MSIClient-" + CLIENT, "everything", [ROOT_MG])
    discovery = setup.discover(doc, "MSIClient-" + CLIENT, "everything", [ROOT_MG], tenant_id=SUB2)
    assert discovery["dashboardScopes"] == [ROOT_MG]


def test_migration_retires_all_and_only_exact_legacy_tasks():
    old_names = [task["defaultName"] for task in setup._load_manifest()[1]]
    existing = [{"id": str(i), "name": name} for i, name in enumerate(old_names)]
    existing.append({"id": "custom", "name": old_names[0] + " custom"})
    plan = setup.task_plan(AGENT, existing=existing)
    assert {task["taskName"] for task in plan["retire"]} == set(old_names)
    assert "custom" not in {task["existingTaskId"] for task in plan["retire"]}


def subscription_inventory():
    return [
        {"id": SUB, "name": "Agent subscription", "tenantId": SUB2, "state": "Enabled"},
        {"id": SUB2, "name": "Another subscription", "tenantId": SUB2, "state": "Enabled"},
    ]


def test_selected_subscriptions_discovers_choices_without_claiming_access():
    selected = [f"/SUBSCRIPTIONS/{SUB.upper()}", f"/subscriptions/{SUB2}", f"/subscriptions/{SUB}"]
    disc = setup.discover(agent_doc(managed=[]), "MSI", "subscriptions", selected,
                          subscription_inventory=subscription_inventory())
    assert disc["dashboardPolicy"] == {
        "mode": "subscriptions", "scopes": [f"/subscriptions/{SUB}", f"/subscriptions/{SUB2}"]}
    assert disc["dashboardScopes"] == disc["costReaderScopes"] == disc["dashboardPolicy"]["scopes"]
    assert disc["defaultSubscriptionScope"] == f"/subscriptions/{SUB}"
    assert len(disc["selectableSubscriptions"]) == 2
    assert disc["accessVerification"]["callerInventoryIsAccessProof"] is False
    assert disc["accessVerification"]["agent"] == "pending-role-checks"
    grants = setup.grant_plan(disc)["grants"]
    assert [grant["scope"] for grant in grants] == [AGENT, *disc["dashboardScopes"]]
    assert all(SAMI_PRINCIPAL in grant["command"] for grant in grants)
    assert all(grant["command"] in setup.grant_script(disc) for grant in grants)
    assert ROOT_MG not in json.dumps(grants)


def test_selected_subscription_grants_preserve_separate_anomaly_requirements():
    disc = setup.discover(agent_doc(), "MSI", "subscriptions", [f"/subscriptions/{SUB}"],
                          subscription_inventory=subscription_inventory())
    assert disc["dashboardScopes"] == [f"/subscriptions/{SUB}"]
    assert disc["costReaderScopes"] == [
        f"/subscriptions/{SUB}", f"/subscriptions/{SUB2}",
        "/providers/Microsoft.Management/managementGroups/mg1"]


def test_selected_subscriptions_does_not_preselect_an_unavailable_host_subscription():
    inventory = subscription_inventory()
    inventory[0]["tenantId"] = SUB  # caller can see it, but it is not an eligible choice
    disc = setup.discover(agent_doc(managed=[]), "MSI", "subscriptions",
                          [f"/subscriptions/{SUB2}"], subscription_inventory=inventory)
    assert disc["defaultSubscriptionScope"] == ""
    assert [choice["scope"] for choice in disc["selectableSubscriptions"]] == [f"/subscriptions/{SUB2}"]


@pytest.mark.parametrize("selection", [
    [], ["not-an-id"], [f"/subscriptions/{SUB}/resourceGroups/rg"],
    [ROOT_MG], ["/subscriptions/not-a-guid"],
])
def test_selected_subscriptions_rejects_empty_or_non_subscription_selection(selection):
    with pytest.raises(setup.SetupError):
        setup.dashboard_policy("subscriptions", selection)


@pytest.mark.parametrize("inventory", [
    None, [], {}, [{"id": SUB}],  # absent/incomplete metadata
    [{"id": SUB, "tenantId": SUB, "state": "Enabled"}],  # foreign tenant
    [{"id": SUB, "tenantId": SUB2, "state": "Disabled"}],
])
def test_selected_subscriptions_rejects_unverified_inventory(inventory):
    with pytest.raises(setup.SetupError):
        setup.discover(agent_doc(), "MSI", "subscriptions", [f"/subscriptions/{SUB}"],
                       subscription_inventory=inventory)


def test_selected_subscriptions_rejects_conflicting_agent_tenant_before_grants():
    with pytest.raises(setup.SetupError, match="does not match"):
        setup.discover(agent_doc(), "MSI", "subscriptions", [f"/subscriptions/{SUB}"],
                       tenant_id=SUB, subscription_inventory=subscription_inventory())


def test_selected_subscriptions_preserves_policy_on_task_repair_and_isolates_anomaly():
    scopes = [f"/subscriptions/{SUB}", f"/subscriptions/{SUB2}"]
    first = setup.task_plan(AGENT, scope_mode="subscriptions", dashboard_scopes=scopes)
    anomaly, dashboard = first["tasks"]
    assert "DASHBOARD_POLICY_JSON:" not in anomaly["agentPrompt"]
    assert "STRICT MANAGED-SCOPE PREAMBLE" in anomaly["agentPrompt"]
    assert "microsoft-product-icons.svg" in dashboard["agentPrompt"]
    existing = [
        {"id": "anomaly", "name": anomaly["taskName"], "cronExpression": anomaly["cronExpression"],
         "agentPrompt": anomaly["agentPrompt"]},
        {"id": "dashboard", "name": dashboard["taskName"],
         "cronExpression": dashboard["cronExpression"], "agentPrompt": dashboard["agentPrompt"]},
    ]
    repaired = setup.task_plan(AGENT, existing=existing, email="a@contoso.com")
    assert repaired["dashboardPolicy"] == first["dashboardPolicy"]
    assert repaired["tasks"][1]["action"] == "update"
    with pytest.raises(setup.SetupError, match="explicit scope mode"):
        setup.task_plan(AGENT, existing=existing, dashboard_scopes=[scopes[0]])


def test_task_plan_preserves_customer_settings_and_marks_revision_after_report_save(monkeypatch):
    first = setup.task_plan(
        AGENT, email="finops@contoso.com", repo="contoso/infra",
        scope_mode="everything", dashboard_scopes=[ROOT_MG],
        cron_overrides={"cost-anomaly": "15 9 * * 1", "dashboard-report": "45 9 * * 1"},
    )
    existing = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in first["tasks"]
    ]
    assert first["updateRequired"] is False
    assert first["pluginVersion"] == "1.1.0"
    before_applied = setup.task_plan(AGENT, existing=existing)
    assert before_applied["alertEmail"] == "finops@contoso.com"
    assert before_applied["githubRepo"] == "contoso/infra"
    assert before_applied["dashboardPolicy"] == {"mode": "everything", "scopes": [ROOT_MG]}
    assert [task["cronExpression"] for task in before_applied["tasks"]] == ["15 9 * * 1", "45 9 * * 1"]
    assert before_applied["updateRequired"] is True
    assert "email `finops@contoso.com`" in before_applied["tasks"][0]["agentPrompt"]
    assert "`contoso/infra`" in before_applied["tasks"][0]["agentPrompt"]

    updated_tasks = [
        {"id": task["existingTaskId"] or task["id"], "name": task["taskName"],
         "cronExpression": task["cronExpression"], "agentPrompt": task["agentPrompt"]}
        for task in before_applied["tasks"]
    ]
    committed = setup.task_plan(AGENT, existing=updated_tasks, mark_applied=True)
    assert committed["setupState"]["appliedRevision"] == committed["pluginRevision"]
    assert committed["updateRequired"] is False
    dashboard = next(task for task in committed["tasks"] if task["id"] == "dashboard-report")
    updated_tasks = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in committed["tasks"]
    ]
    settled = setup.task_plan(AGENT, existing=updated_tasks)
    assert {task["action"] for task in settled["tasks"]} == {"unchanged"}

    updated_tasks = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in settled["tasks"]
    ]
    monkeypatch.setattr(setup, "_plugin_revision", lambda: "a" * 64)
    stale = setup.task_plan(AGENT, existing=updated_tasks)
    assert stale["updateRequired"] is True
    assert stale["pluginRevision"] == "a" * 64
    assert next(task for task in stale["tasks"] if task["id"] == "dashboard-report")["action"] == "update"
    applied_tasks = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in stale["tasks"]
    ]
    marked = setup.task_plan(AGENT, existing=applied_tasks, mark_applied=True)
    assert marked["setupState"]["appliedRevision"] == "a" * 64
    assert marked["updateRequired"] is False


def test_task_plan_preserves_dashboard_only_installation():
    first = setup.task_plan(AGENT, only=["dashboard-report"])
    existing = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in first["tasks"]
    ]
    repeated = setup.task_plan(AGENT, existing=existing)
    assert [task["id"] for task in repeated["tasks"]] == ["dashboard-report"]
    assert repeated["setupState"]["settings"]["configuredTasks"] == ["dashboard-report"]


def test_task_plan_rejects_malformed_saved_state_and_allows_explicit_opt_out():
    first = setup.task_plan(AGENT, email="finops@contoso.com", repo="contoso/infra")
    existing = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in first["tasks"]
    ]
    dashboard = next(task for task in existing if task["name"] == "FinOps: Dashboard (Live Report, Daily)")
    dashboard["agentPrompt"] = dashboard["agentPrompt"].replace(
        setup._STATE_PREFIX, setup._STATE_PREFIX + "{bad-json}")
    with pytest.raises(setup.SetupError, match="state is invalid"):
        setup.task_plan(AGENT, existing=existing)

    existing = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": task["agentPrompt"]}
        for task in first["tasks"]
    ]
    disabled = setup.task_plan(AGENT, email="", repo="", existing=existing)
    assert disabled["alertEmail"] == "" and disabled["githubRepo"] == ""
    assert "do not send email" in disabled["tasks"][0]["agentPrompt"]
    assert "GitHub correlation is disabled" in disabled["tasks"][0]["agentPrompt"]


def test_task_plan_migrates_known_legacy_settings_and_rejects_ambiguous_ones():
    first = setup.task_plan(
        AGENT, email="finops@contoso.com", repo="contoso/infra",
        scope_mode="everything", dashboard_scopes=[ROOT_MG],
    )
    legacy = [
        {"id": task["id"], "name": task["taskName"], "cronExpression": task["cronExpression"],
         "agentPrompt": "\n".join(line for line in task["agentPrompt"].splitlines()
                                   if not line.startswith(setup._STATE_PREFIX))}
        for task in first["tasks"]
    ]
    migrated = setup.task_plan(AGENT, existing=legacy)
    assert migrated["updateRequired"] is True
    assert migrated["alertEmail"] == "finops@contoso.com"
    assert migrated["githubRepo"] == "contoso/infra"
    assert migrated["dashboardPolicy"] == {"mode": "everything", "scopes": [ROOT_MG]}
    assert migrated["setupState"]["settings"]["configuredTasks"] == ["cost-anomaly", "dashboard-report"]

    dashboard = next(task for task in legacy if task["name"] == "FinOps: Dashboard (Live Report, Daily)")
    dashboard["agentPrompt"] = "DASHBOARD_POLICY_JSON: {\"mode\":\"managed\",\"scopes\":[]}\ncustom prompt"
    anomaly = next(task for task in legacy if task["name"] == "FinOps: Cost Anomaly Detection (Daily)")
    anomaly["agentPrompt"] = "custom prompt"
    with pytest.raises(setup.SetupError, match="cannot be recovered"):
        setup.task_plan(AGENT, existing=legacy)


def test_selected_subscriptions_cli_round_trip(tmp_path, capsys):
    agent_path, inventory_path = tmp_path / "agent.json", tmp_path / "subscriptions.json"
    agent_path.write_text(json.dumps(agent_doc(managed=[])))
    inventory_path.write_text(json.dumps(subscription_inventory()))
    args = ["--scope-mode", "subscriptions", "--dashboard-scope", f"/subscriptions/{SUB}",
            "--dashboard-scope", f"/subscriptions/{SUB2}"]
    assert setup.main(["discover", "--agent-json", str(agent_path), "--identity-info", "MSI",
                       "--subscriptions-json", str(inventory_path), "--tenant-id", SUB2, *args]) == 0
    discovery = json.loads(capsys.readouterr().out)
    assert setup.main(["task-plan", "--agent-resource-id", AGENT, "--only", "dashboard-report", *args]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert discovery["dashboardPolicy"] == plan["dashboardPolicy"]
    assert setup.main(["discover", "--agent-json", str(agent_path), "--identity-info", "MSI", *args]) == 2
