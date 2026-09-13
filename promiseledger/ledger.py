from __future__ import annotations

import re
from datetime import date
from email.utils import getaddresses
from typing import Any, Iterable

from promiseledger.models import Artifact, Classification, LedgerEntry


TICKET_KEY = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")


def _recipient_addresses(message: dict[str, Any]) -> set[str]:
    values = [
        value
        for value in [
            message.get("to", ""),
            message.get("cc", ""),
            message.get("bcc", ""),
        ]
        if value
    ]
    return {
        address.lower()
        for _name, address in getaddresses(values)
        if address
    }


def contains_exact_key(text: str | None, key: str) -> bool:
    if not text:
        return False
    pattern = rf"(?<![A-Z0-9-]){re.escape(key)}(?![A-Z0-9-])"
    return re.search(pattern, text.upper()) is not None


def is_valid_human_waiver(ticket: dict[str, Any], world: dict[str, Any]) -> bool:
    drafts = world["gmail"].get("drafts", [])
    contacts = [
        contact
        for contact in world["gmail"].get("contacts", [])
        if contact.get("customer_id") == ticket.get("customer_id")
    ]
    if len(contacts) != 1 or not contacts[0].get("email"):
        return False
    expected_addresses = {contacts[0]["email"].lower()}
    for comment in ticket.get("comments", []):
        if comment.get("kind") != "human_waiver" or comment.get("author_type") != "human":
            continue
        approver = comment.get("approver", "")
        draft_matches = [
            draft for draft in drafts if draft.get("id") == comment.get("draft_id")
        ]
        draft = draft_matches[0] if len(draft_matches) == 1 else None
        if (
            "@" in approver
            and draft
            and draft.get("ticket_id") == ticket.get("id")
            and draft.get("customer_id") == ticket.get("customer_id")
            and draft.get("labels") == ["DRAFT"]
            and _recipient_addresses(draft) == expected_addresses
            and draft.get("approved") is True
            and comment.get("body", "").startswith("[PromiseLedger human-waiver]")
            and re.search(
                rf"(?<!\S)approver={re.escape(approver)}(?:\s|$)",
                comment.get("body", ""),
            )
        ):
            return True
    return False


def structural_refusals(world: dict[str, Any]) -> list[str]:
    refusals: list[str] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for ticket in world["linear"].get("tickets", []):
        ticket_id = ticket.get("id")
        key = ticket.get("key")
        valid_id = (
            isinstance(ticket_id, str)
            and bool(ticket_id)
            and ticket_id.strip() == ticket_id
        )
        if not valid_id or ticket_id in ids:
            refusals.append(f"ticket has missing or duplicate immutable id: {ticket_id!r}")
        else:
            ids.add(ticket_id)
        if not isinstance(key, str) or not TICKET_KEY.fullmatch(key) or key in keys:
            refusals.append(f"ticket {ticket_id!r} has invalid or duplicate key: {key!r}")
        else:
            keys.add(key)
        if ticket.get("required_proof") not in {"merged_pr", "sent_email"}:
            refusals.append(
                f"{key}: required_proof must be explicitly merged_pr or sent_email"
            )
        due_date = ticket.get("due_date")
        if due_date:
            try:
                date.fromisoformat(due_date)
            except (TypeError, ValueError):
                refusals.append(f"{key}: due_date is not an ISO date")
    return refusals


def classify(entry: LedgerEntry, today: date) -> Classification:
    ticket = entry.ticket
    has_proof = entry.has_required_proof

    if ticket.get("status") == "Done" and not has_proof:
        return Classification.DESYNC
    if ticket.get("status") in {"Breached", "Reopened"} and entry.remediation_complete:
        return Classification.HEALTHY
    if ticket.get("status") == "Reopened" and not has_proof:
        return Classification.DESYNC
    if ticket.get("status") == "Breached" and not has_proof:
        return Classification.BREACHED
    due_date = ticket.get("due_date")
    if due_date and date.fromisoformat(due_date) < today and not has_proof:
        return Classification.BREACHED
    if has_proof and ticket.get("status") != "Done":
        return Classification.DONE_UNRECORDED
    return Classification.HEALTHY


