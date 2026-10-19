"""Managed-scope policy integration guardrails."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINOPS = ROOT / "plugins" / "azure-finops"
SETUP_ASSETS = FINOPS / "skills" / "finops-setup" / "assets"
TASKS = SETUP_ASSETS / "tasks"


def _task_templates():
    """Unrendered scheduled-task prompts (scope preamble + task prompt), keyed by task id."""
    manifest = json.loads((TASKS / "task-manifest.json").read_text())
    scope = (TASKS / "prompts" / "_managed-scope.txt").read_text()
    return {
        task["id"]: scope + "\n\n" + (TASKS / task["prompt"]).read_text()
        for task in manifest["tasks"]
    }


SKILL_NAMES = {
    "finops-cost-anomaly-detection",
    "finops-rightsizing-advisor",
    "finops-cost-allocation",
    "finops-budget-governance",
    "finops-budget-editor",
    "finops-cost-optimization-report",
    "finops-for-ai",
    "finops-cost-vs-reliability",
}


def _command_windows(text, command, width=240):
    normalized = re.sub(r"\\\s*\n\s*", " ", text)
    normalized = re.sub(r"\s+", " ", normalized)
    return [
        normalized[match.start() : match.start() + width]
        for match in re.finditer(re.escape(command), normalized)
    ]


def test_all_existing_finops_skills_start_with_managed_scope_policy():
    for name in SKILL_NAMES:
        text = (FINOPS / "skills" / name / "SKILL.md").read_text()
        lowered = text.lower()

        procedure = lowered.index("## procedure")
        scope_step = lowered.index("step 0", procedure)
        next_step = lowered.find("step 1", scope_step)
        assert procedure < scope_step
        assert next_step == -1 or scope_step < next_step
        assert "finops-managed-scope" in lowered
        assert "scope.py" in lowered
        assert "managedresources" in lowered
        assert "scheduled" in lowered
        assert "fail-closed" in lowered or "fail closed" in lowered
        assert "no override" in lowered or "accepts no override" in lowered
        assert re.search(r"explicit\s+confirmation", lowered)
        assert re.search(r"subsequent\s+(?:user\s+)?turn", lowered)
        assert "broad rbac" in lowered
        assert "report" in lowered
        assert "excluded" in lowered


def test_usage_and_telemetry_skills_enforce_per_scope_retrieval():
    for name in SKILL_NAMES - {"finops-budget-editor"}:
        lowered = (FINOPS / "skills" / name / "SKILL.md").read_text().lower()
        assert "each effective scope" in lowered or "every effective scope" in lowered

    for name in {
        "finops-cost-anomaly-detection",
        "finops-rightsizing-advisor",
        "finops-cost-allocation",
        "finops-for-ai",
        "finops-cost-vs-reliability",
    }:
        lowered = (FINOPS / "skills" / name / "SKILL.md").read_text().lower()
        assert "independently" in lowered
        assert "de-duplicate" in lowered
        assert "unattributed" in lowered


def test_budget_editor_uses_subscription_transport_and_client_side_rg_filtering():
    lowered = (
        FINOPS / "skills" / "finops-budget-editor" / "SKILL.md"
    ).read_text().lower()
    normalized = re.sub(r"\s+", " ", lowered)

    assert "subscription-scoped transport" in normalized
    assert "/subscriptions/<sub_id>/providers/microsoft.consumption/usagedetails" in normalized
    assert "without a resource-group server filter" in normalized
    assert "never trust `properties/resourcegroup eq ...`" in normalized
    assert "filter_usage_details" in normalized
    assert "initial page exactly once" in normalized
    assert "pages from separate chains must not be mixed" in normalized
    assert "included, excluded, unattributed, and" in normalized


def test_managed_scope_forbids_server_side_usage_rg_filter_and_mixed_chains():
    lowered = (
        FINOPS / "skills" / "finops-managed-scope" / "SKILL.md"
    ).read_text().lower()
    normalized = re.sub(r"\s+", " ", lowered)

    assert "do not add a `properties/resourcegroup eq ...` filter" in normalized
    assert "silently ignore that filter" in normalized
    assert "fetch the initial page once" in normalized
    assert "do not mix pages from independently restarted requests" in normalized
    assert "filter_usage_details" in normalized


def test_usage_details_docs_share_canonical_transport_projection_contract():
    skills = FINOPS / "skills"
    managed = (skills / "finops-managed-scope" / "SKILL.md").read_text()
    managed_normalized = re.sub(r"\s+", " ", managed).lower()

    assert "canonical usagedetails transport contract" in managed_normalized
    assert "properties.instancename || properties.resourceid" in managed_normalized
    assert "alias `cost` once" in managed_normalized
    assert "subscriptionid" in managed_normalized
    assert "resourcegroup" in managed_normalized
    assert "explicit partial-cost warning" in managed_normalized

    projection_skills = {
        "finops-cost-anomaly-detection",
        "finops-rightsizing-advisor",
        "finops-cost-allocation",
        "finops-budget-editor",
        "finops-for-ai",
        "finops-cost-vs-reliability",
    }
    for name in projection_skills:
        normalized = re.sub(
            r"\s+",
            " ",
            (skills / name / "SKILL.md").read_text(),
        )
        assert "properties.instanceName || properties.resourceId" in normalized, name
        assert re.search(
            r"subscriptionId:\s*properties\.subscriptionId",
            normalized,
        ), name

    allocation = (skills / "finops-cost-allocation" / "SKILL.md").read_text()
    reliability = (skills / "finops-cost-vs-reliability" / "SKILL.md").read_text()
    assert "tags: tags" in allocation
    assert "costInUSD: properties.costInUSD" not in reliability

    for path in skills.glob("finops-*/SKILL.md"):
        assert "same hardened Consumption UsageDetails pull" not in path.read_text()


def test_usage_details_skills_delegate_page_assembly_to_code():
    skills = FINOPS / "skills"
    managed = (skills / "finops-managed-scope" / "SKILL.md").read_text()
    assert "from usage import prepare_usage_details" in managed
    assert "rejects restarted/mixed chains" in managed
    assert "remaining_next_link" in managed

    consumers = {
        "finops-budget-editor",
        "finops-cost-allocation",
        "finops-cost-anomaly-detection",
        "finops-cost-vs-reliability",
        "finops-for-ai",
        "finops-rightsizing-advisor",
    }
    for name in consumers:
        text = (skills / name / "SKILL.md").read_text()
        assert "prepare_usage_details" in text, name
        assert "model reasoning" in text, name


def test_scheduled_templates_have_strict_dynamic_scope_preamble():
    templates = _task_templates()
    assert len(templates) == 7

    for text in templates.values():
        lowered = text.lower()

        assert "{{AGENT_RESOURCE_ID}}" in text
        assert "finops-managed-scope" in lowered
        assert "scope.py" in lowered
        assert "dynamically get" in lowered
        assert "managedresources" in lowered
        assert "fail closed" in lowered
        assert "no override" in lowered
        assert "broad rbac" in lowered
        assert "without querying analysis data" in lowered
        assert "sending email" in lowered
        assert "saving a report" in lowered
        assert "independent scoped retrieval" in lowered
        assert "excluded, unattributed" in lowered
        assert "__SUB_ID__" not in text


def test_agent_manifest_enables_managed_scope_without_write_tools():
    manifest = json.loads(
        (SETUP_ASSETS / "finops-investigator.json").read_text()
    )
    properties = manifest["properties"]
    instructions = properties["instructions"].lower()

    assert "finops-managed-scope" in properties["allowedSkills"]
    assert "{{agent_resource_id}}" in instructions
    assert "before every finops request" in instructions
    assert "managedresources" in instructions
    assert "strict and fail-closed" in instructions
    assert "subsequent turn" in instructions
    assert "broad rbac" in instructions
    assert properties["tools"] == [
        "RunAzCliReadCommands",
        "ExecutePythonCode",
        "ListReports",
        "GetReport",
        "SaveReport",
    ]


def test_budget_skills_preserve_direct_management_group_budget_queries():
    governance = (
        FINOPS / "skills" / "finops-budget-governance" / "SKILL.md"
    ).read_text().lower()
    editor = (
        FINOPS / "skills" / "finops-budget-editor" / "SKILL.md"
    ).read_text().lower()

    for text in (governance, editor):
        assert "configured management-group" in text
        assert "expanded descendant effective scopes" in text
        assert "case-insensitive" in text
        assert "query each target exactly once" in text
        assert "unless" in text and "itself configured" in text

    assert (
        "/providers/microsoft.management/managementgroups/<mg_id>/"
        "providers/microsoft.consumption/budgets"
    ) in governance
    assert "management-group-level budget" in governance
    assert "exact get" in editor


def test_budget_status_task_queries_configured_management_groups_once():
    text = _task_templates()["budget-status-report"]
    lowered = text.lower()

    assert "{{AGENT_RESOURCE_ID}}" in text
    assert "fail closed" in lowered
    assert "no override" in lowered
    assert "finops-budget-governance" in lowered
    assert "full read-only" in lowered
    assert "every directly configured management-group budget scope" in lowered
    assert "every expanded effective scope" in lowered
    assert "never replace management-group budgets with descendant budgets" in lowered
    assert "api-version=" not in lowered


def test_all_documented_arg_and_advisor_commands_are_subscription_scoped():
    documents = [
        *((path, path.read_text()) for path in (FINOPS / "skills" / name / "SKILL.md" for name in SKILL_NAMES)),
        *_task_templates().items(),
    ]
    graph_count = 0
    advisor_count = 0

    for path, text in documents:
        graph_windows = _command_windows(text, "az graph query")
        advisor_windows = _command_windows(text, "az advisor recommendation list")
        graph_count += len(graph_windows)
        advisor_count += len(advisor_windows)

        for command in graph_windows:
            assert "--subscriptions <EFFECTIVE_SUBSCRIPTION_ID>" in command, path
        for command in advisor_windows:
            assert "--subscription <EFFECTIVE_SUBSCRIPTION_ID>" in command, path

    assert graph_count >= 4
    assert advisor_count >= 3


def test_rg_only_arg_and_advisor_policy_is_concrete():
    arg_documents = [
        FINOPS / "skills" / "finops-rightsizing-advisor" / "SKILL.md",
        FINOPS / "skills" / "finops-cost-allocation" / "SKILL.md",
        FINOPS / "skills" / "finops-for-ai" / "SKILL.md",
    ]
    for path in arg_documents:
        lowered = path.read_text().lower()
        assert "resourcegroup =~ '<effective_resource_group>'" in lowered, path
        assert "client-side" in lowered, path
        assert "normalized" in lowered, path

    advisor_documents = [
        FINOPS / "skills" / "finops-rightsizing-advisor" / "SKILL.md",
        FINOPS / "skills" / "finops-cost-vs-reliability" / "SKILL.md",
    ]
    for path in advisor_documents:
        lowered = path.read_text().lower()
        assert "--resource-group <effective_resource_group>" in lowered, path
        assert "client-side" in lowered, path

    templates = _task_templates()
    delegated_tasks = [
        "rightsizing-savings-report",
        "ai-spend-report",
        "cost-vs-reliability-report",
        "cost-optimization-report",
    ]
    for path in delegated_tasks:
        lowered = templates[path].lower()
        assert "skill.md" in lowered, path
        assert "az graph query" not in lowered, path
        assert "az advisor recommendation list" not in lowered, path
