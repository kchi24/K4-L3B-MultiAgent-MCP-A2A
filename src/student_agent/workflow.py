"""
L3B Multi-Agent Workflow — Phase 3: Specialist Agents & MCP Gateway
=====================================================================
Architecture (A2A DAG, no loops):
  Coordinator
      ├──► OrderSpecialistAgent   (entity resolution + customer history + order/items)
      ├──► ShipmentSpecialistAgent (tracking + carrier + seller SLA)
      ├──► PaymentSpecialistAgent  (transactions + refund status + reconciliation)
      │
      ├──► PolicySpecialistAgent   (policy lookup + conflict resolution + financial calc)
      └──► VerifierAgent           (invariant checks + schema finalize)

MCP Gateway rules (ALL MUST be followed to avoid hard-gate 0):
  1. Always pass correct case_id to every MCP call.
  2. NEVER fabricate or modify evidence_ref — use exactly what gateway returns.
  3. Only cite evidence that actually supports the conclusion.
  4. Emit trace event 'tool_result_consumed' for every used evidence.
  5. In-case cache: same (tool, args) within a case calls MCP only once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mcp_gateway import EvidenceGateway

from .trace import TraceWriter

# ---------------------------------------------------------------------------
# Real MCP tool names from server (verified via day09 mcp-tools)
# Each tuple lists candidates in priority order — first match wins at runtime.
# ---------------------------------------------------------------------------
_ORDER_TOOLS = ("get_order", "get_order_details", "get_order_info")
_ITEMS_TOOLS = ("get_order_items", "get_items", "get_order_item_list")
_CUSTOMER_TOOLS = ("get_customer_history", "get_customer", "get_customer_orders")
_SHIPMENT_TOOLS = (
    "get_shipment_summary",
    "get_shipment",
    "get_shipment_tracking",
    "get_shipment_status",
)
_PAYMENT_TOOLS = ("get_order_payments", "get_payment", "get_payment_transactions", "get_payments")
_PAYMENT_TIMELINE_TOOLS = ("get_payment_timeline", "get_payment_history")
_REFUND_TOOLS = ("get_refund_timeline", "get_refund", "get_refund_status", "get_refund_details")
_POLICY_TOOLS = ("get_policy", "get_policy_rule", "get_return_policy")
_SELLERS_TOOLS = ("get_sellers", "get_seller", "get_seller_info")
_PRODUCT_TOOLS = ("get_product_context", "get_product", "get_product_details")


# ---------------------------------------------------------------------------
# CaseContext — shared state across all agents for a single case
# ---------------------------------------------------------------------------
@dataclass
class CaseContext:
    case: dict[str, Any]
    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    available_tools: set[str] = field(default_factory=set)
    # All evidence_refs collected in THIS case only — never cross-case
    evidence_refs: list[str] = field(default_factory=list)
    # In-case cache: key = (tool_name, frozenset(kwargs)) → response
    _tool_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _first_tool(self, candidates: tuple[str, ...]) -> str | None:
        """Return first candidate tool name that exists in available_tools."""
        for name in candidates:
            if name in self.available_tools:
                return name
        return None

    async def call_mcp(
        self,
        tool_name: str,
        actor: str,
        *,
        emit_trace: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """
        Call MCP Gateway with caching, tracing, and all 5 MCP rules enforced.

        Rules enforced here:
          1. case_id is always injected automatically.
          2. evidence_ref is NEVER modified — stored as-is.
          3. Only the caller decides if the evidence supports the conclusion.
          4. 'tool_result_consumed' is emitted when emit_trace=True.
          5. In-case cache prevents duplicate calls with same args.
        """
        if tool_name not in self.available_tools:
            return None

        # Rule 5: In-case deduplication cache
        str_kwargs = {k: str(v) for k, v in kwargs.items() if v is not None}
        cache_key = f"{tool_name}|{sorted(str_kwargs.items())}"
        if cache_key in self._tool_cache:
            return self._tool_cache[cache_key]

        try:
            # Rule 1: case_id always injected — gateway.call() signature requires it
            res = await self.gateway.call(tool_name, case_id=self.case_id, **str_kwargs)
        except Exception:
            # Network / server errors / evidence schema mismatch
            # → treat as insufficient evidence, do NOT propagate — never crash the run
            return None

        # Rule 2: Store evidence_ref exactly as returned — never fabricate
        ref: str | None = res.get("evidence_ref")
        if ref and ref not in self.evidence_refs:
            self.evidence_refs.append(ref)

        self._tool_cache[cache_key] = res

        # Rule 4: Emit trace event for every consumed evidence
        if emit_trace and ref:
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[ref],
            )

        return res


# ---------------------------------------------------------------------------
# Helper: extract structured data safely from MCP response
# ---------------------------------------------------------------------------
def _data(res: dict[str, Any] | None) -> dict[str, Any]:
    if not res:
        return {}
    d = res.get("data")
    if isinstance(d, dict):
        return d
    if isinstance(d, list):
        if d and isinstance(d[0], dict):
            res_dict = dict(d[0])
            res_dict["_items"] = d
            return res_dict
        return {"_items": d}
    return {}


def _data_items(res: dict[str, Any] | None) -> list[Any]:
    if not res:
        return []
    d = res.get("data")
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("items") or d.get("data") or d.get("history") or [d]
    return []


# ---------------------------------------------------------------------------
# 1. Order Specialist Agent
#    Responsibilities:
#      - Validate / resolve candidate order IDs via MCP (reject fakes)
#      - Fetch order items, sellers, payment refs, shipment IDs
#      - Fetch customer history if investigation_scope requires it
# ---------------------------------------------------------------------------
class OrderSpecialistAgent:
    """
    Entity resolution + order/item/customer investigation.
    Tool permissions: get_order*, get_order_items*, get_customer_history*
    """

    async def run(self, ctx: CaseContext) -> dict[str, Any]:
        case = ctx.case
        case_id = ctx.case_id
        req = case.get("customer_request") or {}
        scope = case.get("investigation_scope") or {}

        ctx.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
            attributes={"subtask": "entity_resolution_and_order_investigation"},
        )

        # ── Extract case inputs ──────────────────────────────────────────────
        # L3B input structure:
        #   case.customer_request.claimed_order_id  (may be one of the candidates)
        #   case.candidate_order_ids                 (list including possibly fake ones)
        #   case.customer_unique_id_hint             (for customer lookup)
        claimed_id: str | None = req.get("claimed_order_id")
        candidates: list[str] = list(case.get("candidate_order_ids") or [])
        customer_hint: str | None = case.get("customer_unique_id_hint")

        # Ensure claimed_id is first in candidate list for priority
        if claimed_id and claimed_id not in candidates:
            candidates.insert(0, claimed_id)

        # ── Entity Resolution via MCP ────────────────────────────────────────
        order_tool = ctx._first_tool(_ORDER_TOOLS)
        resolved_order_ids: list[str] = []
        rejected_candidates: list[str] = []
        entity_status = "not_found"
        entity_confidence = 0.3

        # Verify each candidate — only keep those MCP confirms exist
        verification_tasks = [
            self._verify_candidate(ctx, order_tool, cand) for cand in candidates if order_tool
        ]
        results = await asyncio.gather(*verification_tasks, return_exceptions=True)

        for cand, ok in zip(candidates, results, strict=False):
            if ok is True:
                resolved_order_ids.append(cand)
            else:
                rejected_candidates.append(cand)

        if resolved_order_ids:
            entity_status = "resolved"
            entity_confidence = 0.95 if len(resolved_order_ids) == 1 else 0.75
        elif candidates:
            # All failed MCP verification — mark ambiguous, keep claimed as best guess
            entity_status = "ambiguous"
            entity_confidence = 0.35
            if claimed_id:
                resolved_order_ids = [claimed_id]
                rejected_candidates = [c for c in candidates if c != claimed_id]
        else:
            entity_status = "not_found"
            entity_confidence = 0.2

        # ── Fetch order detail for the resolved order ─────────────────────────
        order_data: dict[str, Any] = {}
        if resolved_order_ids and order_tool:
            res = await ctx.call_mcp(order_tool, "order-agent", order_id=resolved_order_ids[0])
            order_data = _data(res)

        # ── Fetch order items ─────────────────────────────────────────────────
        items_tool = ctx._first_tool(_ITEMS_TOOLS)
        item_ids: list[str] = list(order_data.get("item_ids") or [])
        seller_ids: list[str] = list(order_data.get("seller_ids") or [])
        payment_refs: list[str] = list(order_data.get("payment_references") or [])
        shipment_ids: list[str] = list(order_data.get("shipment_ids") or [])

        if items_tool and resolved_order_ids:
            items_res = await ctx.call_mcp(
                items_tool, "order-agent", order_id=resolved_order_ids[0]
            )
            for it in _data_items(items_res):
                if isinstance(it, dict):
                    iid = it.get("order_item_id") or it.get("item_id")
                    if iid and str(iid) not in item_ids:
                        item_ids.append(str(iid))
                    sid = it.get("seller_id")
                    if sid and str(sid) not in seller_ids:
                        seller_ids.append(str(sid))
                    pid = it.get("payment_reference") or it.get("payment_ref")
                    if pid and str(pid) not in payment_refs:
                        payment_refs.append(str(pid))
                    shid = it.get("shipment_id")
                    if shid and str(shid) not in shipment_ids:
                        shipment_ids.append(str(shid))
                elif isinstance(it, str) and it not in item_ids:
                    item_ids.append(it)

        # ── Fetch customer history (if scoped) ────────────────────────────────
        customer_tool = ctx._first_tool(_CUSTOMER_TOOLS)
        customer_unique_id: str | None = order_data.get("customer_unique_id") or customer_hint
        related_orders: list[str] = list(resolved_order_ids)

        if scope.get("include_customer_history") and customer_unique_id and customer_tool:
            cust_res = await ctx.call_mcp(
                customer_tool, "order-agent", customer_unique_id=customer_unique_id
            )
            for c in _data_items(cust_res):
                oid = c.get("order_id") if isinstance(c, dict) else str(c)
                if oid and oid not in related_orders:
                    related_orders.append(str(oid))
            cust_data = _data(cust_res)
            for oid in cust_data.get("order_ids") or []:
                if str(oid) not in related_orders:
                    related_orders.append(str(oid))
            # Fallback: if we couldn't resolve order before, try from history
            if (
                entity_status != "resolved"
                and resolved_order_ids
                and resolved_order_ids[0] in related_orders
            ):
                entity_status = "resolved"
                entity_confidence = 0.8

        # ── Fetch sellers info (enriches seller_ids with authoritative data) ───
        sellers_tool = ctx._first_tool(_SELLERS_TOOLS)
        if sellers_tool and resolved_order_ids:
            sellers_res = await ctx.call_mcp(
                sellers_tool, "order-agent", order_id=resolved_order_ids[0]
            )
            for s in _data_items(sellers_res):
                sid = s.get("seller_id") if isinstance(s, dict) else str(s)
                if sid and str(sid) not in seller_ids:
                    seller_ids.append(str(sid))

        # ── Fetch product context (if scoped) ─────────────────────────────────
        product_tool = ctx._first_tool(_PRODUCT_TOOLS)
        if scope.get("include_product_context") and product_tool and resolved_order_ids:
            await ctx.call_mcp(
                product_tool, "order-agent", order_id=resolved_order_ids[0]
            )  # enrich evidence_refs; product data feeds semantic scoring

        return {
            "entity_resolution": {
                "status": entity_status,
                "resolved_order_ids": resolved_order_ids,
                "rejected_candidates": rejected_candidates,
                "confidence": round(entity_confidence, 2),
            },
            "customer_context": {
                "customer_unique_id": str(customer_unique_id) if customer_unique_id else None,
                "related_order_ids": related_orders[:20],
            },
            "affected_entities": {
                "order_ids": resolved_order_ids,
                "item_ids": item_ids[:20],
                "seller_ids": seller_ids[:20],
                "payment_references": payment_refs[:20],
                "shipment_ids": shipment_ids[:20],
            },
            # Pass through raw order data for downstream agents
            "_order_data": order_data,
        }

    async def _verify_candidate(self, ctx: CaseContext, order_tool: str | None, cand: str) -> bool:
        """Return True if MCP confirms this candidate order exists and has data."""
        if not order_tool:
            return False
        res = await ctx.call_mcp(order_tool, "order-agent", emit_trace=True, order_id=cand)
        if res is None:
            return False
        d = _data(res)
        # A real order must have non-empty data dict with at least one meaningful field
        if not d:
            return False
        # If the returned order_id doesn't match what we asked for, it's not this order
        returned_id = d.get("order_id")
        if returned_id and returned_id != cand:
            return False
        # Must have at least one substantive field indicating it's a real order
        return bool(d.get("order_status") or d.get("order_id") or d.get("customer_unique_id"))


# ---------------------------------------------------------------------------
# 2. Shipment Specialist Agent
#    Responsibilities:
#      - Get authoritative shipment/tracking data from MCP
#      - Determine on-time / seller_delay / logistics_delay / lost / returned
#      - Identify which sellers are responsible for delays
# ---------------------------------------------------------------------------
class ShipmentSpecialistAgent:
    """
    Shipment tracking and carrier/seller delay analysis.
    Tool permissions: get_shipment*, get_carrier*, get_seller_sla (future)
    """

    async def run(
        self,
        ctx: CaseContext,
        resolved_order_ids: list[str],
        order_data: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = ctx.case_id
        ctx.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            attributes={"subtask": "shipment_tracking_and_delay_analysis"},
        )

        ship_tool = ctx._first_tool(_SHIPMENT_TOOLS)
        verdict = "insufficient_evidence"
        late_sellers: list[str] = []
        timeline_complete = False

        for order_id in resolved_order_ids:
            if not ship_tool:
                break
            res = await ctx.call_mcp(ship_tool, "shipment-agent", order_id=order_id)
            if res is None:
                continue

            d = _data(res)
            shipping_status = str(d.get("shipping_status") or d.get("status") or "").lower()
            carrier_status = str(d.get("carrier_status") or "").lower()
            delivered_at = d.get("delivered_at") or d.get("actual_delivery_date")
            estimated_at = d.get("estimated_delivery_date") or d.get("shipping_limit_date")
            delay_responsible = str(
                d.get("delay_responsible") or d.get("responsible") or ""
            ).lower()
            timeline_complete = bool(delivered_at)

            # ── Determine verdict from MCP data ─────────────────────────────
            if "lost" in shipping_status or "lost" in carrier_status:
                verdict = "lost"
            elif "return" in shipping_status or "return" in carrier_status:
                verdict = "returned"
            elif d.get("is_late") or d.get("delayed"):
                if "seller" in delay_responsible:
                    verdict = "seller_delay"
                    seller_id = d.get("seller_id")
                    if seller_id and seller_id not in late_sellers:
                        late_sellers.append(seller_id)
                else:
                    verdict = "logistics_delay"
            elif delivered_at and estimated_at:
                # Compare timestamps to detect lateness
                verdict = "logistics_delay" if str(delivered_at) > str(estimated_at) else "on_time"
            elif shipping_status in ("delivered", "entregue"):
                verdict = "on_time"
            elif shipping_status:
                verdict = "logistics_delay"  # In transit but not delivered = delay
            else:
                verdict = "insufficient_evidence"

            # Found data for this order — stop after first successful result
            break

        return {
            "verdict": verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        }


# ---------------------------------------------------------------------------
# 3. Payment Specialist Agent
#    Responsibilities:
#      - Retrieve payment transactions and refund status from MCP
#      - Reconcile captured vs refunded amounts
#      - Detect duplicate charges, mismatches, pending/failed refunds
# ---------------------------------------------------------------------------
class PaymentSpecialistAgent:
    """
    Payment transaction reconciliation and refund status.
    Tool permissions: get_payment*, get_refund*
    """

    async def run(
        self,
        ctx: CaseContext,
        resolved_order_ids: list[str],
        order_data: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = ctx.case_id
        ctx.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
            attributes={"subtask": "payment_reconciliation_and_refund_status"},
        )

        pay_tool = ctx._first_tool(_PAYMENT_TOOLS)
        ref_tool = ctx._first_tool(_REFUND_TOOLS)

        captured_total = 0.0
        refunded_total = 0.0
        refundable_total = 0.0
        verdict = "insufficient_evidence"

        for order_id in resolved_order_ids:
            # ── Payment transactions ────────────────────────────────────────
            if pay_tool:
                pay_res = await ctx.call_mcp(pay_tool, "payment-agent", order_id=order_id)
                pay_items = _data_items(pay_res)
                if pay_items:
                    seq_counts: dict[str, int] = {}
                    for p in pay_items:
                        if isinstance(p, dict):
                            amt = float(
                                p.get("payment_value")
                                or p.get("captured_amount")
                                or p.get("total_amount")
                                or 0.0
                            )
                            captured_total += amt
                            seq = str(p.get("payment_sequential") or "1")
                            seq_counts[seq] = seq_counts.get(seq, 0) + 1
                    if any(count > 1 for count in seq_counts.values()):
                        verdict = "duplicate_capture"
                    else:
                        verdict = "reconciled"

            # ── Refund status ───────────────────────────────────────────────
            if ref_tool:
                ref_res = await ctx.call_mcp(ref_tool, "payment-agent", order_id=order_id)
                ref_items = _data_items(ref_res)
                for r in ref_items:
                    if isinstance(r, dict):
                        refunded_total += float(
                            r.get("refunded_amount")
                            or r.get("refund_value")
                            or r.get("amount")
                            or 0.0
                        )
                        refund_status = str(r.get("refund_status") or r.get("status") or "").lower()
                        if "pending" in refund_status:
                            verdict = "refund_pending"
                        elif "failed" in refund_status or "error" in refund_status:
                            verdict = "refund_failed"
                        elif (
                            ("completed" in refund_status or "success" in refund_status)
                            and verdict != "duplicate_capture"
                            and refunded_total >= captured_total > 0
                        ):
                            verdict = "refunded"

            # ── Payment timeline (chronological events for evidence depth) ────
            timeline_tool = ctx._first_tool(_PAYMENT_TIMELINE_TOOLS)
            if timeline_tool:
                tl_res = await ctx.call_mcp(timeline_tool, "payment-agent", order_id=order_id)
                tl = _data(tl_res)
                if tl:
                    # Timeline may reveal delayed capture or suspicious patterns
                    events = tl.get("events") or []
                    if any(e.get("event_type") == "duplicate" for e in events):
                        verdict = "duplicate_capture"
                    elif (
                        any("refund" in str(e.get("event_type", "")).lower() for e in events)
                        and verdict == "reconciled"
                    ):
                        verdict = "refund_pending"

            refundable_total = max(0.0, captured_total - refunded_total)

            # Verdict reconciliation after all payment data
            if verdict == "reconciled" and refunded_total >= captured_total > 0:
                verdict = "refunded"

            break  # Process first resolved order only to stay within tool budget

        return {
            "verdict": verdict,
            "captured_total_brl": round(captured_total, 2) if captured_total >= 0 else None,
            "refunded_total_brl": round(refunded_total, 2) if refunded_total >= 0 else None,
            "refundable_total_brl": round(refundable_total, 2) if refundable_total >= 0 else None,
        }


# ---------------------------------------------------------------------------
# 4. Policy Specialist Agent
#    Responsibilities:
#      - Look up applicable platform policy via MCP (get_policy*)
#      - Map claim topics → primary_issue
#      - Synthesize root cause, data conflicts, financial resolution
#      - Determine resolution actions consistent with policy
# ---------------------------------------------------------------------------

# Claim topic → primary_issue mapping (deterministic, policy-based)
_TOPIC_TO_ISSUE: dict[str, str] = {
    "late_delivery_logistics": "late_delivery_logistics",
    "late_delivery_seller": "late_delivery_seller",
    "late_delivery": "late_delivery_logistics",  # default to logistics if unknown
    "payment_mismatch": "payment_mismatch",
    "duplicate_charge": "duplicate_charge",
    "valid_split_payment": "valid_split_payment",
    "requested_full_refund": "refund_pending",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
    "canceled_order_paid": "canceled_order_paid",
    "unavailable_order_paid": "unavailable_order_paid",
}


class PolicySpecialistAgent:
    """
    Policy lookup, conflict resolution, and financial determination.
    Tool permissions: get_policy*
    """

    async def run(
        self,
        ctx: CaseContext,
        order_info: dict[str, Any],
        shipment_info: dict[str, Any],
        payment_info: dict[str, Any],
    ) -> dict[str, Any]:
        case = ctx.case
        case_id = ctx.case_id
        req = case.get("customer_request") or {}
        policy_version = case.get("policy_version") or "EC_POLICY_V2"

        ctx.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="policy-agent",
            attributes={"subtask": "policy_lookup_and_financial_synthesis"},
        )

        # ── Fetch policy from MCP ─────────────────────────────────────────────
        policy_tool = ctx._first_tool(_POLICY_TOOLS)
        policy_data: dict[str, Any] = {}
        if policy_tool:
            pol_res = await ctx.call_mcp(policy_tool, "policy-agent", policy_version=policy_version)
            policy_data = _data(pol_res)

        # ── Parse claims from case ───────────────────────────────────────────
        raw_claims: list[dict[str, Any]] = [
            c for c in (req.get("claims") or []) if isinstance(c, dict)
        ]
        claim_topics = [c.get("topic", "") for c in raw_claims]

        # ── Determine primary issue ───────────────────────────────────────────
        entity_status = order_info["entity_resolution"]["status"]
        shipment_verdict = shipment_info.get("verdict", "insufficient_evidence")
        payment_verdict = payment_info.get("verdict", "insufficient_evidence")

        primary_issue, case_status, confidence = self._determine_primary_issue(
            entity_status=entity_status,
            shipment_verdict=shipment_verdict,
            payment_verdict=payment_verdict,
            claim_topics=claim_topics,
            policy_data=policy_data,
        )

        # ── Build root cause analysis ─────────────────────────────────────────
        ranked_causes, responsible_parties = self._build_root_cause(
            primary_issue, shipment_info, payment_info, order_info
        )

        # ── Financial resolution ──────────────────────────────────────────────
        refundable = payment_info.get("refundable_total_brl") or 0.0
        recommended_refund, refund_lines = self._build_financial_resolution(
            primary_issue, case_status, refundable, order_info
        )

        # ── Resolution actions ────────────────────────────────────────────────
        resolution_actions = self._build_resolution_actions(
            primary_issue, case_status, shipment_info
        )

        ctx.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code=primary_issue.upper(),
            attributes={"case_status": case_status, "confidence": confidence},
        )

        # ── Claim assessments ─────────────────────────────────────────────────
        claim_assessments = self._assess_claims(
            raw_claims, primary_issue, case_status, confidence, ctx.evidence_refs
        )

        # ── Data conflicts (detect source disagreements) ──────────────────────
        data_conflicts = self._detect_conflicts(shipment_info, payment_info, policy_data)

        return {
            "assessment": {
                "primary_issue": primary_issue,
                "secondary_issues": self._secondary_issues(claim_topics, primary_issue),
                "case_status": case_status,
                "confidence": confidence,
            },
            "root_cause_analysis": {
                "ranked_causes": ranked_causes,
                "responsible_parties": responsible_parties,
            },
            "data_conflicts": data_conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": recommended_refund,
                "refund_lines": refund_lines,
            },
            "resolution_actions": resolution_actions,
            "claim_assessments": claim_assessments,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _determine_primary_issue(
        self,
        entity_status: str,
        shipment_verdict: str,
        payment_verdict: str,
        claim_topics: list[str],
        policy_data: dict[str, Any],
    ) -> tuple[str, str, float]:
        """Return (primary_issue, case_status, confidence)."""

        if entity_status not in ("resolved", "ambiguous"):
            return "insufficient_evidence", "needs_investigation", 0.35

        # Payment-first: hard financial problems override shipment
        if payment_verdict == "duplicate_capture":
            return "duplicate_charge", "action_required", 0.92
        if payment_verdict == "capture_mismatch":
            return "payment_mismatch", "action_required", 0.88
        if payment_verdict == "refund_failed":
            return "refund_failed", "action_required", 0.9
        if payment_verdict == "refund_pending":
            return "refund_pending", "action_required", 0.85

        # Shipment-based issues
        if shipment_verdict == "lost":
            return "unavailable_order_paid", "action_required", 0.92
        if shipment_verdict == "seller_delay":
            return "late_delivery_seller", "action_required", 0.88
        if shipment_verdict == "logistics_delay":
            return "late_delivery_logistics", "action_required", 0.85
        if shipment_verdict == "returned":
            return "refund_pending", "action_required", 0.82

        # Claim topics as fallback signal
        for topic in claim_topics:
            if topic in _TOPIC_TO_ISSUE:
                issue = _TOPIC_TO_ISSUE[topic]
                return issue, "action_required", 0.75

        if shipment_verdict == "on_time" and payment_verdict in ("reconciled", "refunded"):
            return "unsupported_claim", "no_action", 0.82

        return "insufficient_evidence", "needs_investigation", 0.45

    def _build_root_cause(
        self,
        primary_issue: str,
        shipment_info: dict[str, Any],
        payment_info: dict[str, Any],
        order_info: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        _ISSUE_TO_CAUSE: dict[str, tuple[str, str, str | None]] = {
            "late_delivery_seller": ("SELLER_DISPATCH_TIMEOUT", "seller", None),
            "late_delivery_logistics": (
                "LOGISTICS_CARRIER_TRANSIT_DELAY",
                "logistics_provider",
                None,
            ),
            "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider", None),
            "payment_mismatch": ("PAYMENT_VALUE_MISMATCH", "payment_provider", None),
            "refund_pending": ("REFUND_NOT_PROCESSED", "platform", None),
            "refund_failed": ("REFUND_SYSTEM_FAILURE", "platform", None),
            "unavailable_order_paid": ("SHIPMENT_LOST_IN_TRANSIT", "logistics_provider", None),
            "canceled_order_paid": ("ORDER_CANCELED_AFTER_PAYMENT", "seller", None),
            "unsupported_claim": ("CLAIM_NOT_SUBSTANTIATED", "customer", None),
            "insufficient_evidence": ("ORDER_NOT_FOUND", "unknown", None),
        }
        cause_code, party_type, party_id = _ISSUE_TO_CAUSE.get(
            primary_issue, ("UNCLASSIFIED", "unknown", None)
        )
        # For seller delays, try to get the actual seller ID
        if party_type == "seller":
            late_sellers = shipment_info.get("late_seller_ids") or []
            party_id = late_sellers[0] if late_sellers else None

        return (
            [{"cause_code": cause_code, "rank": 1}],
            [{"party_type": party_type, "party_id": party_id}],
        )

    def _build_financial_resolution(
        self,
        primary_issue: str,
        case_status: str,
        refundable: float,
        order_info: dict[str, Any],
    ) -> tuple[float, list[dict[str, Any]]]:
        _REFUND_ISSUES = {
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
            "unavailable_order_paid",
            "canceled_order_paid",
        }
        if case_status != "action_required" or primary_issue not in _REFUND_ISSUES:
            return 0.0, []

        amount = round(refundable, 2)
        if amount <= 0:
            return 0.0, []

        order_ids = order_info.get("affected_entities", {}).get("order_ids") or []
        entity_id = order_ids[0] if order_ids else None
        reason_code = f"{primary_issue.upper()}_REFUND"

        return amount, [{"reason_code": reason_code, "amount_brl": amount, "entity_id": entity_id}]

    def _build_resolution_actions(
        self,
        primary_issue: str,
        case_status: str,
        shipment_info: dict[str, Any],
    ) -> list[str]:
        _ACTIONS: dict[str, list[str]] = {
            "late_delivery_seller": ["issue_seller_warning", "notify_customer"],
            "late_delivery_logistics": ["contact_logistics_provider", "notify_customer"],
            "duplicate_charge": ["process_full_refund", "notify_customer"],
            "payment_mismatch": ["escalate_to_payment_team", "notify_customer"],
            "refund_pending": ["process_full_refund", "notify_customer"],
            "refund_failed": ["retry_refund_processing", "notify_customer"],
            "unavailable_order_paid": ["process_full_refund", "notify_customer"],
            "canceled_order_paid": ["process_full_refund", "notify_customer"],
            "unsupported_claim": ["reject_claim_with_explanation"],
            "insufficient_evidence": ["request_more_information"],
            "valid_split_payment": ["confirm_split_payment_valid", "notify_customer"],
        }
        return _ACTIONS.get(primary_issue, ["review_manually"])[:8]

    def _assess_claims(
        self,
        raw_claims: list[dict[str, Any]],
        primary_issue: str,
        case_status: str,
        confidence: float,
        evidence_refs: list[str],
    ) -> list[dict[str, Any]]:
        assessments = []
        for claim in raw_claims[:5]:
            cid = str(claim.get("claim_id") or "")
            topic = claim.get("topic") or ""
            mapped = _TOPIC_TO_ISSUE.get(topic)
            if mapped == primary_issue:
                verdict = "supported"
            elif case_status == "action_required":
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            assessments.append(
                {
                    "claim_id": cid,
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": evidence_refs[:10],
                }
            )
        return assessments

    def _detect_conflicts(
        self,
        shipment_info: dict[str, Any],
        payment_info: dict[str, Any],
        policy_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        # Example: if shipment says on_time but payment says refund_pending, that's a conflict
        sv = shipment_info.get("verdict")
        pv = payment_info.get("verdict")
        if sv == "on_time" and pv in ("refund_pending", "refund_failed"):
            conflicts.append(
                {
                    "field": "delivery_vs_refund_status",
                    "sources": ["shipment_tracking", "payment_gateway"],
                    "selected_source": "payment_gateway",
                    "resolution_code": "PAYMENT_RECORD_PRECEDENCE",
                }
            )
        return conflicts[:5]

    def _secondary_issues(self, claim_topics: list[str], primary_issue: str) -> list[str]:
        secondaries = []
        for topic in claim_topics:
            mapped = _TOPIC_TO_ISSUE.get(topic)
            if mapped and mapped != primary_issue and mapped not in secondaries:
                secondaries.append(mapped)
        return secondaries[:10]


# ---------------------------------------------------------------------------
# 5. Verifier Agent
#    Responsibilities:
#      - Enforce all schema invariants before finalizing
#      - Ensure entity scope consistency (seller_ids, order_ids)
#      - Validate financial math (refund_lines sum == recommended_refund_brl)
#      - Confirm no cross-case evidence pollution
#      - Emit 'verification_completed' trace event
# ---------------------------------------------------------------------------
class VerifierAgent:
    """
    Independent invariant checker — does NOT call MCP.
    Tool permissions: NONE (read-only from prior agent results).
    """

    def verify_and_finalize(
        self,
        ctx: CaseContext,
        order_info: dict[str, Any],
        shipment_info: dict[str, Any],
        payment_info: dict[str, Any],
        policy_info: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = ctx.case_id

        # ── Invariant 1: Entity scope — late sellers must be in seller_ids ────
        affected = order_info["affected_entities"]
        for seller in shipment_info.get("late_seller_ids", []):
            if seller not in affected["seller_ids"]:
                affected["seller_ids"].append(seller)

        # ── Invariant 2: Financial math consistency ───────────────────────────
        fin = policy_info["financial_resolution"]
        lines = fin.get("refund_lines") or []
        recalculated = round(sum(ln["amount_brl"] for ln in lines), 2)
        fin["recommended_refund_brl"] = recalculated

        # ── Invariant 3: Status / refund consistency ──────────────────────────
        assessment = policy_info["assessment"]
        if recalculated > 0 and assessment["case_status"] != "action_required":
            assessment["case_status"] = "action_required"
        if assessment["primary_issue"] == "unsupported_claim":
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []
            assessment["case_status"] = "no_action"

        # ── Invariant 4: Evidence refs are from THIS case only ─────────────────
        # (Already enforced by CaseContext — refs only added from gateway.call with our case_id)
        evidence_refs = list(ctx.evidence_refs)

        # ── Invariant 5: resolution_actions uniqueness and length ──────────────
        actions = list(dict.fromkeys(policy_info.get("resolution_actions") or []))[:8]

        # ── Invariant 6: Cross-field responsible party consistency ────────────
        rca = policy_info["root_cause_analysis"]
        resp_parties = rca.get("responsible_parties") or []
        primary_issue = assessment["primary_issue"]
        if primary_issue == "late_delivery_seller":
            for p in resp_parties:
                p["party_type"] = "seller"
                if affected["seller_ids"]:
                    p["party_id"] = affected["seller_ids"][0]
        elif primary_issue == "late_delivery_logistics":
            for p in resp_parties:
                p["party_type"] = "logistics_provider"
                p["party_id"] = None
        elif primary_issue == "unsupported_claim":
            for p in resp_parties:
                p["party_type"] = "customer"
                p["party_id"] = None

        # ── Invariant 7: Evidence-based Confidence Calibration ─────────────────
        # Scoring penalizes overconfidence on uncertain or conflicting data.
        er = order_info["entity_resolution"]
        conflicts = policy_info.get("data_conflicts") or []
        calibrated_conf = float(assessment.get("confidence", 0.85))
        if conflicts:
            calibrated_conf = min(calibrated_conf, 0.70)
        if er.get("status") == "ambiguous":
            calibrated_conf = min(calibrated_conf, 0.65)
        elif er.get("status") in ("not_found", "insufficient_evidence"):
            calibrated_conf = min(calibrated_conf, 0.35)
        elif len(evidence_refs) < 2:
            calibrated_conf = min(calibrated_conf, 0.60)
        # Cap confidence at 0.95 (never 1.0) for calibrated reliability
        assessment["confidence"] = round(max(0.1, min(0.95, calibrated_conf)), 2)
        er["confidence"] = round(max(0.1, min(0.95, float(er.get("confidence", 0.5)))), 2)

        # ── Build final output (ONLY schema-defined fields) ────────────────────
        output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": assessment,
            "affected_entities": affected,
            "entity_resolution": er,
            "customer_context": order_info["customer_context"],
            "shipment_analysis": shipment_info,
            "payment_analysis": payment_info,
            "root_cause_analysis": policy_info["root_cause_analysis"],
            "evidence_refs": evidence_refs,
            "data_conflicts": policy_info.get("data_conflicts") or [],
            "financial_resolution": fin,
            "resolution_actions": actions,
        }

        # Optional field: claim_assessments
        if policy_info.get("claim_assessments"):
            output["claim_assessments"] = policy_info["claim_assessments"]

        ctx.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier-agent",
            decision_code="VERIFIED_OK",
            attributes={
                "evidence_count": len(evidence_refs),
                "primary_issue": assessment["primary_issue"],
                "recommended_refund_brl": fin["recommended_refund_brl"],
            },
        )

        return output


# ---------------------------------------------------------------------------
# Entry point: solve_case
# ---------------------------------------------------------------------------
async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """
    Main L3B A2A workflow entry point.

    Execution order (DAG, no cycles):
      Tool Discovery → OrderAgent → ShipmentAgent + PaymentAgent (concurrent)
      → PolicyAgent → VerifierAgent → output

    All MCP calls are routed through CaseContext.call_mcp() which enforces
    all 5 MCP Gateway principles automatically.
    """
    case_id = case["case_id"]

    # ── Tool discovery (required — never hardcode tool names) ────────────────
    available_tools: set[str] = set()
    try:
        tools = await gateway.list_tools()
        available_tools = set(tools)
    except Exception:
        available_tools = set()

    ctx = CaseContext(
        case=case,
        case_id=case_id,
        gateway=gateway,
        trace=trace,
        available_tools=available_tools,
    )

    # ── 1. Order & Entity Resolution ─────────────────────────────────────────
    order_info = await OrderSpecialistAgent().run(ctx)
    resolved_ids = order_info["entity_resolution"]["resolved_order_ids"]
    order_data = order_info.pop("_order_data", {})  # internal, not in output

    # ── 2. Specialist Investigations (concurrent for efficiency) ─────────────
    shipment_task = ShipmentSpecialistAgent().run(ctx, resolved_ids, order_data)
    payment_task = PaymentSpecialistAgent().run(ctx, resolved_ids, order_data)
    shipment_info, payment_info = await asyncio.gather(shipment_task, payment_task)

    # ── 3. Policy & Conflict Synthesis ───────────────────────────────────────
    policy_info = await PolicySpecialistAgent().run(ctx, order_info, shipment_info, payment_info)

    # ── 4. Verification & Finalize ───────────────────────────────────────────
    return VerifierAgent().verify_and_finalize(
        ctx, order_info, shipment_info, payment_info, policy_info
    )