def build_ledger(world: dict[str, Any]) -> list[LedgerEntry]:
    today = date.fromisoformat(world["scenario"]["now"])
    pull_requests = world["github"].get("pull_requests", [])
    sent_messages = [
        message
        for message in world["gmail"].get("messages", [])
        if "SENT" in message.get("labels", [])
    ]
    entries: list[LedgerEntry] = []

    for ticket in world["linear"].get("tickets", []):
        artifacts: list[Artifact] = []
        key = ticket.get("key", "")
        matching_contacts = [
            contact
            for contact in world["gmail"].get("contacts", [])
            if contact.get("customer_id") == ticket.get("customer_id")
        ]
        expected_addresses = (
            {matching_contacts[0].get("email", "").lower()}
            if len(matching_contacts) == 1 and matching_contacts[0].get("email")
            else set()
        )
        for pull_request in pull_requests:
            linked_keys = pull_request.get("linked_ticket_keys", [])
            linked = key in linked_keys or contains_exact_key(pull_request.get("body"), key)
            if pull_request.get("merged") is True and linked:
                artifacts.append(
                    Artifact(
                        kind="merged_pr",
                        resource_id=pull_request["id"],
                        url=pull_request.get("url", ""),
                        source="github_pr_body",
                    )
                )

        for message in sent_messages:
            if message.get("join_ambiguous"):
                continue
            exact_ticket = message.get("ticket_key") == key or contains_exact_key(
                f"{message.get('subject', '')}\n{message.get('body', '')}", key
            )
            exact_customer = (
                bool(ticket.get("customer_id"))
                and message.get("customer_id") == ticket.get("customer_id")
            )
            recipient_addresses = _recipient_addresses(message)
            if (
                exact_ticket
                and exact_customer
                and expected_addresses
                and recipient_addresses == expected_addresses
            ):
                artifacts.append(
                    Artifact(
                        kind="sent_email",
                        resource_id=message["id"],
                        url=message.get("url", ""),
                        source="gmail_sent",
                    )
                )

        expected_class = "DESYNC" if ticket.get("status") == "Reopened" else "BREACHED"
        evidence_complete = any(
            comment.get("kind") == "evidence"
            and comment.get("author_type") == "agent"
            and comment.get("classification") == expected_class
            for comment in ticket.get("comments", [])
        )
        expected_email = (
            matching_contacts[0].get("email", "").lower()
            if len(matching_contacts) == 1
            else ""
        )
        draft_complete = any(
            draft.get("ticket_id") == ticket.get("id")
            and draft.get("customer_id") == ticket.get("customer_id")
            and draft.get("labels") == ["DRAFT"]
            and _recipient_addresses(draft) == {expected_email}
            for draft in world["gmail"].get("drafts", [])
        )
        digest_complete = any(
            message.get("idempotency_key", "").startswith("promiseledger:digest:")
            and contains_exact_key(message.get("text"), key)
            for message in world["slack"].get("messages", [])
        )
        entry = LedgerEntry(
            ticket=ticket,
            artifacts=artifacts,
            waiver=is_valid_human_waiver(ticket, world),
            remediation_complete=evidence_complete and draft_complete and digest_complete,
        )
        entry.classification = classify(entry, today)
        entries.append(entry)
    return entries


def identity_refusals(
    world: dict[str, Any], entries: Iterable[LedgerEntry]
) -> list[str]:
    refusals: list[str] = []
    contacts = world["gmail"].get("contacts", [])

    for entry in entries:
        ticket = entry.ticket
        key = ticket.get("key")

        if entry.classification not in {Classification.BREACHED, Classification.DESYNC}:
            continue
        customer_id = ticket.get("customer_id")
        if not customer_id:
            same_name = [
                item for item in contacts if item.get("name") == ticket.get("customer_name")
            ]
            refusals.append(
                f"{key}: customer_id missing; name match count={len(same_name)} and names are never join keys"
            )
            continue
        matches = [item for item in contacts if item.get("customer_id") == customer_id]
        if len(matches) != 1:
            refusals.append(
                f"{key}: customer_id {customer_id!r} resolved to {len(matches)} contacts"
            )
            continue
        contact = matches[0]
        if not all(
            isinstance(contact.get(field), str) and bool(contact.get(field).strip())
            for field in ["id", "email"]
        ):
            refusals.append(f"{key}: resolved contact lacks immutable id or email")
            continue

        draft_key = f"promiseledger:{ticket.get('id')}:gmail-draft:v1:v1"
        candidate_drafts = [
            draft
            for draft in world["gmail"].get("drafts", [])
            if draft.get("ticket_id") == ticket.get("id")
            or draft.get("id") == f"draft-{ticket.get('id')}"
            or draft.get("idempotency_key") == draft_key
        ]
        if not candidate_drafts:
            continue
        expected_addresses = {contact["email"].lower()}
        valid_drafts = [
            draft
            for draft in candidate_drafts
            if draft.get("ticket_id") == ticket.get("id")
            and draft.get("customer_id") == customer_id
            and draft.get("labels") == ["DRAFT"]
            and _recipient_addresses(draft) == expected_addresses
        ]
        if len(candidate_drafts) != 1 or len(valid_drafts) != 1:
            refusals.append(f"{key}: existing draft identity or recipient conflicts")
    return refusals


def contact_for(world: dict[str, Any], customer_id: str) -> dict[str, Any]:
    matches = [
        contact
        for contact in world["gmail"].get("contacts", [])
        if contact.get("customer_id") == customer_id
    ]
    if len(matches) != 1:
        raise ValueError(f"customer_id {customer_id!r} did not resolve uniquely")
    return matches[0]
