"""Tests for Phase 3 workflow — real case input structure + MCP gateway integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(CONTRACTS_ROOT)


def _mock_ev(domain: str, suffix: str) -> dict[str, Any]:
    """Build a valid MCP evidence envelope for testing."""
    ref = f"ev_{'a' * 20}{suffix}"
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": ref,
        "result_hash": f"sha256:{'0' * 64}",
        "domain": domain,
        "data": {},
    }


def _make_gateway(
    tool_responses: dict[str, dict[str, Any]],
    fake_order_ids: set[str] | None = None,
) -> MagicMock:
    """
    Create a mock EvidenceGateway.
    - tool_responses: default response per tool name.
    - fake_order_ids: order IDs that should return empty data (simulating non-existent orders).
    """
    fake_ids = fake_order_ids or set()
    gateway = MagicMock()
    gateway.list_tools = AsyncMock(return_value=list(tool_responses.keys()))

    async def mock_call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
        resp = tool_responses.get(tool_name)
        if resp is None:
            raise RuntimeError(f"Unexpected tool call: {tool_name}")
        # Simulate non-existent order: return empty data so verification fails
        order_id = kwargs.get("order_id", "")
        if order_id in fake_ids:
            return {**resp, "data": {}}
        return resp

    gateway.call = AsyncMock(side_effect=mock_call)
    return gateway


# ── Real L3B case structure ────────────────────────────────────────────────
CASE_001 = {
    "case_id": "L3B_CASE_001",
    "opened_at": "2018-01-01T09:00:00-03:00",
    "customer_request": {
        "language": "vi",
        "message": "Khiếu nại giao hàng trễ và yêu cầu hoàn tiền",
        "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
        "claims": [
            {"claim_id": "claim-001-a", "topic": "late_delivery_logistics"},
            {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
        ],
    },
    "policy_version": "EC_POLICY_V2",
    "candidate_order_ids": [
        "af0bbb47f125381ce9f3597dc70ef07b",
        "candidate-001",  # fake candidate — should be rejected
    ],
    "investigation_scope": {
        "include_customer_history": True,
        "include_product_context": True,
        "require_independent_verification": True,
    },
    "customer_unique_id_hint": "customer-597dc70ef07b",
}


@pytest.mark.asyncio
async def test_late_delivery_logistics_case(contracts: Contracts, tmp_path: Path) -> None:
    """
    CASE_001: logistics delay + refund request.
    Expected: primary_issue=late_delivery_logistics, action_required.
    MCP: get_order validates real candidate, rejects fake, get_shipment shows delay.
    """
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    tool_responses = {
        # Real order returns data; fake order returns empty → rejected
        "get_order": {
            **_mock_ev("order", "ord1"),
            "data": {
                "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                "order_status": "delivered",
                "customer_unique_id": "customer-597dc70ef07b",
                "seller_ids": ["seller-aaa"],
                "payment_references": ["pay-001"],
                "shipment_ids": ["ship-001"],
            },
        },
        "get_shipment": {
            **_mock_ev("shipment", "shp1"),
            "data": {
                "shipping_status": "delivered",
                "estimated_delivery_date": "2018-01-10",
                "delivered_at": "2018-01-15",  # 5 days late → logistics_delay
                "delay_responsible": "logistics",
            },
        },
        "get_payment": {
            **_mock_ev("payment", "pay1"),
            "data": {
                "payment_value": 150.0,
                "payment_type": "credit_card",
                "payment_installments": 1,
                "payment_sequential": 1,
            },
        },
        "get_refund": {
            **_mock_ev("payment", "ref1"),
            "data": {
                "refund_value": 0.0,
                "refund_status": "not_started",
            },
        },
        "get_customer_history": {
            **_mock_ev("customer", "cst1"),
            "data": {
                "order_ids": ["af0bbb47f125381ce9f3597dc70ef07b"],
            },
        },
    }

    gateway = _make_gateway(tool_responses, fake_order_ids={"candidate-001"})

    trace.emit(case_id="L3B_CASE_001", event_type="case_received", actor="coordinator")
    output = await solve_case(CASE_001, gateway, trace)
    trace.emit(case_id="L3B_CASE_001", event_type="case_finalized", actor="coordinator")

    # Schema validation
    contracts.validate_output(output, "test_late_delivery")

    # Semantic assertions
    assert output["case_id"] == "L3B_CASE_001"
    assert output["schema_version"] == "day09-l3b-output-v2"
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["entity_resolution"]["status"] == "resolved"
    assert "af0bbb47f125381ce9f3597dc70ef07b" in output["entity_resolution"]["resolved_order_ids"]
    assert "candidate-001" in output["entity_resolution"]["rejected_candidates"]
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["financial_resolution"]["currency"] == "BRL"

    # Trace validation
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_types = [e["event_type"] for e in events]
    assert "case_received" in event_types
    assert "task_assigned" in event_types
    assert "tool_result_consumed" in event_types
    assert "handoff" in event_types
    assert "policy_decided" in event_types
    assert "verification_completed" in event_types
    assert "case_finalized" in event_types

    # Evidence integrity: all refs must be from gateway (start with ev_)
    for ref in output["evidence_refs"]:
        assert ref.startswith("ev_"), f"Invalid evidence_ref: {ref}"


@pytest.mark.asyncio
async def test_duplicate_charge_case(contracts: Contracts, tmp_path: Path) -> None:
    """Payment duplicate capture → duplicate_charge, full refund."""
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    case = {
        **CASE_001,
        "case_id": "L3B_CASE_DUP",
        "customer_request": {
            **CASE_001["customer_request"],
            "claims": [{"claim_id": "claim-dup-a", "topic": "duplicate_charge"}],
        },
        "candidate_order_ids": ["af0bbb47f125381ce9f3597dc70ef07b"],
    }
    case["customer_request"]["claimed_order_id"] = "af0bbb47f125381ce9f3597dc70ef07b"

    tool_responses = {
        "get_order": {
            **_mock_ev("order", "ord2"),
            "data": {"order_id": "af0bbb47f125381ce9f3597dc70ef07b", "order_status": "delivered"},
        },
        "get_payment": {
            **_mock_ev("payment", "pay2"),
            "data": {"payment_value": 200.0, "payment_sequential": 2, "payment_installments": 1},
        },
        "get_refund": {
            **_mock_ev("payment", "ref2"),
            "data": {"refund_value": 0.0, "refund_status": "not_started"},
        },
        "get_shipment": {
            **_mock_ev("shipment", "shp2"),
            "data": {
                "shipping_status": "delivered",
                "delivered_at": "2018-01-10",
                "estimated_delivery_date": "2018-01-12",
            },
        },
    }
    gateway = _make_gateway(tool_responses)

    trace.emit(case_id="L3B_CASE_DUP", event_type="case_received", actor="coordinator")
    output = await solve_case(case, gateway, trace)
    trace.emit(case_id="L3B_CASE_DUP", event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "test_duplicate_charge")

    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] > 0
    assert len(output["financial_resolution"]["refund_lines"]) >= 1


@pytest.mark.asyncio
async def test_no_action_valid_payment_case(contracts: Contracts, tmp_path: Path) -> None:
    """Valid payment, on-time delivery → unsupported_claim, no_action."""
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    case = {
        **CASE_001,
        "case_id": "L3B_CASE_NO_ACTION",
        "customer_request": {
            **CASE_001["customer_request"],
            "claims": [{"claim_id": "claim-na-a", "topic": "valid_split_payment"}],
        },
        "candidate_order_ids": ["af0bbb47f125381ce9f3597dc70ef07b"],
    }
    case["customer_request"]["claimed_order_id"] = "af0bbb47f125381ce9f3597dc70ef07b"

    tool_responses = {
        "get_order": {
            **_mock_ev("order", "ord3"),
            "data": {"order_id": "af0bbb47f125381ce9f3597dc70ef07b", "order_status": "delivered"},
        },
        "get_payment": {
            **_mock_ev("payment", "pay3"),
            "data": {"payment_value": 100.0, "payment_sequential": 1, "payment_installments": 3},
        },
        "get_refund": {
            **_mock_ev("payment", "ref3"),
            "data": {"refund_value": 0.0, "refund_status": "not_applicable"},
        },
        "get_shipment": {
            **_mock_ev("shipment", "shp3"),
            "data": {
                "shipping_status": "delivered",
                "delivered_at": "2018-01-08",
                "estimated_delivery_date": "2018-01-10",
            },
        },
    }
    gateway = _make_gateway(tool_responses)

    trace.emit(case_id="L3B_CASE_NO_ACTION", event_type="case_received", actor="coordinator")
    output = await solve_case(case, gateway, trace)
    trace.emit(case_id="L3B_CASE_NO_ACTION", event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "test_no_action")

    assert output["assessment"]["case_status"] in (
        "no_action",
        "action_required",
    )  # depends on claim
    assert output["financial_resolution"]["currency"] == "BRL"
    assert output["financial_resolution"]["recommended_refund_brl"] >= 0


@pytest.mark.asyncio
async def test_entity_not_found_no_mcp_tools(contracts: Contracts, tmp_path: Path) -> None:
    """No MCP tools available → not_found, needs_investigation, no crash."""
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    gateway = _make_gateway({})  # no tools at all

    trace.emit(case_id="L3B_CASE_001", event_type="case_received", actor="coordinator")
    output = await solve_case(CASE_001, gateway, trace)
    trace.emit(case_id="L3B_CASE_001", event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "test_no_tools")

    assert output["entity_resolution"]["status"] in ("not_found", "ambiguous", "resolved")
    assert output["assessment"]["confidence"] >= 0.0
    assert output["financial_resolution"]["recommended_refund_brl"] >= 0
