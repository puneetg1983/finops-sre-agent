"""Canonical managed-scope parsing and management-group resolution."""

from __future__ import annotations

import re


_SUBSCRIPTION_RE = re.compile(r"^/subscriptions/([^/]+)$", re.IGNORECASE)
_RESOURCE_GROUP_RE = re.compile(
    r"^/subscriptions/([^/]+)/resourcegroups/([^/]+)$", re.IGNORECASE
)
_MANAGEMENT_GROUP_RE = re.compile(
    r"^/providers/microsoft\.management/managementgroups/([^/]+)$",
    re.IGNORECASE,
)
_INVALID_PATH_CHARS = frozenset("\\?#")


def _segment(value, field):
    text = str(value or "")
    if not text or text != text.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if "/" in text or any(ord(char) < 32 for char in text) or any(
        char in _INVALID_PATH_CHARS for char in text
    ):
        raise ValueError(f"{field} contains unsafe path characters")
    return text


def _scope_value(value):
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in ("id", "resourceid", "scope"):
            if lowered.get(key):
                return lowered[key]
        raise ValueError("managed resource object must contain id, resourceId, or scope")
    return value


def canonicalize_scope(scope):
    """Validate and canonicalize a subscription, resource-group, or MG scope ID.

    ARM path keywords use canonical casing; caller-provided identifier casing is
    preserved. Surrounding whitespace and trailing slashes are harmlessly removed.
    """

    raw = str(_scope_value(scope) or "").strip()
    if raw != "/":
        raw = raw.rstrip("/")

    match = _SUBSCRIPTION_RE.fullmatch(raw)
    if match:
        subscription = _segment(match.group(1), "subscriptionId")
        return f"/subscriptions/{subscription}"

    match = _RESOURCE_GROUP_RE.fullmatch(raw)
    if match:
        subscription = _segment(match.group(1), "subscriptionId")
        resource_group = _segment(match.group(2), "resourceGroupName")
        return f"/subscriptions/{subscription}/resourceGroups/{resource_group}"

    match = _MANAGEMENT_GROUP_RE.fullmatch(raw)
    if match:
        management_group = _segment(match.group(1), "managementGroupId")
        return (
            "/providers/Microsoft.Management/managementGroups/"
            f"{management_group}"
        )

    raise ValueError(
        "scope must be a subscription, resource group, or management group ARM scope ID"
    )


canonical_scope = canonicalize_scope
canonicalize_scope_id = canonicalize_scope


def validate_scope(scope):
    """Return the canonical scope, or raise ValueError when unsupported/malformed."""

    return canonicalize_scope(scope)


validate_scope_id = validate_scope


def scope_kind(scope):
    canonical = canonicalize_scope(scope)
    lowered = canonical.lower()
    if lowered.startswith("/subscriptions/") and "/resourcegroups/" in lowered:
        return "resource_group"
    if lowered.startswith("/subscriptions/"):
        return "subscription"
    return "management_group"


def _subscription_scope(value):
    raw = str(value or "").strip().rstrip("/")
    lowered = raw.casefold()
    if "/subscriptions/" in lowered and not lowered.startswith("/subscriptions/"):
        raw = raw.rsplit("/", 1)[-1]
    if raw.lower().startswith("/subscriptions/"):
        canonical = canonicalize_scope(raw)
        if scope_kind(canonical) != "subscription":
            raise ValueError(f"expected subscription scope, got {raw!r}")
        return canonical
    return f"/subscriptions/{_segment(raw, 'subscriptionId')}"


def _management_group_scope(value):
    raw = str(value or "").strip()
    if raw.lower().startswith("/providers/"):
        canonical = canonicalize_scope(raw)
        if scope_kind(canonical) != "management_group":
            raise ValueError(f"expected management-group scope, got {raw!r}")
        return canonical
    return (
        "/providers/Microsoft.Management/managementGroups/"
        f"{_segment(raw, 'managementGroupId')}"
    )


def _diagnostic(code, message, **details):
    result = {"code": code, "message": message}
    result.update(details)
    return result


def deduplicate_scopes(scopes):
    """Canonicalize scopes and remove duplicates case-insensitively, keeping first."""

    if isinstance(scopes, (str, dict)):
        scopes = [scopes]
    unique = []
    seen = set()
    for value in scopes or ():
        canonical = canonicalize_scope(value)
        key = canonical.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(canonical)
    return unique


