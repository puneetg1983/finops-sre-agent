"""FinOps cost-allocation (showback) — pure, offline, deterministic (no Azure calls).

Given per-resource monthly cost (from Consumption UsageDetails) and per-resource tags
(from Resource Graph), attribute every dollar to an owner dimension and — the governance win —
surface the spend that **has no owner**, plus an inventory of the tags you actually use and how
they measure up against a recommended ownership taxonomy.

The skill is deliberately **tag-generic**: it groups by whatever tag keys exist in your data (a
tag inventory), and treats `team / env / service / costCenter / app / owner` as a *recommended*
set to report coverage against — not a hard-coded filter that defines "tagged".

Design decisions (see SKILL.md):
  * Shared / untaggable cost is NEVER force-allocated. Cost with no value for the requested
    dimension lands in an explicit "unallocated" bucket so the math stays honest and the gap
    is the actionable signal.
  * A resource with **no tags at all** is "untagged" (dimension-independent) and is listed for
    tagging, ranked by cost. (Having *some* tag but not the requested dimension is "unallocated",
    not "untagged".)
  * The **tag inventory** reports every tag key present, with the resource count and cost it
    covers — the "group by the ones we have" view.
  * **Recommended coverage** reports, for each recommended ownership key, whether it is present
    and what share of cost it covers, so gaps become an adopt-these-tags recommendation.
  * Tag values are grouped case-insensitively (trimmed + lower-cased); the most costly raw
    spelling is shown. Values that collapse to the same owner are flagged as tag-hygiene issues
    (e.g. "Prod" vs "production") because they otherwise split one owner's spend.

Input shapes (all keyed/looked-up by resourceId, matched case-insensitively):

    costs   {resourceId: monthly_usd}                     # aggregated from UsageDetails costInUSD
    tags    {resourceId: {"team": "payments", "env": "Prod", ...}}   # from Resource Graph

Output: a single dict (see allocate_costs) — a showback breakdown, an unallocated bucket, a
ranked untagged-resource list, a tag inventory, recommended-coverage, and tag-hygiene flags.
Feed it to a table / Live Report.
"""

from collections import defaultdict

# Recommended ownership taxonomy — advisory only. Coverage against this set is reported; it does
# NOT define whether a resource is "tagged" (that is now "has any tag at all").
RECOMMENDED_TAG_KEYS = ("team", "env", "service", "costCenter", "app", "owner")
DEFAULT_DIMENSION = "team"


def _key(resource_id) -> str:
    """Case-insensitive resource-id key (ARM ids are case-insensitive)."""
    return str(resource_id or "").strip().lower()


def _norm_val(value) -> str:
    """Normalize a tag value for grouping: trimmed + lower-cased. '' if empty."""
    return str(value or "").strip().lower()


def _tags_ci(tag_map):
    """Return a copy of a resource's tag dict with keys lower-cased for lookup."""
    return {str(k).strip().lower(): v for k, v in (tag_map or {}).items()}


def _lookup_tag(ci_tags, wanted_key):
    """Case-insensitive tag lookup; returns the raw value or None if absent/empty."""
    raw = ci_tags.get(str(wanted_key).strip().lower())
    return raw if _norm_val(raw) else None


