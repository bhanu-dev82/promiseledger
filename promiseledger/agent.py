from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any, Protocol

from promiseledger.ledger import (
    build_ledger,
    contact_for,
    contains_exact_key,
    identity_refusals,
    structural_refusals,
)
from promiseledger.models import Classification, LedgerEntry, WriteReceipt


class Adapter(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def list_tickets(self) -> list[dict[str, Any]]: ...
    def list_pull_requests(self) -> list[dict[str, Any]]: ...
    def list_sent_messages(self) -> list[dict[str, Any]]: ...
    def list_contacts(self) -> list[dict[str, Any]]: ...
    def list_slack_messages(self) -> list[dict[str, Any]]: ...
    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None: ...
    def get_draft(self, draft_id: str) -> dict[str, Any] | None: ...
    def update_ticket_status(self, ticket_id: str, status: str, idempotency_key: str) -> WriteReceipt: ...
    def add_ticket_comment(self, ticket_id: str, body: str, classification: str, idempotency_key: str) -> WriteReceipt: ...
    def create_draft(self, ticket_id: str, customer_id: str, recipient: str, subject: str, body: str, idempotency_key: str) -> WriteReceipt: ...
    def post_digest(self, text: str, idempotency_key: str) -> WriteReceipt: ...


@dataclass
class TicketResult:
    ticket_id: str
    key: str
    classification: str
    verified: bool
    receipts: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass
class AgentReport:
    run_id: str
    refused: bool
    refusal_reasons: list[str]
    results: list[TicketResult]

    @property
    def verified(self) -> bool:
        return not self.refused and all(result.verified for result in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "refused": self.refused,
            "refusal_reasons": self.refusal_reasons,
            "verified": self.verified,
            "results": [
                {
                    "ticket_id": result.ticket_id,
                    "key": result.key,
                    "classification": result.classification,
                    "verified": result.verified,
                    "receipts": result.receipts,
                    "reason": result.reason,
                }
                for result in self.results
            ],
        }


class PromiseLedgerAgent:
    def __init__(self, adapter: Adapter, run_id: str, trace: Any = None):
        self.adapter = adapter
        self.run_id = run_id
        self.trace = trace or (lambda *_args, **_kwargs: None)

    def run(self) -> AgentReport:
        world = self._read_world()
        refusals = structural_refusals(world)
        if refusals:
            self.trace("refusal", reasons=refusals, writes=0)
            return AgentReport(self.run_id, True, refusals, [])
        ledger = build_ledger(world)
        for entry in ledger:
            self.trace(
                "classification",
                ticket_id=entry.ticket["id"],
                ticket_key=entry.ticket["key"],
                classification=entry.classification.value,
                required_proof=entry.ticket.get("required_proof"),
                artifacts=[artifact.__dict__ for artifact in entry.artifacts],
                waiver=entry.waiver,
            )

        refusals = identity_refusals(world, ledger)
        allowed_ids = getattr(self.adapter, "allowed_ticket_ids", None)
        actionable_ids = {
            entry.ticket["id"]
            for entry in ledger
            if entry.classification != Classification.HEALTHY
        }
        if allowed_ids is not None:
            disallowed = sorted(actionable_ids - set(allowed_ids))
            if disallowed:
                refusals.append(f"actionable tickets are outside the explicit allowlist: {disallowed}")
            unresolved = sorted(getattr(self.adapter, "unresolved_ticket_ids", set()))
            if unresolved:
                refusals.append(f"allowlisted Linear ticket IDs were not found: {unresolved}")
        if refusals:
            self.trace("refusal", reasons=refusals, writes=0)
            return AgentReport(self.run_id, True, refusals, [])

        results: list[TicketResult] = []
        digest_rows: list[str] = []
        for entry in sorted(ledger, key=lambda item: item.ticket["key"]):
            result = self._remediate(entry, world)
            results.append(result)
            if result.verified and entry.classification in {
                Classification.BREACHED,
                Classification.DESYNC,
            }:
                digest_rows.append(
                    f"• {entry.ticket['key']} — {entry.classification.value} — "
                    f"Linear + Gmail draft verified from read-back"
                )

        actionable_results = [
            result
            for result in results
            if result.classification
            in {Classification.BREACHED.value, Classification.DESYNC.value}
        ]
        if digest_rows and all(result.verified for result in actionable_results):
            digest_results = [
                result
                for result in actionable_results
                if result.verified
            ]
            digest_basis = ",".join(sorted(result.ticket_id for result in digest_results))
            digest_hash = hashlib.sha256(digest_basis.encode()).hexdigest()[:16]
            idempotency_key = f"promiseledger:digest:{digest_hash}:v1"
            receipt = self.adapter.post_digest(
                "PromiseLedger verified reconciliation\n" + "\n".join(digest_rows),
                idempotency_key,
            )
            self.trace("digest", receipt=receipt.as_dict(), rows=len(digest_rows))
            matching_digests = [
                message
                for message in self.adapter.list_slack_messages()
                if message.get("idempotency_key") == idempotency_key
            ]
            digest_verified = len(matching_digests) == 1 and all(
                contains_exact_key(matching_digests[0].get("text"), result.key)
                for result in digest_results
            )
            if not digest_verified:
                for result in digest_results:
                    result.verified = False
                    result.reason = "read-back failed: Slack digest missing or mismatched"
            self.trace("verification", app="slack", verified=digest_verified)

        return AgentReport(self.run_id, False, [], results)

    def _read_world(self) -> dict[str, Any]:
        world = self.adapter.snapshot()
        world["linear"]["tickets"] = self.adapter.list_tickets()
        world["github"]["pull_requests"] = self.adapter.list_pull_requests()
        world["gmail"]["messages"] = self.adapter.list_sent_messages()
        world["gmail"]["contacts"] = self.adapter.list_contacts()
        return world

    def _remediate(self, entry: LedgerEntry, world: dict[str, Any]) -> TicketResult:
        ticket = entry.ticket
        classification = entry.classification
        result = TicketResult(
            ticket_id=ticket["id"],
            key=ticket["key"],
            classification=classification.value,
            verified=True,
            reason="no mutation required",
        )
        if classification == Classification.HEALTHY:
            self.trace("no_op", ticket_id=ticket["id"], proof="final state already consistent")
            return result

        status_by_class = {
            Classification.DONE_UNRECORDED: "Done",
            Classification.BREACHED: "Breached",
            Classification.DESYNC: "Reopened",
        }
        expected_status = status_by_class[classification]
        evidence = self._evidence(entry, expected_status)
        comment_key = self._key(ticket["id"], "evidence", classification.value.lower())
        comment_receipt = self.adapter.add_ticket_comment(
            ticket["id"],
            evidence,
            classification.value,
            comment_key,
        )
        result.receipts.append(comment_receipt.as_dict())
        comment_readback = self.adapter.get_ticket(ticket["id"])
        has_comment = bool(comment_readback) and any(
            comment.get("classification") == classification.value
            and comment.get("author_type") == "agent"
            and (
                comment.get("idempotency_key") == comment_key
                or comment_key in comment.get("body", "")
            )
            for comment in comment_readback.get("comments", [])
        )
        if not has_comment:
            result.verified = False
            result.reason = "read-back failed: evidence comment missing"
            self.trace("verification", ticket_id=ticket["id"], comment_verified=False, verified=False)
            return result

        status_receipt = self.adapter.update_ticket_status(
            ticket["id"],
            expected_status,
            self._key(ticket["id"], "status", expected_status.lower()),
        )
        result.receipts.append(status_receipt.as_dict())
        status_readback = self.adapter.get_ticket(ticket["id"])
        status_verified = bool(status_readback) and status_readback.get("status") == expected_status
        if not status_verified:
            result.verified = False
            result.reason = "read-back failed: status transition missing"
            self.trace(
                "verification",
                ticket_id=ticket["id"],
                comment_verified=True,
                status_verified=False,
                verified=False,
            )
            return result

        draft_id: str | None = None
        if classification in {Classification.BREACHED, Classification.DESYNC}:
            contact = contact_for(world, ticket["customer_id"])
            draft_id = f"draft-{ticket['id']}"
            draft_receipt = self.adapter.create_draft(
                ticket_id=ticket["id"],
                customer_id=ticket["customer_id"],
                recipient=contact["email"],
                subject=f"Draft for human review: {ticket['key']} commitment update",
                body=(
                    f"UNSENT — human review required\n\n"
                    f"Commitment: {ticket['key']} — {ticket['title']}\n"
                    f"Verified state: {classification.value}.\n"
                    f"Evidence: {evidence}\n\n"
                    "PromiseLedger cannot send this message."
                ),
                idempotency_key=self._key(ticket["id"], "gmail-draft", "v1"),
            )
            result.receipts.append(draft_receipt.as_dict())

        draft_verified = True
        if draft_id:
            draft = self.adapter.get_draft(draft_id)
            draft_verified = bool(draft) and draft.get("labels") == ["DRAFT"]
            draft_verified = draft_verified and draft.get("customer_id") == ticket["customer_id"]
            draft_addresses = {
                address.lower()
                for _name, address in getaddresses(
                    [
                        value
                        for value in [
                            draft.get("to", "") if draft else "",
                            draft.get("cc", "") if draft else "",
                            draft.get("bcc", "") if draft else "",
                        ]
                        if value
                    ]
                )
                if address
            }
            draft_verified = draft_verified and draft_addresses == {contact["email"].lower()}

        result.verified = bool(status_verified and has_comment and draft_verified)
        result.reason = (
            "status, evidence, and draft verified from app state"
            if result.verified
            else (
                f"read-back failed: status={status_verified}, "
                f"comment={has_comment}, draft={draft_verified}"
            )
        )
        self.trace(
            "verification",
            ticket_id=ticket["id"],
            status_verified=status_verified,
            comment_verified=has_comment,
            draft_verified=draft_verified,
            verified=result.verified,
        )
        return result

    @staticmethod
    def _key(ticket_id: str, action: str, value: str) -> str:
        return f"promiseledger:{ticket_id}:{action}:{value}:v1"

    @staticmethod
    def _evidence(entry: LedgerEntry, expected_status: str) -> str:
        ticket = entry.ticket
        artifacts = (
            ", ".join(
                f"{artifact.kind}:{artifact.resource_id} ({artifact.url})"
                for artifact in entry.artifacts
            )
            or "none after exact GitHub/Gmail joins"
        )
        return (
            f"[PromiseLedger] {ticket['key']} classification={entry.classification.value}; "
            f"required_proof={ticket['required_proof']}; artifacts={artifacts}; "
            f"due_date={ticket.get('due_date')}; planned_transition={expected_status}."
        )