def _normalise_expansion_records(expansions):
    """Return {mg-key: {scope, subscriptions, management_groups}}.

    Accepted inputs are a mapping keyed by MG ID/scope, or records containing a
    managementGroup/id/scope plus subscriptions and managementGroups/children.
    """

    if not expansions:
        return {}

    if isinstance(expansions, dict):
        records = list(expansions.items())
    else:
        records = []
        for record in expansions:
            if not isinstance(record, dict):
                raise ValueError("management-group expansion records must be objects")
            lowered = {str(key).lower(): value for key, value in record.items()}
            key = (
                lowered.get("managementgroup")
                or lowered.get("management_group")
                or lowered.get("scope")
                or lowered.get("id")
            )
            if not key:
                raise ValueError("management-group expansion record has no group ID")
            records.append((key, record))

    normalised = {}
    for raw_group, raw_value in records:
        group = _management_group_scope(raw_group)
        subscriptions = []
        child_groups = []

        def _as_list(values):
            if values is None:
                return []
            if isinstance(values, (str, dict)):
                return [values]
            return list(values)

        if isinstance(raw_value, dict):
            lowered = {str(key).lower(): value for key, value in raw_value.items()}
            has_expansion_fields = not lowered or any(
                key in lowered
                for key in (
                    "subscriptions",
                    "subscriptionids",
                    "subscription_ids",
                    "managementgroups",
                    "management_groups",
                    "childmanagementgroups",
                    "children",
                )
            )
            subscriptions = _as_list(
                lowered.get("subscriptions")
                or lowered.get("subscriptionids")
                or lowered.get("subscription_ids")
            )
            child_groups = _as_list(
                lowered.get("managementgroups")
                or lowered.get("management_groups")
                or lowered.get("childmanagementgroups")
            )
            for child in _as_list(lowered.get("children")):
                if isinstance(child, dict):
                    lowered_child = {
                        str(key).lower(): value for key, value in child.items()
                    }
                    child_type = str(lowered_child.get("type") or "").casefold()
                    child_id = str(
                        lowered_child.get("id") or lowered_child.get("name") or ""
                    ).strip()
                    if (
                        "subscription" in child_type
                        or child_id.casefold().startswith("/subscriptions/")
                    ):
                        subscriptions.append(child)
                        continue
                child_groups.append(child)
            if not has_expansion_fields:
                raw_value = [raw_value]
        if not isinstance(raw_value, dict):
            values = (
                raw_value
                if isinstance(raw_value, (list, tuple, set))
                else [raw_value]
            )
            for value in values:
                if isinstance(value, dict):
                    lowered = {
                        str(key).lower(): item for key, item in value.items()
                    }
                    item_id = lowered.get("id") or lowered.get("name")
                    item_type = str(lowered.get("type") or "").lower()
                    if not item_id:
                        raise ValueError(
                            "management-group descendant has no id or name"
                        )
                    if "subscription" in item_type:
                        subscriptions.append(item_id)
                    elif "managementgroup" in item_type:
                        child_groups.append(item_id)
                    elif str(item_id).casefold().startswith("/subscriptions/"):
                        subscriptions.append(item_id)
                    else:
                        child_groups.append(item_id)
                elif str(value).strip().lower().startswith("/providers/"):
                    child_groups.append(value)
                else:
                    subscriptions.append(value)

        subscription_scopes = []
        for value in _as_list(subscriptions):
            if isinstance(value, dict):
                lowered = {
                    str(key).lower(): item for key, item in value.items()
                }
                value = (
                    lowered.get("id")
                    or lowered.get("subscriptionid")
                    or lowered.get("name")
                )
            subscription_scopes.append(_subscription_scope(value))

        management_group_scopes = []
        for value in _as_list(child_groups):
            if isinstance(value, dict):
                lowered = {
                    str(key).lower(): item for key, item in value.items()
                }
                value = lowered.get("id") or lowered.get("name")
            management_group_scopes.append(_management_group_scope(value))

        normalised[group.casefold()] = {
            "scope": group,
            "subscriptions": deduplicate_scopes(subscription_scopes),
            "management_groups": deduplicate_scopes(management_group_scopes),
        }
    return normalised


def _subscription_from_scope(value):
    canonical = canonicalize_scope(value)
    if scope_kind(canonical) == "management_group":
        raise ValueError("management-group scope has no subscription")
    return "/".join(canonical.split("/")[:3])


