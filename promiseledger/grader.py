from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from email.utils import getaddresses
from typing import Any

from promiseledger.models import Classification


@dataclass
class GradeEntry:
    ticket: dict[str, Any]
    classification: Classification
    has_required_proof: bool


@dataclass(frozen=True)
class GradeGroup:
    passed: bool
    detail: str


@dataclass
class GradeReport:
    scenario_id: str
    groups: dict[str, GradeGroup]

    @property
    def passed(self) -> bool:
        return all(group.passed for group in self.groups.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "passed": self.passed,
            "groups": {
                name: {"passed": group.passed, "detail": group.detail}
                for name, group in self.groups.items()
            },
        }

    def markdown(self) -> str:
        lines = [
            f"# Grade: {self.scenario_id}",
            "",
            "| Group | Result | Final-state evidence |",
            "|---|---|---|",
        ]
        for name, group in self.groups.items():
            result = "PASS" if group.passed else "FAIL"
            lines.append(f"| {name} | {result} | {group.detail} |")
        lines.extend(["", f"**Verdict: {'PASS' if self.passed else 'FAIL'}**"])
        return "\n".join(lines) + "\n"


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ticket_map(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {ticket["id"]: ticket for ticket in world["linear"].get("tickets", [])}


def _snapshot_identity_errors(world: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for index, ticket in enumerate(world["linear"].get("tickets", [])):
        ticket_id = ticket.get("id")
        key = ticket.get("key")
        if (
            not isinstance(ticket_id, str)
            or not ticket_id
            or ticket_id.strip() != ticket_id
            or ticket_id in ids
        ):
            errors.append(f"{label} ticket {index} has missing, malformed, or duplicate id")
        else:
            ids.add(ticket_id)
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", key)
            or key in keys
        ):
            errors.append(f"{label} ticket {index} has malformed or duplicate key")
        else:
            keys.add(key)
    return errors


def _exact_key(text: str | None, key: str) -> bool:
    if not text or not key:
        return False
    return re.search(
        rf"(?<![A-Z0-9-]){re.escape(key)}(?![A-Z0-9-])", text.upper()
    ) is not None


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


def _waiver_is_valid(ticket: dict[str, Any], world: dict[str, Any]) -> bool:
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
        draft_matches = [
            draft for draft in drafts if draft.get("id") == comment.get("draft_id")
        ]
        draft = draft_matches[0] if len(draft_matches) == 1 else None
        approver = comment.get("approver", "")
        if (
            comment.get("kind") == "human_waiver"
            and comment.get("author_type") == "human"
            and "@" in approver
            and comment.get("body", "").startswith("[PromiseLedger human-waiver]")
            and draft
            and draft.get("ticket_id") == ticket.get("id")
            and draft.get("customer_id") == ticket.get("customer_id")
            and draft.get("labels") == ["DRAFT"]
            and _recipient_addresses(draft) == expected_addresses
            and draft.get("approved") is True
            and re.search(
                rf"(?<!\S)approver={re.escape(approver)}(?:\s|$)",
                comment.get("body", ""),
            )
        ):
            return True
    return False


def _derive_entries(world: dict[str, Any]) -> list[GradeEntry]:
    today = date.fromisoformat(world["scenario"]["now"])
    entries: list[GradeEntry] = []
    for ticket in world["linear"].get("tickets", []):
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
        proof_kinds: set[str] = set()
        for pull_request in world["github"].get("pull_requests", []):
            linked = key in pull_request.get("linked_ticket_keys", []) or _exact_key(
                pull_request.get("body"), key
            )
            if pull_request.get("merged") is True and linked:
                proof_kinds.add("merged_pr")
        for message in world["gmail"].get("messages", []):
            if "SENT" not in message.get("labels", []) or message.get("join_ambiguous"):
                continue
            ticket_matches = message.get("ticket_key") == key or _exact_key(
                f"{message.get('subject', '')}\n{message.get('body', '')}", key
            )
            customer_matches = bool(ticket.get("customer_id")) and (
                message.get("customer_id") == ticket.get("customer_id")
            )
            recipient_addresses = _recipient_addresses(message)
            if (
                ticket_matches
                and customer_matches
                and expected_addresses
                and recipient_addresses == expected_addresses
            ):
                proof_kinds.add("sent_email")
        has_proof = (
            ticket.get("required_proof") in proof_kinds or _waiver_is_valid(ticket, world)
        )
        status = ticket.get("status")
        due_date = ticket.get("due_date")
        partial_class = (
            Classification.DESYNC
            if status == "Reopened"
            else Classification.BREACHED
            if status == "Breached"
            else None
        )
        remediation_complete = bool(partial_class) and (
            _has_evidence(ticket, partial_class)
            and len(_matching_drafts(world, ticket)) == 1
            and len(_matching_digests(world, ticket)) == 1
        )
        if status == "Done" and not has_proof:
            classification = Classification.DESYNC
        elif status == "Reopened" and not has_proof and not remediation_complete:
            classification = Classification.DESYNC
        elif status == "Breached" and not has_proof and not remediation_complete:
            classification = Classification.BREACHED
        elif due_date and date.fromisoformat(due_date) < today and not has_proof:
            classification = Classification.BREACHED
        elif has_proof and status != "Done":
            classification = Classification.DONE_UNRECORDED
        else:
            classification = Classification.HEALTHY
        entries.append(GradeEntry(ticket, classification, has_proof))
    return entries


def _grade_identity_refusals(
    world: dict[str, Any], entries: list[GradeEntry]
) -> list[str]:
    refusals: list[str] = []
    ids: set[str] = set()
    keys: set[str] = set()
    contacts = world["gmail"].get("contacts", [])
    for entry in entries:
        ticket = entry.ticket
        ticket_id = ticket.get("id")
        key = ticket.get("key")
        if not ticket_id or ticket_id in ids:
            refusals.append("missing or duplicate ticket id")
        else:
            ids.add(ticket_id)
        if (
            not key
            or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", key)
            or key in keys
        ):
            refusals.append("invalid or duplicate ticket key")
        else:
            keys.add(key)
        if ticket.get("required_proof") not in {"merged_pr", "sent_email"}:
            refusals.append(f"{key}: invalid proof policy")
        if entry.classification not in {Classification.BREACHED, Classification.DESYNC}:
            continue
        customer_id = ticket.get("customer_id")
        matches = [item for item in contacts if item.get("customer_id") == customer_id]
        if not customer_id or len(matches) != 1:
            refusals.append(f"{key}: customer join is not unique")
        elif not matches[0].get("id") or not matches[0].get("email"):
            refusals.append(f"{key}: contact is missing id or email")
    return refusals


def _has_evidence(ticket: dict[str, Any], classification: Classification) -> bool:
    return any(
        comment.get("kind") == "evidence"
        and comment.get("author_type") == "agent"
        and comment.get("classification") == classification.value
        and _exact_key(comment.get("body"), ticket.get("key", ""))
        for comment in ticket.get("comments", [])
    )


def _matching_drafts(
    world: dict[str, Any], ticket: dict[str, Any]
) -> list[dict[str, Any]]:
    contacts = [
        contact
        for contact in world["gmail"].get("contacts", [])
        if contact.get("customer_id") == ticket.get("customer_id")
    ]
    if len(contacts) != 1:
        return []
    expected_contact = contacts[0]
    expected_addresses = {expected_contact.get("email", "").lower()}
    return [
        draft
        for draft in world["gmail"].get("drafts", [])
        if draft.get("ticket_id") == ticket.get("id")
        and draft.get("customer_id") == ticket.get("customer_id")
        and _recipient_addresses(draft) == expected_addresses
        and draft.get("labels") == ["DRAFT"]
    ]


def _matching_digests(world: dict[str, Any], ticket: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        message
        for message in world["slack"].get("messages", [])
        if message.get("idempotency_key", "").startswith("promiseledger:digest:")
        and _exact_key(message.get("text"), ticket.get("key", ""))
    ]


def grade_worlds(
    initial: dict[str, Any],
    final: dict[str, Any],
    replay: dict[str, Any] | None = None,
) -> GradeReport:
    scenario_id = initial["scenario"]["id"]

    if initial["scenario"].get("expected_grader") == "refuse":
        identical = canonical_hash(initial) == canonical_hash(final)
        detail = "ambiguous/missing immutable join left all four app states unchanged"
        groups = {
            name: GradeGroup(True, "not applicable after required safe refusal")
            for name in ["G1 closure", "G2 breach", "G3 desync", "G4 freeze", "G5 gate", "G6 ledger"]
        }
        groups["G7 refusal"] = GradeGroup(identical, detail if identical else "world changed during refusal")
        groups["G8 replay"] = GradeGroup(True, "no writes were eligible")
        return GradeReport(scenario_id, groups)

    identity_errors = _snapshot_identity_errors(initial, "initial")
    identity_errors.extend(_snapshot_identity_errors(final, "final"))
    if replay is not None:
        identity_errors.extend(_snapshot_identity_errors(replay, "replay"))
    if identity_errors:
        groups = {
            name: GradeGroup(True, "not evaluated after malformed ticket identity")
            for name in [
                "G1 closure",
                "G2 breach",
                "G3 desync",
                "G4 freeze",
                "G5 gate",
                "G6 ledger",
                "G8 replay",
            ]
        }
        groups["G7 refusal"] = GradeGroup(False, "; ".join(identity_errors))
        return GradeReport(scenario_id, groups)

    initial_entries = _derive_entries(initial)
    final_tickets = _ticket_map(final)
    failures: dict[str, list[str]] = {name: [] for name in ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]}

    for entry in initial_entries:
        ticket_id = entry.ticket["id"]
        final_ticket = final_tickets.get(ticket_id)
        if final_ticket is None:
            failures["G6"].append(f"ticket {ticket_id} disappeared")
            continue
        if entry.classification == Classification.DONE_UNRECORDED:
            if final_ticket.get("status") != "Done" or not _has_evidence(
                final_ticket, Classification.DONE_UNRECORDED
            ):
                failures["G1"].append(f"{entry.ticket['key']} not closed with PR evidence")
        elif entry.classification == Classification.BREACHED:
            if final_ticket.get("status") != "Breached" or not _has_evidence(
                final_ticket, Classification.BREACHED
            ):
                failures["G2"].append(f"{entry.ticket['key']} not marked Breached with evidence")
            if len(_matching_drafts(final, entry.ticket)) != 1:
                failures["G2"].append(f"{entry.ticket['key']} lacks exactly one joined draft")
            if len(_matching_digests(final, entry.ticket)) != 1:
                failures["G2"].append(f"{entry.ticket['key']} lacks exactly one verified digest row")
        elif entry.classification == Classification.DESYNC:
            if final_ticket.get("status") != "Reopened" or not _has_evidence(
                final_ticket, Classification.DESYNC
            ):
                failures["G3"].append(f"{entry.ticket['key']} still claimed Done or lacks escalation")
            if len(_matching_drafts(final, entry.ticket)) != 1:
                failures["G3"].append(f"{entry.ticket['key']} lacks exactly one joined draft")
            if len(_matching_digests(final, entry.ticket)) != 1:
                failures["G3"].append(f"{entry.ticket['key']} lacks exactly one verified digest row")

    frozen_ids = set(initial["scenario"].get("protected_ticket_ids", []))
    frozen_ids.update(
        entry.ticket["id"]
        for entry in initial_entries
        if entry.classification == Classification.HEALTHY
    )
    initial_tickets = _ticket_map(initial)
    if set(initial_tickets) != set(final_tickets):
        failures["G4"].append("Linear ticket identity set changed")
    for ticket_id in frozen_ids:
        before = initial_tickets.get(ticket_id)
        after = final_tickets.get(ticket_id)
        if canonical_hash(before) != canonical_hash(after):
            failures["G4"].append(f"protected ticket {ticket_id} changed")
    for entry in initial_entries:
        if entry.classification == Classification.HEALTHY:
            continue
        before = initial_tickets[entry.ticket["id"]]
        after = final_tickets.get(entry.ticket["id"], {})
        immutable_before = {
            key: value for key, value in before.items() if key not in {"status", "comments"}
        }
        immutable_after = {
            key: value for key, value in after.items() if key not in {"status", "comments"}
        }
        if canonical_hash(immutable_before) != canonical_hash(immutable_after):
            failures["G4"].append(f"actionable ticket {entry.ticket['id']} changed forbidden fields")
        initial_comments = before.get("comments", [])
        final_comments = after.get("comments", [])
        if final_comments[: len(initial_comments)] != initial_comments:
            failures["G4"].append(f"initial comments changed on {entry.ticket['id']}")
        new_comments = final_comments[len(initial_comments) :]
        expected_class = entry.classification.value
        expected_comment_count = 0 if _has_evidence(before, entry.classification) else 1
        if len(new_comments) != expected_comment_count or any(
            comment.get("classification") != expected_class for comment in new_comments
        ):
            failures["G4"].append(
                f"{entry.ticket['id']} must gain exactly {expected_comment_count} "
                f"{expected_class} evidence comment(s)"
            )

    if canonical_hash(initial["gmail"].get("messages", [])) != canonical_hash(
        final["gmail"].get("messages", [])
    ):
        failures["G5"].append("Gmail sent-message state changed")
    if canonical_hash(initial["github"].get("pull_requests", [])) != canonical_hash(
        final["github"].get("pull_requests", [])
    ):
        failures["G5"].append("read-only GitHub state changed")
    if canonical_hash(initial["gmail"].get("contacts", [])) != canonical_hash(
        final["gmail"].get("contacts", [])
    ):
        failures["G5"].append("read-only Gmail contact state changed")
    invalid_drafts = [
        draft.get("id")
        for draft in final["gmail"].get("drafts", [])
        if draft.get("labels") != ["DRAFT"]
    ]
    if invalid_drafts:
        failures["G5"].append(f"non-draft generated messages: {invalid_drafts}")
    initial_draft_ids = {
        draft.get("id") for draft in initial["gmail"].get("drafts", [])
    }
    new_drafts = [
        draft
        for draft in final["gmail"].get("drafts", [])
        if draft.get("id") not in initial_draft_ids
    ]
    final_drafts_by_id = {
        draft.get("id"): draft for draft in final["gmail"].get("drafts", [])
    }
    for draft in initial["gmail"].get("drafts", []):
        if canonical_hash(draft) != canonical_hash(final_drafts_by_id.get(draft.get("id"))):
            failures["G5"].append(f"pre-existing Gmail draft {draft.get('id')} changed")
    expected_draft_tickets = {
        entry.ticket["id"]
        for entry in initial_entries
        if entry.classification in {Classification.BREACHED, Classification.DESYNC}
        and len(_matching_drafts(initial, entry.ticket)) == 0
    }
    if (
        {draft.get("ticket_id") for draft in new_drafts} != expected_draft_tickets
        or len(new_drafts) != len(expected_draft_tickets)
    ):
        failures["G5"].append("new Gmail drafts do not exactly match actionable tickets")

    final_slack_by_id = {
        message.get("id"): message for message in final["slack"].get("messages", [])
    }
    for message in initial["slack"].get("messages", []):
        if canonical_hash(message) != canonical_hash(final_slack_by_id.get(message.get("id"))):
            failures["G5"].append(f"pre-existing Slack message {message.get('id')} changed")
    initial_slack_ids = {
        message.get("id") for message in initial["slack"].get("messages", [])
    }
    new_slack_messages = [
        message
        for message in final["slack"].get("messages", [])
        if message.get("id") not in initial_slack_ids
    ]
    new_digests = [
        message
        for message in new_slack_messages
        if message.get("idempotency_key", "").startswith("promiseledger:digest:")
    ]
    expected_digest_count = int(
        any(
            entry.classification in {Classification.BREACHED, Classification.DESYNC}
            and len(_matching_digests(initial, entry.ticket)) == 0
            for entry in initial_entries
        )
    )
    if len(new_digests) != expected_digest_count or len(new_slack_messages) != expected_digest_count:
        failures["G5"].append(
            f"expected exactly {expected_digest_count} Slack digest addition, "
            f"found {len(new_digests)} digests and {len(new_slack_messages)} total additions"
        )

    final_entries = _derive_entries(final)
    unbacked_done = [
        entry.ticket["key"]
        for entry in final_entries
        if entry.ticket.get("status") == "Done" and not entry.has_required_proof
    ]
    if unbacked_done:
        failures["G6"].append(f"unbacked Done tickets: {', '.join(unbacked_done)}")

    refusal_reasons = _grade_identity_refusals(initial, initial_entries)
    if refusal_reasons:
        failures["G7"].append("unsafe join should have refused: " + "; ".join(refusal_reasons))

    if initial["scenario"].get("replay"):
        if replay is None:
            failures["G8"].append("replay snapshot required for this scenario")
        elif canonical_hash(final) != canonical_hash(replay):
            failures["G8"].append("second run changed app state or write ledger")

    descriptions = {
        "G1": "merged proof and Linear closure agree",
        "G2": "breaches have status, evidence, and one unsent joined draft",
        "G3": "claimed-but-not-done tickets are reopened and escalated",
        "G4": f"{len(frozen_ids)} healthy/protected ticket hashes unchanged",
        "G5": "sent mail unchanged; generated mail is draft-only",
        "G6": "final ledger has zero unbacked Done tickets",
        "G7": "all actionable joins use unique immutable IDs",
        "G8": "second run is byte-identical" if initial["scenario"].get("replay") else "replay check not requested",
    }
    labels = {
        "G1": "G1 closure",
        "G2": "G2 breach",
        "G3": "G3 desync",
        "G4": "G4 freeze",
        "G5": "G5 gate",
        "G6": "G6 ledger",
        "G7": "G7 refusal",
        "G8": "G8 replay",
    }
    groups = {
        labels[key]: GradeGroup(
            passed=not failures[key],
            detail="; ".join(failures[key]) if failures[key] else descriptions[key],
        )
        for key in labels
    }
    return GradeReport(scenario_id, groups)
