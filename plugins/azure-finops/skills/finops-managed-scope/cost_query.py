"""Deterministic preparation of ARM MCP ``query_costs`` results.

The Azure Resource Manager (ARM) MCP server's ``query_costs`` tool returns Cost
Management Query results (``properties.columns`` / ``properties.rows``). This module
never calls Azure. It plans the per-scope tool arguments, then validates and flattens
the persisted tool results into the same row shape the FinOps helpers already use for
UsageDetails (``date``, ``cost``, ``resourceId``, ``resourceGroup``, ...), and applies the
managed-scope filter as a defensive check.

Fail-closed rules enforced here:
- every queried scope must be an effective managed scope (or explicitly confirmed);
- costs must be USD (``metric=PreTaxCostUSD``);
- a result with a ``nextLink`` was truncated at ``top`` rows and is incomplete;
- date slices for one scope must share one query shape and must not overlap or leave
  gaps inside the requested window;
- every expected scope must have been queried.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from itertools import zip_longest

from scope import canonicalize_scope, filter_usage_details, resolve_managed_scopes

USD_METRIC = "PreTaxCostUSD"
MAX_TOP = 5000
LOOKBACK_DAYS = 92
SOURCE = "arm-mcp:query_costs"

_COST_COLUMNS = {"pretaxcostusd", "costusd"}
_NON_USD_COST_COLUMNS = {"pretaxcost", "cost", "amortizedcost"}
_COLUMN_FIELDS = {
    "usagedate": "date",
    "billingmonth": "date",
    "resourceid": "resourceId",
    "resourcegroupname": "resourceGroup",
    "resourcegroup": "resourceGroup",
    "subscriptionid": "subscriptionId",
    "subscriptionname": "subscriptionName",
    "servicename": "serviceName",
    "servicetier": "serviceTier",
    "servicefamily": "serviceFamily",
    "metercategory": "meterCategory",
    "metersubcategory": "meterSubCategory",
    "meter": "meterName",
    "meterid": "meterId",
    "resourcetype": "resourceType",
    "resourcelocation": "resourceLocation",
    "chargetype": "chargeType",
    "pricingmodel": "pricingModel",
    "publishertype": "publisherType",
    "usagequantity": "quantity",
    "unitofmeasure": "unitOfMeasure",
    "currency": "currency",
}


def _text(value):
    return str(value or "").strip()


def _iso_date(value, field):
    raw = _text(value)
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from error


def _csv(value):
    return [part.strip() for part in _text(value).split(",") if part.strip()]


def _shape(arguments):
    granularity = _text(arguments.get("granularity")) or "None"
    return (
        granularity.casefold(),
        tuple(item.casefold() for item in _csv(arguments.get("groupBy"))),
        _text(arguments.get("filterDimension")).casefold(),
        tuple(sorted(item.casefold() for item in _csv(arguments.get("filterValues")))),
        _text(arguments.get("metric")).casefold(),
    )


def plan_cost_queries(
    managed_scopes,
    *,
    start,
    end,
    granularity=None,
    group_by=None,
    filter_dimension=None,
    filter_values=None,
    management_group_expansions=None,
    top=MAX_TOP,
    as_of=None,
    slice_days=None,
):
    """Return scoped ``query_costs`` arguments, optionally pre-sliced by UTC date.

    Management groups are expanded to their descendant subscriptions, so no query is
    ever issued at a broader scope than the managed boundary. Raises when a configured
    management group was not expanded. When ``as_of`` (today, UTC) is supplied, a
    ``start`` older than the tool's 92-day lookback is rejected up front.
    ``slice_days`` bounds each Daily query's inclusive date span before retrieval,
    avoiding predictable oversized history probes. It does not guarantee a row count.
    """

    resolved = resolve_managed_scopes(
        managed_scopes, management_group_expansions=management_group_expansions
    )
    if resolved["unexpanded_management_groups"]:
        raise ValueError(
            "management groups must be expanded before planning cost queries: "
            + ", ".join(resolved["unexpanded_management_groups"])
        )
    if not resolved["effective_scopes"]:
        raise ValueError("no effective managed scopes to query")
    start_date = _iso_date(start, "start")
    end_date = _iso_date(end, "end")
    if end_date < start_date:
        raise ValueError("end must not be before start")
    if as_of is not None:
        earliest = _iso_date(as_of, "as_of") - timedelta(days=LOOKBACK_DAYS - 1)
        if start_date < earliest:
            raise ValueError(
                f"query_costs only reaches back {LOOKBACK_DAYS} days; earliest start is "
                f"{earliest.isoformat()}"
            )
    if not 1 <= int(top) <= MAX_TOP:
        raise ValueError(f"top must be between 1 and {MAX_TOP}")
    if bool(filter_dimension) != bool(filter_values):
        raise ValueError("filter_dimension and filter_values must be supplied together")

    if isinstance(group_by, (list, tuple)):
        group_by = ",".join(group_by)
    if isinstance(filter_values, (list, tuple)):
        filter_values = ",".join(filter_values)
    if len(_csv(group_by)) > 5:
        raise ValueError("query_costs accepts at most 5 groupBy dimensions")
    if slice_days is not None:
        if isinstance(slice_days, bool) or not isinstance(slice_days, int) or slice_days < 1:
            raise ValueError("slice_days must be a positive integer")
        if _text(granularity).casefold() != "daily":
            raise ValueError("slice_days requires Daily granularity")

    plans = []
    for scope in resolved["effective_scopes"]:
        arguments = {
            "scope": scope,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "metric": USD_METRIC,
            "top": int(top),
        }
        if granularity:
            arguments["granularity"] = granularity
        if group_by:
            arguments["groupBy"] = group_by
        if filter_dimension:
            arguments["filterDimension"] = filter_dimension
            arguments["filterValues"] = filter_values
        if slice_days is None:
            plans.append(arguments)
        else:
            first = start_date
            while first <= end_date:
                last = min(end_date, first + timedelta(days=slice_days - 1))
                plans.append({**arguments, "from": first.isoformat(), "to": last.isoformat()})
                first = last + timedelta(days=1)
    return plans


def batch_cost_queries(queries, *, max_concurrent=4, max_per_subscription=2):
    """Pack pending read calls into bounded parallel batches without changing arguments.

    This is a pure scheduler, not an Azure client. Dispatch each batch together and wait
    for it before starting the next. Resource groups in the same subscription share the
    per-subscription bound, including calls from different query shapes. After throttling,
    callers can re-batch remaining work with ``max_per_subscription=1``.
    """
    for name, value, ceiling in (
        ("max_concurrent", max_concurrent, 4),
        ("max_per_subscription", max_per_subscription, 2),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
    by_shape, seen = {}, set()
    for arguments in queries:
        scope = canonicalize_scope(arguments["scope"])
        if not scope.casefold().startswith("/subscriptions/"):
            raise ValueError("cost query batches require expanded subscription or resource-group scopes")
        first = _iso_date(arguments.get("from"), "from")
        last = _iso_date(arguments.get("to"), "to")
        if last < first:
            raise ValueError("to must not be before from")
        identity = (scope.casefold(), first, last, _shape(arguments), arguments.get("top"))
        if identity in seen:
            raise ValueError("duplicate pending cost query; reuse its saved result instead")
        seen.add(identity)
        subscription = scope.split("/")[2].casefold()
        by_shape.setdefault(_shape(arguments), []).append((subscription, dict(arguments)))
    pending = [item for wave in zip_longest(*by_shape.values()) for item in wave if item is not None]
    batches = []
    while pending:
        batch, counts, remaining = [], {}, []
        for subscription, arguments in pending:
            if len(batch) < max_concurrent and counts.get(subscription, 0) < max_per_subscription:
                batch.append(arguments)
                counts[subscription] = counts.get(subscription, 0) + 1
            else:
                remaining.append((subscription, arguments))
        batches.append(batch)
        pending = remaining
    return batches


def split_cost_query(arguments):
    """Split a truncated query's date range into two contiguous halves.

    Use when a ``query_costs`` result carried a ``nextLink``. A single-day query
    cannot be split; reduce ``groupBy`` or add a filter instead.
    """

    start_date = _iso_date(arguments.get("from"), "from")
    end_date = _iso_date(arguments.get("to"), "to")
    if end_date <= start_date:
        raise ValueError(
            "a single-day query cannot be split by date; reduce groupBy or add a filter"
        )
    middle = start_date + timedelta(days=(end_date - start_date).days // 2)
    first = {**arguments, "from": start_date.isoformat(), "to": middle.isoformat()}
    second = {
        **arguments,
        "from": (middle + timedelta(days=1)).isoformat(),
        "to": end_date.isoformat(),
    }
    return [first, second]


def _unwrap(result, index):
    """Return Cost Management query properties from any persisted tool-output shape."""

    value = result
    for _ in range(8):
        if isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except ValueError as error:
                raise ValueError(
                    f"query_costs result {index} is not JSON: {_text(value)[:300]}"
                ) from error
            continue
        if not isinstance(value, dict):
            raise ValueError(f"query_costs result {index} must be an object")
        if value.get("isError") is True:
            messages = [
                _text(item.get("text"))
                for item in value.get("content") or []
                if isinstance(item, dict)
            ]
            raise ValueError(
                f"query_costs result {index} is a tool error: "
                + (" ".join(messages) or "no message")[:500]
            )
        if "columns" in value and "rows" in value:
            return value
        if isinstance(value.get("properties"), dict):
            value = value["properties"]
        elif "structuredContent" in value and value["structuredContent"]:
            value = value["structuredContent"]
        elif "result" in value:
            value = value["result"]
        elif isinstance(value.get("content"), list):
            texts = [
                item.get("text")
                for item in value["content"]
                if isinstance(item, dict) and item.get("type", "text") == "text"
            ]
            if not texts:
                break
            value = texts[0]
        else:
            break
    raise ValueError(f"query_costs result {index} has no columns/rows")


def _date_value(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        digits = str(int(value))
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
        if len(digits) == 6:
            return f"{digits[:4]}-{digits[4:]}-01"
    raw = _text(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw[:10]


def _field_name(column):
    key = column.casefold()
    if key in _COST_COLUMNS:
        return "cost"
    if key in _COLUMN_FIELDS:
        return _COLUMN_FIELDS[key]
    return column[:1].lower() + column[1:]


def _rows(properties, index):
    columns = properties.get("columns")
    rows = properties.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError(f"query_costs result {index} columns/rows must be arrays")
    names = []
    for column in columns:
        name = column.get("name") if isinstance(column, dict) else column
        names.append(_text(name))
    lowered = {name.casefold() for name in names}
    if not lowered & _COST_COLUMNS:
        if lowered & _NON_USD_COST_COLUMNS:
            raise ValueError(
                f"query_costs result {index} is not in USD; re-query with metric={USD_METRIC}"
            )
        raise ValueError(f"query_costs result {index} has no cost column")
    fields = [_field_name(name) for name in names]
    flattened = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError(
                f"query_costs result {index} row {row_index} does not match its columns"
            )
        item = {}
        for field, value in zip(fields, row):
            if field == "date":
                value = _date_value(value)
            item[field] = value
        flattened.append(item)
    return flattened


def _scope_parts(scope):
    parts = scope.split("/")
    subscription = parts[2] if len(parts) > 2 else None
    resource_group = parts[4] if len(parts) > 4 else None
    return subscription, resource_group


def _resource_group_from_resource_id(resource_id):
    parts = _text(resource_id).split("/")
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "resourcegroups" and parts[index + 1]:
            return parts[index + 1]
    return None


def prepare_cost_query(
    results,
    managed_scopes,
    management_group_expansions=None,
    *,
    window=None,
    expected_scopes=None,
    confirmed_scopes=None,
    require_complete=True,
):
    """Validate persisted ``query_costs`` results and filter them to managed scope.

    ``results`` is a list of ``{"arguments": {...}, "result": <tool output>}`` objects:
    the exact arguments sent to ``query_costs`` and its raw output (structured
    content, the ``content`` text, or the parsed query object). ``window`` is an
    optional inclusive ``(start, end)`` pair every scope's slices must cover.

    Returns the ``filter_usage_details`` coverage plus query diagnostics. With
    ``require_complete=False`` truncation, gaps and missing scopes are reported
    through ``partial``/``warnings`` instead of raising.
    """

    resolved = resolve_managed_scopes(
        managed_scopes, management_group_expansions=management_group_expansions
    )
    if resolved["unexpanded_management_groups"]:
        raise ValueError(
            "management groups must be expanded before preparing cost results: "
            + ", ".join(resolved["unexpanded_management_groups"])
        )
    effective = {scope.casefold(): scope for scope in resolved["effective_scopes"]}
    confirmed = {}
    for scope in confirmed_scopes or ():
        canonical = canonicalize_scope(scope)
        confirmed[canonical.casefold()] = canonical
    allowed = {**effective, **confirmed}
    if expected_scopes is None:
        expected = dict(effective)
    else:
        expected = {}
        for scope in expected_scopes:
            canonical = canonicalize_scope(scope)
            expected[canonical.casefold()] = canonical

    result_list = list(results or ())
    if not result_list:
        raise ValueError("at least one query_costs result is required")

    warnings = []
    truncated = []
    slices_by_scope = {}
    shapes = {}
    rows = []

    for index, record in enumerate(result_list):
        if not isinstance(record, dict):
            raise ValueError(f"query_costs record {index} must be an object")
        arguments = record.get("arguments") or record.get("request")
        if not isinstance(arguments, dict):
            raise ValueError(f"query_costs record {index} is missing its arguments")
        scope = canonicalize_scope(record.get("scope") or arguments.get("scope"))
        key = scope.casefold()
        if key not in allowed:
            raise ValueError(
                f"query_costs record {index} queried {scope}, which is not an effective "
                "managed scope"
            )
        scope = allowed[key]
        metric = _text(arguments.get("metric"))
        if metric.casefold() != USD_METRIC.casefold():
            raise ValueError(
                f"query_costs record {index} must use metric={USD_METRIC}, got {metric or 'default'}"
            )
        shape = _shape(arguments)
        if shapes.setdefault(key, shape) != shape:
            raise ValueError(
                f"query_costs records for {scope} mix query shapes; prepare each shape separately"
            )
        start = arguments.get("from")
        end = arguments.get("to")
        if not start or not end:
            raise ValueError(
                f"query_costs record {index} must use explicit from/to dates, not a timeframe"
            )
        start_date = _iso_date(start, "from")
        end_date = _iso_date(end, "to")
        if end_date < start_date:
            raise ValueError(f"query_costs record {index} has to before from")

        properties = _unwrap(record.get("result", record.get("output")), index)
        next_link = _text(properties.get("nextLink"))
        if next_link:
            truncated.append(
                {"scope": scope, "from": start_date.isoformat(), "to": end_date.isoformat()}
            )

        subscription, resource_group = _scope_parts(scope)
        slice_id = f"{start_date.isoformat()}..{end_date.isoformat()}"
        slices_by_scope.setdefault(key, []).append((start_date, end_date, index))
        for item in _rows(properties, index):
            currency = _text(item.get("currency"))
            if currency and currency.upper() != "USD":
                raise ValueError(
                    f"query_costs record {index} returned {currency}; re-query with metric={USD_METRIC}"
                )
            if subscription and not item.get("subscriptionId"):
                item["subscriptionId"] = subscription
            if resource_group and not item.get("resourceGroup"):
                item["resourceGroup"] = resource_group
            if not item.get("resourceGroup"):
                derived = _resource_group_from_resource_id(item.get("resourceId"))
                if derived:
                    item["resourceGroup"] = derived
            resource_type = _text(item.get("resourceType"))
            if resource_type and not item.get("consumedService"):
                item["consumedService"] = resource_type.split("/")[0]
            group_values = [
                f"{field}={item[field]}"
                for field in sorted(item, key=str.casefold)
                if field not in {"cost", "currency", "quantity"}
            ]
            item["id"] = "|".join([SOURCE, key, slice_id, *group_values]).casefold()
            item["queryScope"] = scope
            rows.append(item)

    gaps = []
    window_dates = None
    if window is not None:
        window_dates = (_iso_date(window[0], "window start"), _iso_date(window[1], "window end"))
    for key, slices in slices_by_scope.items():
        scope = allowed[key]
        slices.sort()
        for (prev_start, prev_end, prev_index), (next_start, next_end, next_index) in zip(
            slices, slices[1:]
        ):
            if next_start <= prev_end:
                raise ValueError(
                    f"query_costs records {prev_index} and {next_index} overlap for {scope}"
                )
            if next_start != prev_end + timedelta(days=1):
                gaps.append(
                    {
                        "scope": scope,
                        "from": (prev_end + timedelta(days=1)).isoformat(),
                        "to": (next_start - timedelta(days=1)).isoformat(),
                    }
                )
        if window_dates:
            if slices[0][0] > window_dates[0]:
                gaps.append(
                    {
                        "scope": scope,
                        "from": window_dates[0].isoformat(),
                        "to": (slices[0][0] - timedelta(days=1)).isoformat(),
                    }
                )
            if slices[-1][1] < window_dates[1]:
                gaps.append(
                    {
                        "scope": scope,
                        "from": (slices[-1][1] + timedelta(days=1)).isoformat(),
                        "to": window_dates[1].isoformat(),
                    }
                )

    missing = sorted(
        (scope for key, scope in expected.items() if key not in slices_by_scope),
        key=str.casefold,
    )

    problems = []
    for item in truncated:
        problems.append(
            f"{item['scope']} {item['from']}..{item['to']} was truncated at the top row "
            "limit (nextLink present); split the date range or reduce groupBy"
        )
    for item in gaps:
        problems.append(f"{item['scope']} has no cost data for {item['from']}..{item['to']}")
    for scope in missing:
        problems.append(f"{scope} was not queried")
    if problems and require_complete:
        raise ValueError("incomplete query_costs results: " + "; ".join(problems))
    warnings.extend(problems)

    filter_scopes = list(managed_scopes or ()) + list(confirmed.values())
    coverage = filter_usage_details(
        rows,
        filter_scopes,
        management_group_expansions=management_group_expansions,
    )

    partial_scopes = {item["scope"].casefold() for item in truncated + gaps}
    scope_coverage = []
    for key in sorted({*slices_by_scope, *(scope.casefold() for scope in missing)}):
        scope = allowed.get(key) or expected.get(key)
        tallies = {}
        for bucket in ("included_rows", "excluded_rows", "unattributed_rows"):
            matching = [row for row in coverage[bucket] if row["queryScope"].casefold() == key]
            tallies[bucket] = matching
        cost = sum(
            (Decimal(str(row.get("cost") or 0)) for row in tallies["included_rows"]),
            Decimal("0"),
        )
        is_missing = key not in slices_by_scope
        is_partial = is_missing or key in partial_scopes
        scope_coverage.append(
            {
                "scope": scope,
                "status": "missing" if is_missing else ("partial" if is_partial else "complete"),
                "included": len(tallies["included_rows"]),
                "excluded": len(tallies["excluded_rows"]),
                "unattributed": len(tallies["unattributed_rows"]),
                "cost_usd": cost,
                "partial": is_partial,
                "detail": SOURCE,
            }
        )

    return {
        **coverage,
        "source": SOURCE,
        "currency": "USD",
        "metric": USD_METRIC,
        "result_count": len(result_list),
        "retrieved_row_count": len(rows),
        "scopes_queried": sorted(
            (allowed[key] for key in slices_by_scope), key=str.casefold
        ),
        "missing_scopes": missing,
        "truncated_slices": truncated,
        "date_gaps": gaps,
        "partial": bool(problems),
        "warnings": warnings,
        "scope_coverage": scope_coverage,
    }


def sum_cost_by(rows, field):
    """Sum ``cost`` per value of ``field`` (case-insensitive) with exact decimals.

    Returns ``(totals, unkeyed)`` where ``totals`` maps the first-seen spelling of each
    value to a float total and ``unkeyed`` is the float cost of rows with no value.
    """

    totals = {}
    names = {}
    unkeyed = Decimal("0")
    for row in rows:
        cost = Decimal(str(row.get("cost") or 0))
        value = _text(row.get(field))
        if not value:
            unkeyed += cost
            continue
        key = value.casefold()
        names.setdefault(key, value)
        totals[key] = totals.get(key, Decimal("0")) + cost
    return {names[key]: float(amount) for key, amount in totals.items()}, float(unkeyed)


def summarize_cost_overview(prepared, *, top=15):
    """Aggregate prepared daily rows into the ``build_cost_overview_model`` input.

    ``prepared`` is the ``prepare_cost_query`` result of a Daily query grouped by
    ``ResourceGroupName,ServiceName``. Totals use exact decimal arithmetic.
    """

    daily = {}
    services = {}
    groups = {}
    total = Decimal("0")
    for row in prepared["included_rows"]:
        cost = Decimal(str(row.get("cost") or 0))
        total += cost
        day = _text(row.get("date"))
        if day:
            daily[day] = daily.get(day, Decimal("0")) + cost
        service = _text(row.get("serviceName")) or "(unknown)"
        services[service] = services.get(service, Decimal("0")) + cost
        group = _text(row.get("resourceGroup"))
        group_key = group.casefold()
        name, amount = groups.get(group_key, (group, Decimal("0")))
        groups[group_key] = (name, amount + cost)

    def ranked(items):
        ordered = sorted(items, key=lambda item: (-item[1], item[0].casefold(), item[0]))
        return ordered[:top]

    return {
        "total_usd": total,
        "daily": [{"date": day, "cost_usd": cost} for day, cost in sorted(daily.items())],
        "by_service": [
            {"service": name, "cost_usd": cost} for name, cost in ranked(services.items())
        ],
        "by_resource_group": [
            {"resource_group": name, "cost_usd": cost} for name, cost in ranked(groups.values())
        ],
        "partial": prepared.get("partial", False),
        "warnings": list(prepared.get("warnings") or []),
        "scope_coverage": list(prepared.get("scope_coverage") or []),
    }
