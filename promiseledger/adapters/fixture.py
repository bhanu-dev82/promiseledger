from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

from promiseledger.models import ForbiddenOperation, WriteReceipt


TraceFn = Callable[..., None]


class FixtureAdapter:
    """Four-app state adapter backed by one isolated JSON world."""

    def __init__(self, world: dict[str, Any], trace: TraceFn | None = None):
        self.world = copy.deepcopy(world)
        self.trace = trace or (lambda *_args, **_kwargs: None)

    @classmethod
    def from_path(cls, path: str | Path, trace: TraceFn | None = None) -> "FixtureAdapter":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), trace=trace)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.world)

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.world, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def list_tickets(self) -> list[dict[str, Any]]:
        tickets = copy.deepcopy(self.world["linear"]["tickets"])
        self.trace("read", app="linear", action="list_tickets", count=len(tickets))
        return tickets

    def list_pull_requests(self) -> list[dict[str, Any]]:
        prs = copy.deepcopy(self.world["github"]["pull_requests"])
        self.trace("read", app="github", action="list_pull_requests", count=len(prs))
        return prs

    def list_sent_messages(self) -> list[dict[str, Any]]:
        sent = [
            copy.deepcopy(message)
            for message in self.world["gmail"]["messages"]
            if "SENT" in message.get("labels", [])
        ]
        self.trace("read", app="gmail", action="list_sent_messages", count=len(sent))
        return sent

    def list_contacts(self) -> list[dict[str, Any]]:
        contacts = copy.deepcopy(self.world["gmail"]["contacts"])
        self.trace("read", app="gmail", action="list_contacts", count=len(contacts))
        return contacts

    def list_slack_messages(self) -> list[dict[str, Any]]:
        messages = copy.deepcopy(self.world["slack"]["messages"])
        self.trace("read", app="slack", action="list_messages", count=len(messages))
        return messages

    def list_drafts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.world["gmail"]["drafts"])

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        ticket = self._find("linear", "tickets", ticket_id)
        self.trace(
            "readback",
            app="linear",
            action="get_ticket",
            target_id=ticket_id,
            found=ticket is not None,
        )
        return copy.deepcopy(ticket) if ticket else None

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        draft = self._find("gmail", "drafts", draft_id)
        self.trace(
            "readback",
            app="gmail",
            action="get_draft",
            target_id=draft_id,
            found=draft is not None,
        )
        return copy.deepcopy(draft) if draft else None

    def update_ticket_status(
        self, ticket_id: str, status: str, idempotency_key: str
    ) -> WriteReceipt:
        def apply() -> None:
            ticket = self._required("linear", "tickets", ticket_id)
            ticket["status"] = status

        return self._write("linear", "update_status", ticket_id, idempotency_key, apply)

    def add_ticket_comment(
        self,
        ticket_id: str,
        body: str,
        classification: str,
        idempotency_key: str,
    ) -> WriteReceipt:
        def apply() -> None:
            ticket = self._required("linear", "tickets", ticket_id)
            ticket.setdefault("comments", []).append(
                {
                    "id": f"comment-{ticket_id}-{classification.lower()}",
                    "body": body,
                    "author_type": "agent",
                    "kind": "evidence",
                    "classification": classification,
                    "idempotency_key": idempotency_key,
                }
            )

        return self._write("linear", "add_comment", ticket_id, idempotency_key, apply)

    def create_draft(
        self,
        ticket_id: str,
        customer_id: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> WriteReceipt:
        draft_id = f"draft-{ticket_id}"

        def apply() -> None:
            self.world["gmail"]["drafts"].append(
                {
                    "id": draft_id,
                    "ticket_id": ticket_id,
                    "customer_id": customer_id,
                    "to": recipient,
                    "subject": subject,
                    "body": body,
                    "labels": ["DRAFT"],
                    "approved": False,
                    "idempotency_key": idempotency_key,
                }
            )

        return self._write("gmail", "create_draft", draft_id, idempotency_key, apply)

    def post_digest(self, text: str, idempotency_key: str) -> WriteReceipt:
        channel_id = self.world["slack"]["channel_id"]

        def apply() -> None:
            self.world["slack"]["messages"].append(
                {
                    "id": f"message-{idempotency_key}",
                    "channel_id": channel_id,
                    "text": text,
                    "idempotency_key": idempotency_key,
                }
            )

        return self._write("slack", "post_digest", channel_id, idempotency_key, apply)

    def record_human_waiver(
        self, ticket_id: str, draft_id: str, approver: str
    ) -> WriteReceipt:
        ticket = self._required("linear", "tickets", ticket_id)
        draft = self._required("gmail", "drafts", draft_id)
        if draft.get("ticket_id") != ticket_id:
            raise ValueError("draft does not belong to ticket")
        if not approver or "@" not in approver:
            raise ValueError("an explicit human approver email is required")
        idempotency_key = f"promiseledger:{ticket_id}:human-waiver:{draft_id}:v1"

        def apply() -> None:
            draft["approved"] = True
            ticket.setdefault("comments", []).append(
                {
                    "id": f"waiver-{ticket_id}-{draft_id}",
                    "body": (
                        f"[PromiseLedger human-waiver] ticket={ticket_id} "
                        f"draft={draft_id} approver={approver}"
                    ),
                    "author_type": "human",
                    "kind": "human_waiver",
                    "draft_id": draft_id,
                    "approver": approver,
                    "idempotency_key": idempotency_key,
                }
            )

        return self._write("linear", "human_waiver", ticket_id, idempotency_key, apply)

    def send_email(self, *_args: Any, **_kwargs: Any) -> None:
        raise ForbiddenOperation("PromiseLedger has no authorized Gmail send path")

    def mutate_github(self, *_args: Any, **_kwargs: Any) -> None:
        raise ForbiddenOperation("GitHub is read-only for PromiseLedger")

    def _write(
        self,
        app: str,
        action: str,
        target_id: str,
        idempotency_key: str,
        apply: Callable[[], None],
    ) -> WriteReceipt:
        for existing in self.world["writes"]:
            if existing["idempotency_key"] == idempotency_key:
                receipt = WriteReceipt(
                    app, action, target_id, idempotency_key, 200, False, duplicate=True
                )
                self.trace("write_receipt", **receipt.as_dict())
                return receipt

        fault = self.world.get("faults", {}).get(f"{app}.{action}", {})
        fault_targets = fault.get("targets", [])
        silent_noop = fault.get("mode") == "http_200_noop" and (
            target_id in fault_targets or "*" in fault_targets
        )
        if not silent_noop:
            apply()

        receipt = WriteReceipt(
            app=app,
            action=action,
            target_id=target_id,
            idempotency_key=idempotency_key,
            status_code=200,
            applied=not silent_noop,
        )
        self.world["writes"].append(receipt.as_dict())
        self.trace("write_receipt", **receipt.as_dict())
        return receipt

    def _find(self, app: str, collection: str, resource_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.world[app][collection]
                if item.get("id") == resource_id
            ),
            None,
        )

    def _required(self, app: str, collection: str, resource_id: str) -> dict[str, Any]:
        resource = self._find(app, collection, resource_id)
        if resource is None:
            raise KeyError(f"unknown {app} {collection} id: {resource_id}")
        return resource