def resolve_managed_scopes(managed_scopes, management_group_expansions=None):
    """Resolve configured scopes into non-overlapping effective subscription/RG scopes.

    Management-group expansion data is supplied by the caller. The result preserves
    configured scopes for audit, removes case-insensitive duplicates, removes RG scopes
    nested under an effective subscription, and reports MG/explicit overlap.
    """

    if isinstance(managed_scopes, (str, dict)):
        managed_scopes = [managed_scopes]
    configured = []
    diagnostics = []
    seen = {}
    for position, value in enumerate(managed_scopes or ()):
        canonical = canonicalize_scope(value)
        key = canonical.casefold()
        if key in seen:
            diagnostics.append(
                _diagnostic(
                    "duplicate_scope",
                    f"Removed duplicate managed scope {canonical}",
                    scope=canonical,
                    duplicate_of=seen[key],
                    position=position,
                )
            )
            continue
        seen[key] = canonical
        configured.append(canonical)

    expansions = _normalise_expansion_records(management_group_expansions)
    explicit_subscriptions = [
        value for value in configured if scope_kind(value) == "subscription"
    ]
    explicit_resource_groups = [
        value for value in configured if scope_kind(value) == "resource_group"
    ]
    configured_management_groups = [
        value for value in configured if scope_kind(value) == "management_group"
    ]

    mg_subscriptions = {}
    mg_descendants = {}
    unexpanded = []

    def walk(group, path):
        key = group.casefold()
        if key in path:
            cycle = " -> ".join(path[path.index(key) :] + [key])
            raise ValueError(f"management-group expansion contains a cycle: {cycle}")
        record = expansions.get(key)
        if record is None:
            return set(), {group.casefold(): group}, {group}
        subscriptions = {
            item.casefold(): item for item in record["subscriptions"]
        }
        descendants = {group.casefold(): group}
        missing = set()
        next_path = path + [key]
        for child in record["management_groups"]:
            child_subscriptions, child_descendants, child_missing = walk(
                child, next_path
            )
            subscriptions.update(child_subscriptions)
            descendants.update(child_descendants)
            missing.update(child_missing)
        return subscriptions, descendants, missing

    for group in configured_management_groups:
        subscriptions, descendants, missing = walk(group, [])
        mg_subscriptions[group] = [
            subscriptions[key] for key in sorted(subscriptions)
        ]
        mg_descendants[group] = [
            descendants[key] for key in sorted(descendants)
        ]
        for missing_group in sorted(missing, key=str.casefold):
            if missing_group.casefold() not in {
                item.casefold() for item in unexpanded
            }:
                unexpanded.append(missing_group)
    for group in sorted(unexpanded, key=str.casefold):
        diagnostics.append(
            _diagnostic(
                "unexpanded_management_group",
                f"No descendant expansion was supplied for {group}",
                scope=group,
            )
        )

    effective_subscriptions = []
    effective_seen = {}

    def add_subscription(subscription, source):
        key = subscription.casefold()
        if key in effective_seen:
            previous = effective_seen[key]
            code = (
                "management_group_overlap"
                if source.startswith("management_group:")
                or previous.startswith("management_group:")
                else "duplicate_scope"
            )
            diagnostics.append(
                _diagnostic(
                    code,
                    f"Scope overlap for {subscription}; kept {previous}",
                    scope=subscription,
                    kept_source=previous,
                    removed_source=source,
                )
            )
            return
        effective_seen[key] = source
        effective_subscriptions.append(subscription)

    for subscription in explicit_subscriptions:
        add_subscription(subscription, "explicit")
    for group in configured_management_groups:
        for subscription in mg_subscriptions[group]:
            add_subscription(subscription, f"management_group:{group}")

    effective_resource_groups = []
    for resource_group in explicit_resource_groups:
        subscription = _subscription_from_scope(resource_group)
        if subscription.casefold() in effective_seen:
            diagnostics.append(
                _diagnostic(
                    "nested_scope_removed",
                    f"Removed {resource_group}; parent {subscription} is already managed",
                    scope=resource_group,
                    parent_scope=subscription,
                )
            )
        else:
            effective_resource_groups.append(resource_group)

    effective_scopes = effective_subscriptions + effective_resource_groups
    return {
        "configured_scopes": configured,
        "effective_scopes": effective_scopes,
        "subscription_scopes": effective_subscriptions,
        "resource_group_scopes": effective_resource_groups,
        "management_group_scopes": configured_management_groups,
        "management_group_subscriptions": mg_subscriptions,
        "management_group_descendants": mg_descendants,
        "unexpanded_management_groups": sorted(unexpanded, key=str.casefold),
        "duplicates_removed": sum(
            item["code"] == "duplicate_scope" for item in diagnostics
        ),
        "nested_removed": sum(
            item["code"] == "nested_scope_removed" for item in diagnostics
        ),
        "overlaps_removed": sum(
            item["code"] == "management_group_overlap" for item in diagnostics
        ),
        "diagnostics": diagnostics,
    }


normalize_managed_scopes = resolve_managed_scopes
expand_management_groups = resolve_managed_scopes
normalize_scopes = resolve_managed_scopes
