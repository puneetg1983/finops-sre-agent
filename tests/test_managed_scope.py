"""Focused offline tests for the finops-managed-scope foundation."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-managed-scope"
    / "scope.py"
)
SPEC = importlib.util.spec_from_file_location("managed_scope", PATH)
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)

SUB_A = "/subscriptions/Sub-A"
SUB_B = "/subscriptions/sub-b"
SUB_C = "/subscriptions/sub-c"
RG_A = f"{SUB_A}/resourceGroups/Prod"
RG_OTHER = f"{SUB_A}/resourceGroups/Other"
MG_ROOT = "/providers/Microsoft.Management/managementGroups/Root"
MG_CHILD = "/providers/Microsoft.Management/managementGroups/Child"
VM_A = f"{RG_A}/providers/Microsoft.Compute/virtualMachines/vm-a"
VM_OTHER = f"{RG_OTHER}/providers/Microsoft.Compute/virtualMachines/vm-b"
VM_B = f"{SUB_B}/resourceGroups/team/providers/Microsoft.Compute/virtualMachines/vm-b"


def test_canonicalizes_all_supported_scope_kinds():
    assert scope.canonicalize_scope(" /SUBSCRIPTIONS/Sub-A/ ") == SUB_A
    assert scope.canonicalize_scope(
        "/Subscriptions/Sub-A/RESOURCEGROUPS/Prod/"
    ) == RG_A
    assert scope.canonicalize_scope(
        "/PROVIDERS/MICROSOFT.MANAGEMENT/MANAGEMENTGROUPS/Root/"
    ) == MG_ROOT


@pytest.mark.parametrize(
    "value",
    [
        "",
        "subscriptions/sub-a",
        "/subscriptions/",
        "/subscriptions/sub-a/resourceGroups",
        "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute",
        "/providers/Microsoft.Management/managementGroups/",
        "/tenants/t",
        "/subscriptions/sub-a?x=1",
    ],
)
def test_malformed_or_unsupported_scope_ids_fail(value):
    with pytest.raises(ValueError):
        scope.canonicalize_scope(value)


def test_duplicate_casing_matches_current_live_configuration_behavior():
    result = scope.resolve_managed_scopes(
        [
            SUB_A,
            "/SUBSCRIPTIONS/sub-a",
            {"resourceId": RG_A},
            {"id": "/subscriptions/SUB-A/resourcegroups/prod"},
        ]
    )
    assert result["configured_scopes"] == [SUB_A, RG_A]
    assert result["effective_scopes"] == [SUB_A]
    assert result["duplicates_removed"] == 2
    assert result["nested_removed"] == 1
    assert [item["code"] for item in result["diagnostics"]] == [
        "duplicate_scope",
        "duplicate_scope",
        "nested_scope_removed",
    ]


def test_nested_rg_removed_under_subscription_case_insensitively():
    result = scope.resolve_managed_scopes([RG_A, "/subscriptions/sub-a"])
    assert result["effective_scopes"] == ["/subscriptions/sub-a"]
    assert result["resource_group_scopes"] == []


def test_single_scope_string_is_not_treated_as_character_iterable():
    result = scope.resolve_managed_scopes(SUB_A)
    assert result["effective_scopes"] == [SUB_A]


def test_recursive_management_group_expansion_and_overlap():
    expansions = {
        MG_ROOT: {
            "subscriptions": ["Sub-A"],
            "managementGroups": ["Child"],
        },
        MG_CHILD: {"subscriptions": [SUB_B]},
    }
    result = scope.resolve_managed_scopes(
        [MG_ROOT, "/subscriptions/SUB-B", f"{SUB_B}/resourceGroups/team"],
        expansions,
    )
    assert {item.casefold() for item in result["subscription_scopes"]} == {
        SUB_A.casefold(),
        SUB_B.casefold(),
    }
    assert result["nested_removed"] == 1
    assert result["overlaps_removed"] == 1
    assert result["unexpanded_management_groups"] == []
    assert set(result["management_group_descendants"][MG_ROOT]) == {
        MG_ROOT,
        MG_CHILD,
    }


def test_management_group_overlap_fixture_is_deterministic():
    expansions = {
        MG_ROOT: [SUB_A, SUB_B],
        MG_CHILD: ["/subscriptions/SUB-B", SUB_C],
    }
    first = scope.resolve_managed_scopes([MG_ROOT, MG_CHILD], expansions)
    second = scope.resolve_managed_scopes([MG_ROOT, MG_CHILD], expansions)
    assert first == second
    assert first["subscription_scopes"] == [SUB_A, SUB_B, SUB_C]
    assert first["overlaps_removed"] == 1


def test_management_group_accepts_azure_cli_subscription_rows_and_empty_group():
    cli_row = {
        "id": f"{MG_ROOT}/subscriptions/Sub-A",
        "name": "Sub-A",
        "type": "Microsoft.Management/managementGroups/subscriptions",
    }
    populated = scope.resolve_managed_scopes([MG_ROOT], {MG_ROOT: [cli_row]})
    assert populated["subscription_scopes"] == [SUB_A]

    empty = scope.resolve_managed_scopes([MG_CHILD], {MG_CHILD: {}})
    assert empty["subscription_scopes"] == []
    assert empty["unexpanded_management_groups"] == []


def test_management_group_children_distinguish_subscriptions_from_child_groups():
    expansions = {
        MG_ROOT: {
            "children": [
                {"type": "/subscriptions", "name": "Sub-A"},
                {
                    "type": "Microsoft.Management/managementGroups",
                    "name": "Child",
                },
            ]
        },
        MG_CHILD: {"children": [{"type": "/subscriptions", "name": "sub-b"}]},
    }
    result = scope.resolve_managed_scopes([MG_ROOT], expansions)

    assert result["subscription_scopes"] == [SUB_A, SUB_B]
    assert result["unexpanded_management_groups"] == []
    assert result["management_group_descendants"][MG_ROOT] == [MG_CHILD, MG_ROOT]


def test_management_group_children_infer_subscription_from_arm_id_without_type():
    result = scope.resolve_managed_scopes(
        [MG_ROOT],
        {MG_ROOT: {"children": [{"id": SUB_A}, {"id": MG_CHILD}]}, MG_CHILD: {}},
    )

    assert result["subscription_scopes"] == [SUB_A]
    assert result["unexpanded_management_groups"] == []


def test_management_group_cycle_rejected():
    with pytest.raises(ValueError, match="cycle"):
        scope.resolve_managed_scopes(
            [MG_ROOT],
            {
                MG_ROOT: {"managementGroups": [MG_CHILD]},
                MG_CHILD: {"managementGroups": [MG_ROOT]},
            },
        )


def test_containment_for_scopes_and_resource_ids():
    assert scope.scope_contains([SUB_A], RG_A)
    assert scope.scope_contains([SUB_A], VM_OTHER)
    assert scope.scope_contains([RG_A], VM_A)
    assert not scope.scope_contains([RG_A], VM_OTHER)
    assert not scope.scope_contains([RG_A], SUB_A)


def test_canonicalizes_subscription_level_and_nested_extension_resource_ids():
    subscription_resource = (
        f"{SUB_A}/providers/Microsoft.Billing/billingAccounts/account"
    )
    extension_resource = (
        f"{VM_A}/providers/Microsoft.Insights/diagnosticSettings/default"
    )

    assert scope.canonicalize_resource_id(subscription_resource) == subscription_resource
    assert scope.canonicalize_resource_id(extension_resource) == extension_resource
    assert scope.scope_contains([SUB_A], subscription_resource)
    assert scope.scope_contains([RG_A], extension_resource)


def test_resource_name_providers_is_not_an_extension_boundary():
    root_name = (
        f"{RG_A}/providers/Microsoft.Compute/virtualMachines/providers"
    )
    nested_type_name = (
        f"{RG_A}/providers/Microsoft.Network/virtualNetworks/vnet/"
        "subnets/providers"
    )
    extension_name = (
        f"{VM_A}/providers/Microsoft.Insights/diagnosticSettings/providers"
    )

    for resource in (root_name, nested_type_name, extension_name):
        assert scope.canonicalize_resource_id(resource) == resource
        assert scope.scope_contains([RG_A], resource)


def test_subscription_resource_named_resource_groups_is_not_misattributed():
    resource = (
        f"{SUB_A}/providers/Microsoft.Authorization/"
        "policyAssignments/resourceGroups"
    )

    assert scope.canonicalize_resource_id(resource) == resource
    assert scope._resource_group_from_id(resource) is None
    assert scope.scope_contains([SUB_A], resource)
    assert not scope.scope_contains([RG_A], resource)


def test_subscription_extension_resource_group_segments_are_not_root_rg():
    extension_name = (
        f"{SUB_A}/providers/Microsoft.Authorization/policyAssignments/base/"
        "providers/Microsoft.Example/widgets/resourceGroups"
    )
    extension_type = (
        f"{SUB_A}/providers/Microsoft.Authorization/policyAssignments/base/"
        "providers/Microsoft.Example/resourceGroups/Prod"
    )

    for resource in (extension_name, extension_type):
        assert scope.canonicalize_resource_id(resource) == resource
        assert scope._resource_group_from_id(resource) is None
        assert scope.scope_contains([SUB_A], resource)
        assert not scope.scope_contains([RG_A], resource)


def test_rg_extension_resource_uses_only_root_resource_group():
    resource = (
        f"{VM_A}/providers/Microsoft.Example/resourceGroups/not-the-root-rg"
    )

    assert scope._resource_group_from_id(resource) == RG_A
    assert scope.scope_contains([RG_A], resource)
    assert not scope.scope_contains(
        [f"{SUB_A}/resourceGroups/not-the-root-rg"], resource
    )


def test_rejects_incomplete_or_unpaired_provider_resource_paths():
    with pytest.raises(ValueError, match="type/name pairs"):
        scope.canonicalize_resource_id(
            f"{RG_A}/providers/Microsoft.Compute/virtualMachines"
        )
    with pytest.raises(ValueError, match="type/name pairs"):
        scope.canonicalize_resource_id(
            f"{VM_A}/providers/Microsoft.Insights/diagnosticSettings"
        )
    with pytest.raises(ValueError, match="namespace"):
        scope.canonicalize_resource_id(
            f"{VM_A}/providers/Microsoft.Insights"
        )
    with pytest.raises(ValueError, match="namespace"):
        scope.canonicalize_resource_id(f"{VM_A}/providers")
    with pytest.raises(ValueError, match="complete type/name pairs"):
        scope.canonicalize_resource_id(
            f"{VM_A}/providers/Microsoft.Insights/diagnosticSettings/default/"
            "providers/Microsoft.Authorization/roleAssignments"
        )
    with pytest.raises(ValueError, match="namespace"):
        scope.canonicalize_resource_id(
            f"{VM_A}/providers/Microsoft.Insights/diagnosticSettings/default/"
            "providers/Microsoft.Authorization"
        )
    with pytest.raises(ValueError, match="unsafe path characters"):
        scope._management_group_scope("parent/child")


def test_management_group_containment_requires_expansion_for_resources():
    assert not scope.scope_contains([MG_ROOT], VM_B)
    assert scope.scope_contains([MG_ROOT], VM_B, {MG_ROOT: [SUB_B]})
    assert scope.scope_contains(
        [MG_ROOT],
        MG_CHILD,
        {
            MG_ROOT: {"managementGroups": [MG_CHILD]},
            MG_CHILD: [SUB_B],
        },
    )


def _row(cost, **values):
    return {"costInUSD": cost, **values}


def test_usage_filter_prefers_instance_name_and_falls_back_to_resource_id():
    rows = [
        _row("1.10", instanceName=VM_A, resourceId=VM_OTHER),
        _row("2.20", instanceName="plain-name", resourceId=VM_A),
        _row("3.30", resourceId=VM_OTHER),
    ]
    result = scope.filter_usage_details(rows, [RG_A])
    assert result["included_count"] == 2
    assert result["excluded_count"] == 1
    assert result["included_cost"] == Decimal("3.30")
    assert result["excluded_cost"] == Decimal("3.30")


def test_usage_filter_uses_nested_properties_and_case_insensitive_fields():
    rows = [
        {
            "properties": {
                "InstanceName": VM_A.upper(),
                "CostInUSD": "0.01",
            }
        }
    ]
    result = scope.filter_usage_details(rows, [RG_A])
    assert result["included_count"] == 1
    assert result["included_cost"] == Decimal("0.01")


def test_usage_filter_resolves_scope_once_for_large_mixed_rg_data(monkeypatch):
    managed_resource_groups = [
        f"{SUB_A}/resourceGroups/managed-{index}" for index in range(30)
    ]
    rows = []
    expected_excluded_targets = []
    for index in range(1200):
        if index % 2 == 0:
            resource_group = managed_resource_groups[index % 30]
        else:
            resource_group = f"{SUB_A}/resourceGroups/unmanaged-{index % 30}"
            expected_excluded_targets.append(
                f"{resource_group}/providers/Microsoft.Compute/disks/disk-{index}"
            )
        rows.append(
            _row(
                "0.01",
                chargeId=f"charge-{index}",
                resourceId=(
                    f"{resource_group}/providers/Microsoft.Compute/disks/disk-{index}"
                ),
            )
        )

    original_resolve = scope.resolve_managed_scopes
    original_evaluate = scope.evaluate_containment
    resolution_calls = 0
    evaluation_calls = 0

    def counted_resolve(*args, **kwargs):
        nonlocal resolution_calls
        resolution_calls += 1
        return original_resolve(*args, **kwargs)

    def counted_evaluate(*args, **kwargs):
        nonlocal evaluation_calls
        evaluation_calls += 1
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(scope, "resolve_managed_scopes", counted_resolve)
    monkeypatch.setattr(scope, "evaluate_containment", counted_evaluate)

    result = scope.filter_usage_details(rows, managed_resource_groups)

    assert resolution_calls == 1
    assert evaluation_calls == 0
    assert result["included_count"] == 600
    assert result["excluded_count"] == 600
    assert result["included_cost"] == Decimal("6.00")
    assert result["excluded_cost"] == Decimal("6.00")
    assert [
        item["target"]
        for item in result["diagnostics"]
        if item["code"] == "outside_managed_scope"
    ] == expected_excluded_targets


def test_missing_ids_and_shared_charges_are_not_guessed_into_rg():
    rows = [
        _row("4.00", subscriptionId="Sub-A"),
        _row("5.00", subscriptionId="Sub-A", resourceGroup="Prod"),
        _row("6.00", meterCategory="Support"),
    ]
    rg_result = scope.filter_usage_details(rows, [RG_A])
    assert rg_result["included_count"] == 1
    assert rg_result["excluded_count"] == 1
    assert rg_result["unattributed_count"] == 1
    assert rg_result["unattributed_cost"] == Decimal("6.00")

    subscription_result = scope.filter_usage_details(rows, [SUB_A])
    assert subscription_result["included_count"] == 2
    assert subscription_result["unattributed_count"] == 1


def test_duplicate_rows_deduplicated_by_charge_id_case_insensitively():
    rows = [
        _row("0.10", chargeId="Charge-1", instanceName=VM_A),
        _row("0.10", chargeId="charge-1", instanceName=VM_A.upper()),
    ]
    result = scope.filter_usage_details(rows, [RG_A])
    assert result["input_count"] == 2
    assert result["unique_count"] == 1
    assert result["duplicate_count"] == 1
    assert result["included_cost"] == Decimal("0.10")
    assert [item["code"] for item in result["diagnostics"]] == [
        "duplicate_usage_row"
    ]


def test_duplicate_rows_without_ids_use_stable_semantic_fingerprint():
    row = _row("1.25", instanceName=VM_A, meterCategory="Compute")
    result = scope.filter_usage_details([row, dict(row)], [SUB_A])
    assert result["duplicate_count"] == 1
    assert result["total_cost"] == Decimal("1.25")


def test_exact_decimal_coverage_reconciles_included_excluded_unattributed():
    rows = [
        _row("0.1", instanceName=VM_A),
        _row("0.2", instanceName=VM_OTHER),
        _row("0.3"),
    ]
    result = scope.filter_usage_details(rows, [RG_A])
    assert result["included_cost"] == Decimal("0.1")
    assert result["excluded_cost"] == Decimal("0.2")
    assert result["unattributed_cost"] == Decimal("0.3")
    assert result["total_cost"] == Decimal("0.6")
    assert (
        result["included_cost"]
        + result["excluded_cost"]
        + result["unattributed_cost"]
        == result["total_cost"]
    )
    assert result["attribution_coverage_pct"] == Decimal("66.67")


def test_invalid_cost_is_zero_with_deterministic_diagnostic():
    result = scope.filter_usage_details(
        [_row("NaN", instanceName=VM_A)], [SUB_A]
    )
    assert result["included_count"] == 1
    assert result["included_cost"] == Decimal("0")
    assert result["diagnostics"][0]["code"] == "invalid_cost"


def test_scheduled_policy_fails_closed_outside_scope_even_if_confirmed():
    result = scope.decide_scope_policy(
        [SUB_B],
        [SUB_A],
        mode="scheduled",
        outside_scope_confirmed=True,
    )
    assert result["allowed"] is False
    assert result["decision"] == "deny"
    assert result["state"] == "outside_managed_scope"
    assert result["confirmation_state"] == "not_permitted"


def test_scheduled_policy_fails_closed_for_unexpanded_management_group():
    result = scope.decide_scope_policy(
        [SUB_A], [MG_ROOT], mode="scheduled"
    )
    assert result["allowed"] is False
    assert result["state"] == "management_group_expansion_required"


def test_scheduled_policy_accepts_managed_request_after_expansion():
    result = scope.decide_scope_policy(
        [VM_B],
        [MG_ROOT],
        mode="scheduled",
        management_group_expansions={MG_ROOT: [SUB_B]},
    )
    assert result["allowed"] is True
    assert result["state"] == "within_managed_scope"


def test_interactive_outside_scope_requires_then_records_confirmation():
    pending = scope.decide_scope_policy([SUB_B], [SUB_A], mode="interactive")
    assert pending["allowed"] is False
    assert pending["decision"] == "confirm"
    assert pending["requires_confirmation"] is True
    assert pending["confirmation_state"] == "required"
    assert pending["outside_scopes"] == [SUB_B]
    assert pending["confirmation_key"]

    confirmed = scope.decide_scope_policy(
        [SUB_B],
        [SUB_A],
        mode="interactive",
        outside_scope_confirmed=True,
        confirmation_key=pending["confirmation_key"],
    )
    assert confirmed["allowed"] is True
    assert confirmed["state"] == "outside_scope_confirmed"
    assert confirmed["confirmation_state"] == "confirmed"
    assert confirmed["confirmation_key"] == pending["confirmation_key"]


def test_interactive_confirmation_is_bound_to_displayed_request():
    request_b = scope.decide_scope_policy([SUB_B], [SUB_A], mode="interactive")

    request_c_with_b_key = scope.decide_scope_policy(
        [SUB_C],
        [SUB_A],
        mode="interactive",
        outside_scope_confirmed=True,
        confirmation_key=request_b["confirmation_key"],
    )
    assert request_c_with_b_key["allowed"] is False
    assert request_c_with_b_key["decision"] == "confirm"
    assert request_c_with_b_key["state"] == "outside_scope_confirmation_key_mismatch"
    assert request_c_with_b_key["confirmation_state"] == "key_mismatch"
    assert request_c_with_b_key["confirmation_key"] != request_b["confirmation_key"]
    assert request_c_with_b_key["diagnostics"][-1]["code"] == (
        "outside_scope_confirmation_key_mismatch"
    )


def test_interactive_confirmation_without_key_remains_required():
    result = scope.decide_scope_policy(
        [SUB_B],
        [SUB_A],
        mode="interactive",
        outside_scope_confirmed=True,
    )
    assert result["allowed"] is False
    assert result["decision"] == "confirm"
    assert result["state"] == "outside_scope_confirmation_key_required"
    assert result["confirmation_state"] == "key_required"
    assert result["requires_confirmation"] is True
    assert result["diagnostics"][-1]["code"] == (
        "outside_scope_confirmation_key_missing"
    )


def test_interactive_decline_denies_and_inside_scope_needs_no_confirmation():
    declined = scope.decide_scope_policy(
        [SUB_B],
        [SUB_A],
        mode="interactive",
        outside_scope_confirmed=False,
    )
    assert declined["allowed"] is False
    assert declined["confirmation_state"] == "declined"

    inside = scope.decide_scope_policy([VM_A], [RG_A], mode="interactive")
    assert inside["allowed"] is True
    assert inside["confirmation_state"] == "not_required"


def test_invalid_interactive_request_cannot_be_confirmed():
    result = scope.decide_scope_policy(
        ["/tenants/not-a-supported-scope"],
        [SUB_A],
        mode="interactive",
        outside_scope_confirmed=True,
    )
    assert result["allowed"] is False
    assert result["state"] == "invalid_requested_scope"
    assert result["confirmation_state"] == "not_permitted"
