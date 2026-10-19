"""Deterministic preparation of projected Consumption UsageDetails pages."""

from html import unescape

from scope import filter_usage_details


def _text(value):
    return str(value or "").strip()


def _page_field(page, *names):
    for name in names:
        if name in page:
            return page[name]
    return None


def prepare_usage_details(
    pages,
    managed_scopes,
    management_group_expansions=None,
    *,
    require_complete=True,
):
    """Validate one continuation chain, merge its rows, and apply managed scope.

    Each page is an object with:
      request_url / requestUrl  the URL used for that page;
      value / rows              the projected UsageDetails rows;
      nextLink / next_link      the continuation URL returned by Azure.

    The first page starts the chain. Every later request URL must exactly match the
    preceding decoded nextLink. Independently restarted page sequences cannot be
    mixed accidentally.
    """

    page_list = list(pages or ())
    if not page_list:
        raise ValueError("at least one UsageDetails page is required")

    merged_rows = []
    request_urls = []
    page_row_counts = []
    expected_request_url = None

    for index, page in enumerate(page_list):
        if not isinstance(page, dict):
            raise ValueError(f"UsageDetails page {index} must be an object")

        request_url = _text(_page_field(page, "request_url", "requestUrl"))
        if not request_url:
            raise ValueError(f"UsageDetails page {index} is missing request_url")
        request_url = unescape(request_url)

        if expected_request_url is not None and request_url != expected_request_url:
            raise ValueError(
                f"UsageDetails page {index} does not continue the preceding nextLink"
            )
        if request_url in request_urls:
            raise ValueError(f"UsageDetails page {index} repeats a request URL")

        rows = _page_field(page, "value", "rows")
        if not isinstance(rows, list):
            raise ValueError(f"UsageDetails page {index} value must be an array")

        next_link = _text(_page_field(page, "nextLink", "next_link"))
        expected_request_url = unescape(next_link) if next_link else None
        request_urls.append(request_url)
        page_row_counts.append(len(rows))
        merged_rows.extend(rows)

    chain_complete = expected_request_url is None
    if require_complete and not chain_complete:
        raise ValueError("UsageDetails page chain is incomplete; final nextLink was not fetched")

    coverage = filter_usage_details(
        merged_rows,
        managed_scopes,
        management_group_expansions=management_group_expansions,
    )
    return {
        **coverage,
        "page_count": len(page_list),
        "page_row_counts": page_row_counts,
        "retrieved_row_count": len(merged_rows),
        "request_urls": request_urls,
        "chain_complete": chain_complete,
        "remaining_next_link": expected_request_url,
        "partial": not chain_complete,
    }
