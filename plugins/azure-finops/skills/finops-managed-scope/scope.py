"""Managed-scope enforcement for FinOps skills.

Pure, dependency-free, deterministic logic.  This module never calls Azure.  Callers
must retrieve the agent's managedResources and any management-group descendants, then
pass those values to the functions below.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_scope_resolution():
    path = Path(__file__).with_name("_scope_resolution.py")
    spec = spec_from_file_location("finops_managed_scope_resolution", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load managed-scope resolution helpers from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scope_resolution = _load_scope_resolution()
for _name in (
    "_SUBSCRIPTION_RE",
    "_RESOURCE_GROUP_RE",
    "_MANAGEMENT_GROUP_RE",
    "_INVALID_PATH_CHARS",
    "_segment",
    "_scope_value",
    "canonicalize_scope",
    "canonical_scope",
    "canonicalize_scope_id",
    "validate_scope",
    "validate_scope_id",
    "scope_kind",
    "_subscription_scope",
    "_management_group_scope",
    "_diagnostic",
    "deduplicate_scopes",
    "_normalise_expansion_records",
    "resolve_managed_scopes",
    "normalize_managed_scopes",
    "expand_management_groups",
    "normalize_scopes",
):
    globals()[_name] = getattr(_scope_resolution, _name)
del _name


def canonicalize_resource_id(resource_id):
    """Validate a subscription-based ARM resource ID and canonicalize path keywords."""

    raw = str(resource_id or "").strip()
    if raw != "/":
        raw = raw.rstrip("/")
    try:
        return canonicalize_scope(raw)
    except ValueError:
        pass

    if not raw.startswith("/") or any(char in raw for char in _INVALID_PATH_CHARS):
        raise ValueError("resourceId must be an absolute ARM resource ID")
    parts = raw.split("/")[1:]
    if len(parts) < 4 or parts[0].casefold() != "subscriptions":
        raise ValueError("resourceId must begin with /subscriptions/<subscriptionId>")
    _segment(parts[1], "subscriptionId")

    index = 2
    if index < len(parts) and parts[index].casefold() == "resourcegroups":
        if index + 1 >= len(parts):
            raise ValueError("resourceId has no resource-group name")
        _segment(parts[index + 1], "resourceGroupName")
        index += 2
    if index >= len(parts) or parts[index].casefold() != "providers":
        raise ValueError("resourceId must contain a providers segment")

    provider_index = index
    provider_markers = []
    while provider_index < len(parts):
        if parts[provider_index].casefold() != "providers":
            raise ValueError(
                "resourceId extension path must begin with a providers segment"
            )
        provider_markers.append(provider_index)
        if provider_index + 3 >= len(parts):
            raise ValueError(
                "resourceId providers namespace must be followed by complete "
                "type/name pairs"
            )

        pair_index = provider_index + 2
        while True:
            if pair_index + 1 >= len(parts):
                raise ValueError(
                    "resourceId providers namespace must be followed by complete "
                    "type/name pairs"
                )
            # Consume one complete type/name pair. A resource name may itself be
            # "providers"; only the next type-position can begin an extension.
            pair_index += 2
            if pair_index == len(parts):
                provider_index = len(parts)
                break
            if parts[pair_index].casefold() == "providers":
                provider_index = pair_index
                break

    for position, part in enumerate(parts):
        _segment(part, f"resourceId segment {position + 1}")

    canonical = list(parts)
    canonical[0] = "subscriptions"
    if len(canonical) > 2 and canonical[2].casefold() == "resourcegroups":
        canonical[2] = "resourceGroups"
    for position in provider_markers:
        canonical[position] = "providers"
    return "/" + "/".join(canonical)


def _subscription_from_id(value):
    canonical = canonicalize_resource_id(value)
    parts = canonical.split("/")
    return f"/subscriptions/{parts[2]}"


def _resource_group_from_id(value):
    canonical = canonicalize_resource_id(value)
    parts = canonical.split("/")
    if len(parts) < 5 or parts[3].casefold() != "resourcegroups":
        return None
    return (
        f"/subscriptions/{parts[2]}/resourceGroups/{parts[4]}"
    )


def _target_details(target):
    try:
        canonical = canonicalize_scope(target)
        return {
            "target": canonical,
            "kind": scope_kind(canonical),
            "subscription": (
                _subscription_from_id(canonical)
                if scope_kind(canonical) != "management_group"
                else None
            ),
            "resource_group": (
                canonical if scope_kind(canonical) == "resource_group" else None
            ),
        }
    except ValueError:
        canonical = canonicalize_resource_id(target)
        return {
            "target": canonical,
            "kind": "resource",
            "subscription": _subscription_from_id(canonical),
            "resource_group": _resource_group_from_id(canonical),
        }


def _containment_index(resolved):
    management_groups = {}
    for group in resolved["management_group_scopes"]:
        descendants = resolved["management_group_descendants"].get(group, [group])
        for descendant in descendants:
            management_groups.setdefault(descendant.casefold(), group)

    subscriptions = {}
    for subscription in resolved["subscription_scopes"]:
        subscriptions.setdefault(subscription.casefold(), subscription)

    resource_groups = {}
    for resource_group in resolved["resource_group_scopes"]:
        resource_groups.setdefault(resource_group.casefold(), resource_group)

    return {
        "management_groups": management_groups,
        "subscriptions": subscriptions,
        "resource_groups": resource_groups,
    }


def _evaluate_indexed_containment(resolved, index, requested_scope_or_resource_id):
    target = _target_details(requested_scope_or_resource_id)
    target_key = target["target"].casefold()

    containing_scope = None
    reason = "outside_managed_scope"
    if target["kind"] == "management_group":
        containing_scope = index["management_groups"].get(target_key)
        if containing_scope is not None:
            reason = "management_group"
    if containing_scope is None and target["subscription"]:
        containing_scope = index["subscriptions"].get(
            target["subscription"].casefold()
        )
        if containing_scope is not None:
            reason = "subscription"
    if containing_scope is None and target["resource_group"]:
        containing_scope = index["resource_groups"].get(
            target["resource_group"].casefold()
        )
        if containing_scope is not None:
            reason = "resource_group"

    return {
        "contained": containing_scope is not None,
        "target": target["target"],
        "target_kind": target["kind"],
        "containing_scope": containing_scope,
        "reason": reason,
        "diagnostics": resolved["diagnostics"],
    }


def evaluate_containment(
    managed_scopes, requested_scope_or_resource_id, management_group_expansions=None
):
    """Return a deterministic containment decision for a requested scope/resource."""

    resolved = resolve_managed_scopes(
        managed_scopes, management_group_expansions=management_group_expansions
    )
    return _evaluate_indexed_containment(
        resolved,
        _containment_index(resolved),
        requested_scope_or_resource_id,
    )


def scope_contains(
    managed_scopes, requested_scope_or_resource_id, management_group_expansions=None
):
    """Boolean convenience wrapper around evaluate_containment."""

    if isinstance(managed_scopes, (str, dict)):
        managed_scopes = [managed_scopes]
    return evaluate_containment(
        managed_scopes,
        requested_scope_or_resource_id,
        management_group_expansions,
    )["contained"]


def is_contained(
    requested_scope_or_resource_id, managed_scopes, management_group_expansions=None
):
    """Boolean containment helper with the requested target as the first argument."""

    return scope_contains(
        managed_scopes,
        requested_scope_or_resource_id,
        management_group_expansions,
    )


is_within_managed_scope = is_contained


def _ci_get(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        if name.casefold() in lowered and lowered[name.casefold()] not in (None, ""):
            return lowered[name.casefold()]
    properties = lowered.get("properties")
    if isinstance(properties, dict):
        return _ci_get(properties, *names)
    return None


def _row_resource_id(row):
    instance_name = _ci_get(row, "instanceName")
    resource_id = _ci_get(row, "resourceId")
    last_error = None
    for value in (instance_name, resource_id):
        if not value or not str(value).strip().startswith("/"):
            continue
        try:
            return canonicalize_resource_id(value), None
        except ValueError as error:
            # instanceName sometimes contains a plain instance label; only report
            # malformed values that looked like ARM IDs.
            last_error = str(error)
    return None, last_error


def _metadata_scope(row):
    subscription = _ci_get(
        row, "subscriptionId", "subscriptionGuid", "subscription"
    )
    resource_group = _ci_get(row, "resourceGroup", "resourceGroupName")
    if not subscription:
        return None
    try:
        subscription_scope = _subscription_scope(subscription)
        if resource_group:
            raw_resource_group = str(resource_group).strip()
            if raw_resource_group.startswith("/"):
                canonical = canonicalize_scope(raw_resource_group)
                if scope_kind(canonical) != "resource_group":
                    return None
                if _subscription_from_id(canonical).casefold() != subscription_scope.casefold():
                    return None
                return canonical
            return (
                f"{subscription_scope}/resourceGroups/"
                f"{_segment(resource_group, 'resourceGroupName')}"
            )
        return subscription_scope
    except ValueError:
        return None


def _normalise_for_fingerprint(value, key=""):
    if isinstance(value, dict):
        return {
            str(item_key).casefold(): _normalise_for_fingerprint(
                item_value, str(item_key)
            )
            for item_key, item_value in sorted(
                value.items(), key=lambda pair: str(pair[0]).casefold()
            )
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_for_fingerprint(item, key) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str) and key.casefold() in {
        "id",
        "instanceid",
        "instancename",
        "resourceid",
        "subscriptionid",
        "subscriptionguid",
        "resourcegroup",
        "resourcegroupname",
    }:
        return value.strip().casefold()
    return value


def _row_identity(row):
    explicit = _ci_get(
        row, "usageDetailId", "usageRecordId", "chargeId", "billingRecordId"
    )
    if not explicit:
        top_level = {str(key).casefold(): value for key, value in row.items()}
        explicit = top_level.get("id")
    if explicit:
        return "id:" + str(explicit).strip().casefold()
    payload = json.dumps(
        _normalise_for_fingerprint(row),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "row:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_cost(row):
    value = _ci_get(row, "cost", "costInUSD", "pretaxCost")
    if value in (None, ""):
        return Decimal("0"), "missing cost; treated as 0"
    if isinstance(value, bool):
        return Decimal("0"), "boolean cost is invalid; treated as 0"
    try:
        cost = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0"), f"invalid cost {value!r}; treated as 0"
    if not cost.is_finite():
        return Decimal("0"), f"non-finite cost {value!r}; treated as 0"
    return cost, None


def filter_usage_details(
    rows, managed_scopes, management_group_expansions=None
):
    """Filter UsageDetails rows to managed scope and report exact Decimal coverage.

    instanceName is preferred over resourceId. Rows without a usable ARM ID fall
    back to resourceGroup + subscription metadata. A subscription-only shared charge
    is included only when that subscription (not merely one RG in it) is managed.
    """

    resolved = resolve_managed_scopes(
        managed_scopes, management_group_expansions=management_group_expansions
    )
    containment_index = _containment_index(resolved)
    diagnostics = list(resolved["diagnostics"])
    included_rows = []
    excluded_rows = []
    unattributed_rows = []
    included_cost = Decimal("0")
    excluded_cost = Decimal("0")
    unattributed_cost = Decimal("0")
    seen = {}
    duplicate_count = 0
    duplicate_cost = Decimal("0")
    input_rows = list(rows or ())

    for index, row in enumerate(input_rows):
        if not isinstance(row, dict):
            diagnostics.append(
                _diagnostic(
                    "invalid_usage_row",
                    f"UsageDetails row {index} is not an object",
                    row_index=index,
                )
            )
            row = {"value": row}

        identity = _row_identity(row)
        if identity in seen:
            duplicate_count += 1
            duplicate_cost += _row_cost(row)[0]
            diagnostics.append(
                _diagnostic(
                    "duplicate_usage_row",
                    f"Removed duplicate UsageDetails row {index}",
                    row_index=index,
                    duplicate_of=seen[identity],
                )
            )
            continue
        seen[identity] = index

        cost, cost_error = _row_cost(row)
        if cost_error:
            diagnostics.append(
                _diagnostic(
                    "invalid_cost",
                    f"UsageDetails row {index}: {cost_error}",
                    row_index=index,
                )
            )

        resource_id, id_error = _row_resource_id(row)
        metadata_scope = _metadata_scope(row)
        if id_error:
            diagnostics.append(
                _diagnostic(
                    "malformed_resource_id",
                    f"UsageDetails row {index}: {id_error}",
                    row_index=index,
                )
            )
        target = resource_id or metadata_scope
        if target is None:
            unattributed_rows.append(row)
            unattributed_cost += cost
            diagnostics.append(
                _diagnostic(
                    "unattributed_usage_row",
                    f"UsageDetails row {index} has no usable resource or scope identity",
                    row_index=index,
                    detail=id_error,
                )
            )
            continue

        decision = _evaluate_indexed_containment(
            resolved,
            containment_index,
            target,
        )
        if decision["contained"]:
            included_rows.append(row)
            included_cost += cost
        else:
            excluded_rows.append(row)
            excluded_cost += cost
            diagnostics.append(
                _diagnostic(
                    "outside_managed_scope",
                    f"Excluded UsageDetails row {index} outside managed scope",
                    row_index=index,
                    target=decision["target"],
                )
            )

    included_count = len(included_rows)
    excluded_count = len(excluded_rows)
    unattributed_count = len(unattributed_rows)
    unique_count = included_count + excluded_count + unattributed_count
    total_cost = included_cost + excluded_cost + unattributed_cost
    attributed_count = included_count + excluded_count
    coverage = (
        (Decimal(attributed_count) * Decimal("100") / Decimal(unique_count)).quantize(
            Decimal("0.01")
        )
        if unique_count
        else Decimal("100.00")
    )

    return {
        "included_rows": included_rows,
        "excluded_rows": excluded_rows,
        "unattributed_rows": unattributed_rows,
        "included": included_rows,
        "excluded": excluded_rows,
        "unattributed": unattributed_rows,
        "input_count": len(input_rows),
        "unique_count": unique_count,
        "duplicate_count": duplicate_count,
        "duplicate_cost": duplicate_cost,
        "duplicate_cost_usd": duplicate_cost,
        "included_count": included_count,
        "excluded_count": excluded_count,
        "unattributed_count": unattributed_count,
        "included_cost": included_cost,
        "excluded_cost": excluded_cost,
        "unattributed_cost": unattributed_cost,
        "included_cost_usd": included_cost,
        "excluded_cost_usd": excluded_cost,
        "unattributed_cost_usd": unattributed_cost,
        "attributed_cost": included_cost + excluded_cost,
        "total_cost": total_cost,
        "attribution_coverage_pct": coverage,
        "diagnostics": diagnostics,
    }


filter_usage_rows = filter_usage_details
filter_usage_details_by_scope = filter_usage_details


def decide_scope_policy(
    requested_scopes,
    managed_scopes,
    *,
    mode="interactive",
    outside_scope_confirmed=None,
    confirmation_key=None,
    management_group_expansions=None,
):
    """Decide whether requested scopes may be used.

    Scheduled mode is strict and fail-closed. Interactive mode returns an explicit
    confirmation-required state for any target outside managed scope. An affirmative
    ``outside_scope_confirmed=True`` permits that broader request only when
    ``confirmation_key`` exactly matches the key computed for the current request.
    """

    mode = str(mode or "").strip().casefold()
    if mode not in {"scheduled", "interactive"}:
        raise ValueError("mode must be 'scheduled' or 'interactive'")

    requested = (
        [requested_scopes]
        if isinstance(requested_scopes, (str, dict))
        else list(requested_scopes or ())
    )
    try:
        resolved = resolve_managed_scopes(
            managed_scopes, management_group_expansions=management_group_expansions
        )
    except ValueError as error:
        return {
            "allowed": False,
            "decision": "deny",
            "state": "invalid_managed_scope",
            "confirmation_state": "not_permitted",
            "requires_confirmation": False,
            "inside_scopes": [],
            "outside_scopes": [],
            "confirmation_key": None,
            "diagnostics": [
                _diagnostic("invalid_managed_scope", str(error))
            ],
        }

    diagnostics = list(resolved["diagnostics"])
    if not resolved["configured_scopes"]:
        diagnostics.append(
            _diagnostic(
                "empty_managed_scope",
                "No managed scopes are configured",
            )
        )
        return {
            "allowed": False,
            "decision": "deny",
            "state": "empty_managed_scope",
            "confirmation_state": "not_permitted",
            "requires_confirmation": False,
            "inside_scopes": [],
            "outside_scopes": [],
            "confirmation_key": None,
            "diagnostics": diagnostics,
        }

    if mode == "scheduled" and resolved["unexpanded_management_groups"]:
        return {
            "allowed": False,
            "decision": "deny",
            "state": "management_group_expansion_required",
            "confirmation_state": "not_permitted",
            "requires_confirmation": False,
            "inside_scopes": [],
            "outside_scopes": [],
            "confirmation_key": None,
            "diagnostics": diagnostics,
        }

    inside = []
    outside = []
    invalid = []
    for value in requested:
        try:
            decision = evaluate_containment(
                resolved["configured_scopes"],
                value,
                management_group_expansions=management_group_expansions,
            )
            (inside if decision["contained"] else outside).append(decision["target"])
        except ValueError as error:
            invalid.append(str(value))
            diagnostics.append(
                _diagnostic(
                    "invalid_requested_scope",
                    str(error),
                    scope=str(value),
                )
            )

    def _sorted_ci_unique(values):
        unique = {}
        for value in values:
            unique.setdefault(value.casefold(), value)
        return [unique[key] for key in sorted(unique)]

    inside = _sorted_ci_unique(inside)
    outside = _sorted_ci_unique(outside)
    invalid = _sorted_ci_unique(invalid)
    if invalid:
        return {
            "allowed": False,
            "decision": "deny",
            "state": "invalid_requested_scope",
            "confirmation_state": "not_permitted",
            "requires_confirmation": False,
            "inside_scopes": inside,
            "outside_scopes": outside,
            "invalid_scopes": invalid,
            "confirmation_key": None,
            "diagnostics": diagnostics,
        }
    expected_confirmation_key = (
        hashlib.sha256(
            json.dumps(
                {
                    "inside_scopes": [item.casefold() for item in inside],
                    "outside_scopes": [item.casefold() for item in outside],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        if outside
        else None
    )

    if not outside:
        return {
            "allowed": True,
            "decision": "allow",
            "state": "within_managed_scope",
            "confirmation_state": "not_required",
            "requires_confirmation": False,
            "inside_scopes": inside,
            "outside_scopes": [],
            "confirmation_key": None,
            "diagnostics": diagnostics,
        }

    diagnostics.append(
        _diagnostic(
            "requested_scope_outside_managed_scope",
            "One or more requested scopes are outside the agent's managed scope",
            scopes=outside,
        )
    )
    if mode == "scheduled":
        return {
            "allowed": False,
            "decision": "deny",
            "state": "outside_managed_scope",
            "confirmation_state": "not_permitted",
            "requires_confirmation": False,
            "inside_scopes": inside,
            "outside_scopes": outside,
            "confirmation_key": expected_confirmation_key,
            "diagnostics": diagnostics,
        }
    if outside_scope_confirmed is True:
        if confirmation_key is None:
            diagnostics.append(
                _diagnostic(
                    "outside_scope_confirmation_key_missing",
                    "Outside-scope confirmation was supplied without the key "
                    "displayed for this request",
                    expected_confirmation_key=expected_confirmation_key,
                )
            )
            return {
                "allowed": False,
                "decision": "confirm",
                "state": "outside_scope_confirmation_key_required",
                "confirmation_state": "key_required",
                "requires_confirmation": True,
                "inside_scopes": inside,
                "outside_scopes": outside,
                "confirmation_key": expected_confirmation_key,
                "diagnostics": diagnostics,
            }
        if confirmation_key != expected_confirmation_key:
            diagnostics.append(
                _diagnostic(
                    "outside_scope_confirmation_key_mismatch",
                    "The supplied confirmation key does not match the current "
                    "outside-scope request",
                    supplied_confirmation_key=str(confirmation_key),
                    expected_confirmation_key=expected_confirmation_key,
                )
            )
            return {
                "allowed": False,
                "decision": "confirm",
                "state": "outside_scope_confirmation_key_mismatch",
                "confirmation_state": "key_mismatch",
                "requires_confirmation": True,
                "inside_scopes": inside,
                "outside_scopes": outside,
                "confirmation_key": expected_confirmation_key,
                "diagnostics": diagnostics,
            }
        return {
            "allowed": True,
            "decision": "allow",
            "state": "outside_scope_confirmed",
            "confirmation_state": "confirmed",
            "requires_confirmation": False,
            "inside_scopes": inside,
            "outside_scopes": outside,
            "confirmation_key": expected_confirmation_key,
            "diagnostics": diagnostics,
        }
    if outside_scope_confirmed is False:
        return {
            "allowed": False,
            "decision": "deny",
            "state": "outside_scope_declined",
            "confirmation_state": "declined",
            "requires_confirmation": False,
            "inside_scopes": inside,
            "outside_scopes": outside,
            "confirmation_key": expected_confirmation_key,
            "diagnostics": diagnostics,
        }
    return {
        "allowed": False,
        "decision": "confirm",
        "state": "outside_scope_confirmation_required",
        "confirmation_state": "required",
        "requires_confirmation": True,
        "inside_scopes": inside,
        "outside_scopes": outside,
        "confirmation_key": expected_confirmation_key,
        "diagnostics": diagnostics,
    }


evaluate_scope_policy = decide_scope_policy
decide_policy = decide_scope_policy
