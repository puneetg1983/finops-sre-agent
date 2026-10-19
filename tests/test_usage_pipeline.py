"""Tests for deterministic UsageDetails page-chain preparation."""

import importlib.util
import sys
from pathlib import Path

import pytest


SKILL = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "azure-finops"
    / "skills"
    / "finops-managed-scope"
)
sys.path.insert(0, str(SKILL))
SPEC = importlib.util.spec_from_file_location("finops_usage", SKILL / "usage.py")
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)

RG = "/subscriptions/sub/resourceGroups/managed"


def _row(row_id, resource_group, cost):
    return {
        "id": row_id,
        "properties": {
            "instanceName": (
                f"/subscriptions/sub/resourceGroups/{resource_group}"
                "/providers/Microsoft.Storage/storageAccounts/account"
            ),
            "costInUSD": cost,
        },
    }


def test_prepares_complete_chain_and_filters_after_merge():
    second = "https://management.azure.com/usage?skip=next&top=2"
    result = usage.prepare_usage_details(
        [
            {
                "requestUrl": "https://management.azure.com/usage?top=2",
                "value": [
                    _row("one", "managed", "1.25"),
                    _row("two", "outside", "8.75"),
                ],
                "nextLink": second.replace("&", "&amp;"),
            },
            {
                "request_url": second,
                "rows": [
                    _row("three", "MANAGED", "2.50"),
                    _row("one", "managed", "1.25"),
                ],
                "next_link": None,
            },
        ],
        [RG],
    )

    assert result["chain_complete"] is True
    assert result["partial"] is False
    assert result["page_count"] == 2
    assert result["page_row_counts"] == [2, 2]
    assert result["retrieved_row_count"] == 4
    assert result["included_count"] == 2
    assert result["excluded_count"] == 1
    assert result["duplicate_count"] == 1
    assert str(result["duplicate_cost"]) == "1.25"
    assert str(result["included_cost"]) == "3.75"
    assert str(result["excluded_cost"]) == "8.75"


def test_rejects_pages_from_a_restarted_chain():
    with pytest.raises(ValueError, match="does not continue"):
        usage.prepare_usage_details(
            [
                {
                    "request_url": "https://management.azure.com/usage?top=1",
                    "value": [_row("one", "managed", "1")],
                    "nextLink": "https://management.azure.com/usage?skip=next",
                },
                {
                    "request_url": "https://management.azure.com/usage?top=1",
                    "value": [_row("two", "managed", "2")],
                    "nextLink": None,
                },
            ],
            [RG],
        )


def test_incomplete_chain_fails_closed_by_default():
    pages = [
        {
            "request_url": "https://management.azure.com/usage?top=1",
            "value": [_row("one", "managed", "1")],
            "nextLink": "https://management.azure.com/usage?skip=next",
        }
    ]
    with pytest.raises(ValueError, match="incomplete"):
        usage.prepare_usage_details(pages, [RG])

    partial = usage.prepare_usage_details(pages, [RG], require_complete=False)
    assert partial["chain_complete"] is False
    assert partial["partial"] is True
    assert partial["remaining_next_link"].endswith("skip=next")