def allocate_costs(
    costs=None,
    tags=None,
    *,
    dimension: str = DEFAULT_DIMENSION,
    recommended_keys=RECOMMENDED_TAG_KEYS,
    top_n_resources: int = 25,
):
    """Attribute monthly cost to an owner dimension, keeping unallocated spend explicit.

    costs           {resourceId: monthly_usd}
    tags            {resourceId: {tagKey: tagVal}}
    dimension       the ownership tag key to group by (default "team")
    recommended_keys the advisory best-practice ownership keys to report coverage against; this
                    does NOT define "tagged" (a resource is untagged only if it has NO tags)
    top_n_resources cap on how many resources are listed inside the unallocated/untagged details

    Returns a dict:
      {
        "dimension", "total_usd", "allocated_usd", "unallocated_usd", "unallocated_pct",
        "groups": [{"owner", "monthly_usd", "pct", "resource_count"}...] sorted desc,
        "unallocated": {"monthly_usd", "pct", "resource_count", "resources": [{resourceId, monthly_usd}...]},
        "untagged_resources": [{resourceId, monthly_usd}...],   # NO tags at all
        "untagged_usd",
        "tag_inventory": [{"key", "resource_count", "cost_usd", "pct"}...],   # every tag key present, ranked
        "recommended_coverage": [{"key", "present", "resource_count", "cost_usd", "pct"}...],  # in recommended order
        "missing_recommended": [key...],   # recommended keys with no usage at all
        "tag_hygiene": [{"canonical", "variants": {raw: cost}, "cost_affected"}...],
      }
    """
    costs = costs or {}
    tags = tags or {}
    dim = str(dimension).strip()
    rec_keys = tuple(recommended_keys) or RECOMMENDED_TAG_KEYS

    # index raw tag maps case-insensitively by resource id (raw keys/values preserved for display)
    tags_by_id = {_key(rid): (t or {}) for rid, t in tags.items()}

    group_cost = defaultdict(float)          # normalized owner value -> cost
    group_count = defaultdict(int)
    group_raw = defaultdict(lambda: defaultdict(float))  # norm -> {raw spelling: cost} (display + hygiene)
    key_cost = defaultdict(float)            # normalized tag key -> cost covered
    key_count = defaultdict(int)             # normalized tag key -> resource count
    key_raw = defaultdict(lambda: defaultdict(float))    # normalized tag key -> {raw key spelling: cost}
    unallocated_cost = 0.0
    unallocated = []                          # resources with no value for `dim`
    untagged = []                             # resources with NO tags at all
    total = 0.0

    for rid, monthly in costs.items():
        if not isinstance(monthly, (int, float)):
            continue
        total += monthly
        raw_tags = tags_by_id.get(_key(rid), {})
        ci = _tags_ci(raw_tags)

        # dimension grouping
        raw = _lookup_tag(ci, dim)
        if raw is not None:
            norm = _norm_val(raw)
            group_cost[norm] += monthly
            group_count[norm] += 1
            group_raw[norm][str(raw).strip()] += monthly
        else:
            unallocated_cost += monthly
            unallocated.append({"resourceId": rid, "monthly_usd": round(monthly, 2)})

        # tag inventory — count every tag key with a non-empty value on this resource
        present = False
        for raw_key, raw_val in raw_tags.items():
            if not _norm_val(raw_val):
                continue
            present = True
            kn = str(raw_key).strip().lower()
            key_cost[kn] += monthly
            key_count[kn] += 1
            key_raw[kn][str(raw_key).strip()] += monthly

        # untagged = no tags at all (dimension-independent governance list)
        if not present:
            untagged.append({"resourceId": rid, "monthly_usd": round(monthly, 2)})

    allocated_cost = total - unallocated_cost

    groups = []
    for norm, cost in group_cost.items():
        display = max(group_raw[norm].items(), key=lambda kv: kv[1])[0]  # most-costly raw spelling
        groups.append({
            "owner": display,
            "monthly_usd": round(cost, 2),
            "pct": _pct(cost, total),
            "resource_count": group_count[norm],
        })
    groups.sort(key=lambda g: g["monthly_usd"], reverse=True)

    unallocated.sort(key=lambda r: r["monthly_usd"], reverse=True)
    untagged.sort(key=lambda r: r["monthly_usd"], reverse=True)
    untagged_usd = sum(r["monthly_usd"] for r in untagged)

    # tag inventory — every key present, ranked by cost covered
    tag_inventory = []
    for kn, cost in key_cost.items():
        display = max(key_raw[kn].items(), key=lambda kv: kv[1])[0]  # most-costly raw key spelling
        tag_inventory.append({
            "key": display,
            "resource_count": key_count[kn],
            "cost_usd": round(cost, 2),
            "pct": _pct(cost, total),
        })
    tag_inventory.sort(key=lambda t: t["cost_usd"], reverse=True)

    # recommended coverage — for each recommended key, present? and how much cost it covers
    recommended_coverage = []
    missing_recommended = []
    for rk in rec_keys:
        kn = str(rk).strip().lower()
        present = kn in key_cost
        recommended_coverage.append({
            "key": rk,
            "present": present,
            "resource_count": key_count.get(kn, 0),
            "cost_usd": round(key_cost.get(kn, 0.0), 2),
            "pct": _pct(key_cost.get(kn, 0.0), total),
        })
        if not present:
            missing_recommended.append(rk)

    hygiene = []
    for norm, raws in group_raw.items():
        if len(raws) > 1:  # same owner spelled multiple ways -> splits their spend
            canonical = max(raws.items(), key=lambda kv: kv[1])[0]
            hygiene.append({
                "canonical": canonical,
                "variants": {k: round(v, 2) for k, v in sorted(raws.items(), key=lambda kv: kv[1], reverse=True)},
                "cost_affected": round(sum(raws.values()), 2),
            })
    hygiene.sort(key=lambda h: h["cost_affected"], reverse=True)

    return {
        "dimension": dim,
        "total_usd": round(total, 2),
        "allocated_usd": round(allocated_cost, 2),
        "unallocated_usd": round(unallocated_cost, 2),
        "unallocated_pct": _pct(unallocated_cost, total),
        "groups": groups,
        "unallocated": {
            "monthly_usd": round(unallocated_cost, 2),
            "pct": _pct(unallocated_cost, total),
            "resource_count": len(unallocated),
            "resources": unallocated[:top_n_resources],
        },
        "untagged_resources": untagged[:top_n_resources],
        "untagged_usd": round(untagged_usd, 2),
        "tag_inventory": tag_inventory,
        "recommended_coverage": recommended_coverage,
        "missing_recommended": missing_recommended,
        "tag_hygiene": hygiene,
    }


def _pct(part, whole) -> float:
    """part/whole as a rounded percentage; 0.0 when whole is 0."""
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)
