from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import date
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from promiseledger.models import ApiError, ForbiddenOperation, WriteReceipt


TraceFn = Callable[..., None]


class LiveAdapter:
    """Least-privilege adapters for disposable Linear/GitHub/Gmail/Slack workspaces."""

    def __init__(
        self,
        *,
        linear_key: str,
        linear_team_id: str,
        linear_status_ids: dict[str, str],
        allowed_ticket_ids: set[str],
        github_token: str,
        github_repository: str,
        gmail_token: str,
        slack_token: str,
        slack_channel_id: str,
        trace: TraceFn | None = None,
    ):
        self.linear_key = linear_key
        self.linear_team_id = linear_team_id
        self.linear_status_ids = linear_status_ids
        self.allowed_ticket_ids = allowed_ticket_ids
        self.github_token = github_token
        self.github_repository = github_repository
        self.gmail_token = gmail_token
        self.slack_token = slack_token
        self.slack_channel_id = slack_channel_id
        self.trace = trace or (lambda *_args, **_kwargs: None)
        self._contacts: list[dict[str, Any]] = []
        self._draft_ids: dict[str, str] = {}
        self._draft_cache: dict[str, dict[str, Any]] = {}
        self._slack_messages: list[dict[str, Any]] = []
        self._writes: list[dict[str, Any]] = []
        self.unresolved_ticket_ids: set[str] = set()
        self._ticket_keys: set[str] = set()

    @classmethod
    def from_environment(cls, trace: TraceFn | None = None) -> "LiveAdapter":
        required = [
            "LINEAR_API_KEY",
            "LINEAR_TEAM_ID",
            "LINEAR_STATUS_IDS_JSON",
            "PROMISELEDGER_ALLOWED_TICKET_IDS",
            "GITHUB_TOKEN",
            "GITHUB_REPOSITORY",
            "GMAIL_ACCESS_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_CHANNEL_ID",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(f"missing live adapter variables: {', '.join(missing)}")
        try:
            status_ids = json.loads(os.environ["LINEAR_STATUS_IDS_JSON"])
        except json.JSONDecodeError as error:
            raise ValueError("LINEAR_STATUS_IDS_JSON must be valid JSON") from error
        for status in ["Done", "Reopened", "Breached"]:
            if not status_ids.get(status):
                raise ValueError(f"LINEAR_STATUS_IDS_JSON lacks explicit {status!r} UUID")
        return cls(
            linear_key=os.environ["LINEAR_API_KEY"],
            linear_team_id=os.environ["LINEAR_TEAM_ID"],
            linear_status_ids=status_ids,
            allowed_ticket_ids={
                value.strip()
                for value in os.environ["PROMISELEDGER_ALLOWED_TICKET_IDS"].split(",")
                if value.strip()
            },
            github_token=os.environ["GITHUB_TOKEN"],
            github_repository=os.environ["GITHUB_REPOSITORY"],
            gmail_token=os.environ["GMAIL_ACCESS_TOKEN"],
            slack_token=os.environ["SLACK_BOT_TOKEN"],
            slack_channel_id=os.environ["SLACK_CHANNEL_ID"],
            trace=trace,
        )

    def snapshot(self) -> dict[str, Any]:
        tickets = self.list_tickets()
        pull_requests = self.list_pull_requests()
        messages = self.list_sent_messages()
        self._discover_existing_drafts(tickets)
        drafts = [draft for key in self._draft_ids if (draft := self.get_draft(key))]
        slack_messages = self.list_slack_messages()
        return {
            "schema_version": 1,
            "scenario": {
                "id": "live",
                "now": date.today().isoformat(),
                "expected_grader": "pass",
                "protected_ticket_ids": [],
                "replay": False,
            },
            "linear": {"tickets": tickets},
            "github": {"pull_requests": pull_requests},
            "gmail": {
                "contacts": self.list_contacts(),
                "messages": messages,
                "drafts": drafts,
            },
            "slack": {
                "channel_id": self.slack_channel_id,
                "messages": slack_messages,
            },
            "faults": {},
            "writes": list(self._writes),
        }

    def list_tickets(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self._linear(
                """
            query PromiseLedgerIssues($teamId: ID!, $after: String) {
              issues(filter: {team: {id: {eq: $teamId}}}, first: 100, after: $after) {
                nodes {
                  id identifier title description dueDate
                  state { name }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
            """,
                {"teamId": self.linear_team_id, "after": after},
            )
            nodes.extend(data["issues"]["nodes"])
            page_info = data["issues"]["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]
        tickets: list[dict[str, Any]] = []
        contacts: dict[tuple[str, str], dict[str, Any]] = {}
        for node in nodes:
            if node["id"] not in self.allowed_ticket_ids:
                continue
            description = node.get("description") or ""
            proof = self._description_value(description, "PromiseLedger-Proof")
            customer_id = self._description_value(description, "PromiseLedger-Customer-ID")
            email = self._description_value(description, "PromiseLedger-Customer-Email")
            name = self._description_value(description, "PromiseLedger-Customer-Name") or email or ""
            if customer_id and email:
                contacts[(customer_id, email.lower())] = {
                    "id": f"linear-contact-{customer_id}-{email.lower()}",
                    "customer_id": customer_id,
                    "name": name,
                    "email": email,
                }
            comments = self._list_linear_comments(node["id"])
            tickets.append(
                {
                    "id": node["id"],
                    "key": node["identifier"],
                    "title": node["title"],
                    "status": node["state"]["name"],
                    "due_date": node.get("dueDate"),
                    "required_proof": proof,
                    "customer_id": customer_id,
                    "customer_name": name,
                    "comments": comments,
                }
            )
        self._contacts = list(contacts.values())
        self._ticket_keys = {ticket["key"] for ticket in tickets}
        self.unresolved_ticket_ids = self.allowed_ticket_ids - {ticket["id"] for ticket in tickets}
        self.trace("read", app="linear", action="list_tickets", count=len(tickets))
        return tickets

    def list_pull_requests(self) -> list[dict[str, Any]]:
        response: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request_json(
                "GET",
                f"https://api.github.com/repos/{self.github_repository}/pulls?state=all&per_page=100&page={page}",
                headers={
                    "Authorization": f"Bearer {self.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            )
            response.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        result: list[dict[str, Any]] = []
        for item in response:
            body = item.get("body") or ""
            linked_ticket_keys = sorted(
                key
                for key in self._ticket_keys
                if re.search(
                    rf"(?<![A-Z0-9-]){re.escape(key)}(?![A-Z0-9-])",
                    body.upper(),
                )
            )
            if not linked_ticket_keys:
                continue
            result.append(
                {
                    "id": str(item["id"]),
                    "number": item["number"],
                    "linked_ticket_keys": linked_ticket_keys,
                    "merged": item.get("merged_at") is not None,
                    "merged_at": item.get("merged_at"),
                    "url": item.get("html_url", ""),
                }
            )
        self.trace("read", app="github", action="list_pull_requests", count=len(result))
        return result

    def list_sent_messages(self) -> list[dict[str, Any]]:
        message_refs: dict[str, dict[str, Any]] = {}
        for ticket_key in sorted(self._ticket_keys):
            page_token: str | None = None
            while True:
                parameters = {
                    "labelIds": "SENT",
                    "maxResults": 100,
                    "q": f'"{ticket_key}"',
                }
                if page_token:
                    parameters["pageToken"] = page_token
                listing = self._gmail_get("messages?" + urlencode(parameters))
                message_refs.update(
                    {item["id"]: item for item in listing.get("messages", [])}
                )
                page_token = listing.get("nextPageToken")
                if not page_token:
                    break
        messages: list[dict[str, Any]] = []
        for item in message_refs.values():
            payload = self._gmail_get(f"messages/{quote(item['id'])}?format=full")
            headers = self._headers(payload)
            message_text = self._message_text(payload)
            searchable = f"{headers.get('subject', '')}\n{message_text}"
            found_keys = set(
                re.findall(
                    r"(?<![A-Z0-9-])([A-Z][A-Z0-9]*-[1-9][0-9]*)(?![A-Z0-9-])",
                    searchable.upper(),
                )
            ) & self._ticket_keys
            recipient_addresses = self._recipient_addresses(headers)
            matching_contacts = [
                contact
                for contact in self._contacts
                if contact["email"].strip().lower() in recipient_addresses
            ]
            exact_recipient = len(matching_contacts) == 1 and recipient_addresses == {
                matching_contacts[0]["email"].strip().lower()
            }
            ambiguous = len(found_keys) != 1 or not exact_recipient
            messages.append(
                {
                    "id": payload["id"],
                    "ticket_key": next(iter(found_keys)) if len(found_keys) == 1 else None,
                    "customer_id": (
                        matching_contacts[0]["customer_id"] if not ambiguous else None
                    ),
                    "to": matching_contacts[0]["email"] if not ambiguous else "",
                    "subject": "",
                    "body": "",
                    "labels": payload.get("labelIds", []),
                    "url": f"https://mail.google.com/mail/u/0/#sent/{payload['id']}",
                    "join_ambiguous": ambiguous,
                }
            )
        self.trace("read", app="gmail", action="list_sent_messages", count=len(messages))
        return messages

    def list_contacts(self) -> list[dict[str, Any]]:
        self.trace("read", app="gmail", action="list_contacts", count=len(self._contacts))
        return list(self._contacts)

    def list_slack_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            parameters = {"channel": self.slack_channel_id, "limit": 100}
            if cursor:
                parameters["cursor"] = cursor
            response = self._request_json(
                "GET",
                "https://slack.com/api/conversations.history?" + urlencode(parameters),
                headers={"Authorization": f"Bearer {self.slack_token}"},
            )
            if not response.get("ok"):
                raise ApiError(
                    "Slack conversations.history failed: "
                    f"{response.get('error', 'unknown error')}"
                )
            normalized = (
                self._normalize_slack_message(item) for item in response.get("messages", [])
            )
            messages.extend(
                message
                for message in normalized
                if (message.get("idempotency_key") or "").startswith(
                    "promiseledger:digest:"
                )
            )
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        self._slack_messages = messages
        self.trace("read", app="slack", action="list_messages", count=len(messages))
        return list(messages)

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        # Re-listing deliberately verifies Linear state rather than trusting a mutation response.
        ticket = next((item for item in self.list_tickets() if item["id"] == ticket_id), None)
        self.trace("readback", app="linear", action="get_ticket", target_id=ticket_id, found=bool(ticket))
        return ticket

    def get_draft(self, synthetic_id: str) -> dict[str, Any] | None:
        gmail_id = self._draft_ids.get(synthetic_id)
        if not gmail_id:
            return self._draft_cache.get(synthetic_id)
        payload = self._gmail_get(f"drafts/{quote(gmail_id)}?format=metadata")
        message = payload.get("message", {})
        headers = self._headers(message)
        cached = self._draft_cache[synthetic_id]
        actual_addresses = self._recipient_addresses(headers)
        if actual_addresses != {cached["to"].lower()}:
            raise ApiError(f"existing draft {synthetic_id} has a different recipient")
        draft = {
            **cached,
            "to": cached["to"],
            "subject": headers.get("subject", cached["subject"]),
            "labels": message.get("labelIds", ["DRAFT"]),
        }
        self.trace("readback", app="gmail", action="get_draft", target_id=synthetic_id, found=True)
        return draft

    def update_ticket_status(self, ticket_id: str, status: str, idempotency_key: str) -> WriteReceipt:
        self._assert_allowed(ticket_id)
        state_id = self.linear_status_ids.get(status)
        if not state_id:
            raise ApiError(f"no explicit Linear state UUID configured for {status!r}")
        existing = self.get_ticket(ticket_id)
        if existing is None:
            raise ApiError(f"allowlisted Linear ticket {ticket_id!r} was not found")
        if existing.get("status") == status:
            return WriteReceipt(
                "linear", "update_status", ticket_id, idempotency_key, 200, False, True
            )
        data = self._linear(
            "mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }",
            {"id": ticket_id, "input": {"stateId": state_id}},
        )
        if not data["issueUpdate"]["success"]:
            raise ApiError("Linear issueUpdate returned success=false")
        return self._receipt("linear", "update_status", ticket_id, idempotency_key)

    def add_ticket_comment(self, ticket_id: str, body: str, classification: str, idempotency_key: str) -> WriteReceipt:
        self._assert_allowed(ticket_id)
        existing = self.get_ticket(ticket_id)
        if existing and any(idempotency_key in comment.get("body", "") for comment in existing["comments"]):
            return WriteReceipt("linear", "add_comment", ticket_id, idempotency_key, 200, False, True)
        tagged_body = f"{body}\n\n<!-- {idempotency_key} -->"
        data = self._linear(
            "mutation AddComment($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
            {"input": {"issueId": ticket_id, "body": tagged_body}},
        )
        if not data["commentCreate"]["success"]:
            raise ApiError("Linear commentCreate returned success=false")
        return self._receipt("linear", "add_comment", ticket_id, idempotency_key)

    def create_draft(self, ticket_id: str, customer_id: str, recipient: str, subject: str, body: str, idempotency_key: str) -> WriteReceipt:
        self._assert_allowed(ticket_id)
        synthetic_id = f"draft-{ticket_id}"
        self._discover_one_draft(ticket_id, customer_id, recipient, idempotency_key)
        if synthetic_id in self._draft_ids:
            return WriteReceipt("gmail", "create_draft", synthetic_id, idempotency_key, 200, False, True)
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = f"<{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)}@promiseledger.invalid>"
        message.set_content(f"PromiseLedger-ID: {idempotency_key}\n\n{body}")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        response = self._request_json(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={"Authorization": f"Bearer {self.gmail_token}"},
            body={"message": {"raw": raw}},
        )
        self._draft_ids[synthetic_id] = response["id"]
        self._draft_cache[synthetic_id] = {
            "id": synthetic_id,
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "to": recipient,
            "subject": subject,
            "body": body,
            "labels": ["DRAFT"],
            "approved": False,
            "idempotency_key": idempotency_key,
        }
        return self._receipt("gmail", "create_draft", synthetic_id, idempotency_key)

    def post_digest(self, text: str, idempotency_key: str) -> WriteReceipt:
        client_message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
        existing = [
            message
            for message in self.list_slack_messages()
            if message.get("idempotency_key") == idempotency_key
            or message.get("client_msg_id") == client_message_id
        ]
        if len(existing) > 1:
            raise ApiError(f"multiple Slack digests found for immutable key {idempotency_key}")
        if existing:
            if existing[0].get("text") != text:
                raise ApiError(f"Slack digest key {idempotency_key} has different content")
            return WriteReceipt(
                "slack",
                "post_digest",
                self.slack_channel_id,
                idempotency_key,
                200,
                False,
                True,
            )
        marked_text = f"{text}\n\nPromiseLedger-ID: {idempotency_key}"
        response = self._request_json(
            "POST",
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.slack_token}"},
            body={
                "channel": self.slack_channel_id,
                "text": marked_text,
                "client_msg_id": client_message_id,
            },
        )
        if not response.get("ok"):
            raise ApiError(f"Slack chat.postMessage failed: {response.get('error', 'unknown error')}")
        persisted = [
            message
            for message in self.list_slack_messages()
            if message.get("idempotency_key") == idempotency_key
            or message.get("client_msg_id") == client_message_id
        ]
        if len(persisted) != 1 or persisted[0].get("text") != text:
            raise ApiError("Slack returned success but digest read-back did not match")
        return self._receipt("slack", "post_digest", self.slack_channel_id, idempotency_key)

    def send_email(self, *_args: Any, **_kwargs: Any) -> None:
        raise ForbiddenOperation("Gmail send is intentionally absent")

    def mutate_github(self, *_args: Any, **_kwargs: Any) -> None:
        raise ForbiddenOperation("GitHub live credentials are read-only by policy")

    def _linear(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json(
            "POST",
            "https://api.linear.app/graphql",
            headers={"Authorization": self.linear_key},
            body={"query": query, "variables": variables},
        )
        if response.get("errors"):
            messages = "; ".join(error.get("message", "unknown") for error in response["errors"])
            raise ApiError(f"Linear GraphQL returned HTTP success with errors: {messages}")
        if "data" not in response:
            raise ApiError("Linear GraphQL response omitted data")
        return response["data"]

    def _list_linear_comments(self, ticket_id: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self._linear(
                """
                query PromiseLedgerComments($id: String!, $after: String) {
                  issue(id: $id) {
                    comments(first: 100, after: $after) {
                      nodes { id body user { email } }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                {"id": ticket_id, "after": after},
            )
            connection = data["issue"]["comments"]
            comments.extend(
                self._normalize_linear_comment(item) for item in connection["nodes"]
            )
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"]["endCursor"]
        return comments

    def _gmail_get(self, suffix: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/{suffix}",
            headers={"Authorization": f"Bearer {self.gmail_token}"},
        )

    @staticmethod
    def _request_json(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        request_headers = {"Accept": "application/json", **headers}
        data = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(url, method=method, headers=request_headers, data=data)
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode()
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise ApiError(f"{method} {url} returned HTTP {error.code}: {detail}") from error
        try:
            return json.loads(payload)
        except json.JSONDecodeError as error:
            raise ApiError(f"{method} {url} returned invalid JSON") from error

    def _receipt(self, app: str, action: str, target_id: str, idempotency_key: str) -> WriteReceipt:
        receipt = WriteReceipt(app, action, target_id, idempotency_key, 200, True)
        self._writes.append(receipt.as_dict())
        self.trace("write_receipt", **receipt.as_dict())
        return receipt

    def _discover_existing_drafts(self, tickets: list[dict[str, Any]]) -> None:
        contacts = {contact["customer_id"]: contact for contact in self._contacts}
        for ticket in tickets:
            customer_id = ticket.get("customer_id")
            contact = contacts.get(customer_id)
            if not customer_id or not contact:
                continue
            idempotency_key = f"promiseledger:{ticket['id']}:gmail-draft:v1:v1"
            self._discover_one_draft(
                ticket["id"], customer_id, contact["email"], idempotency_key
            )

    def _discover_one_draft(
        self, ticket_id: str, customer_id: str, recipient: str, idempotency_key: str
    ) -> None:
        synthetic_id = f"draft-{ticket_id}"
        if synthetic_id in self._draft_ids:
            return
        query = "drafts?" + urlencode({"q": f'"PromiseLedger-ID: {idempotency_key}"'})
        listing = self._gmail_get(query)
        matches = listing.get("drafts", [])
        if len(matches) > 1:
            raise ApiError(f"multiple live Gmail drafts found for immutable key {idempotency_key}")
        if not matches:
            return
        gmail_id = matches[0]["id"]
        payload = self._gmail_get(f"drafts/{quote(gmail_id)}?format=metadata")
        headers = self._headers(payload.get("message", {}))
        actual_addresses = self._recipient_addresses(headers)
        if actual_addresses != {recipient.lower()}:
            raise ApiError(f"existing draft for {ticket_id} has a different recipient")
        self._draft_ids[synthetic_id] = gmail_id
        self._draft_cache[synthetic_id] = {
            "id": synthetic_id,
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "to": recipient,
            "subject": headers.get("subject", ""),
            "body": "existing live draft",
            "labels": payload.get("message", {}).get("labelIds", ["DRAFT"]),
            "approved": False,
            "idempotency_key": idempotency_key,
        }

    def _assert_allowed(self, ticket_id: str) -> None:
        if ticket_id not in self.allowed_ticket_ids:
            raise ForbiddenOperation(f"ticket {ticket_id!r} is outside the live write allowlist")

    @staticmethod
    def _description_value(description: str, key: str) -> str | None:
        match = re.search(rf"(?mi)^{re.escape(key)}:\s*(\S.*?)\s*$", description)
        return match.group(1).strip() if match else None

    @staticmethod
    def _normalize_linear_comment(comment: dict[str, Any]) -> dict[str, Any]:
        body = comment.get("body", "")
        classification = None
        match = re.search(r"classification=(HEALTHY|DONE_UNRECORDED|BREACHED|DESYNC)", body)
        if match:
            classification = match.group(1)
        human = body.startswith("[PromiseLedger human-waiver]")
        return {
            "id": comment["id"],
            "body": body,
            "author_type": "human" if human else "agent" if body.startswith("[PromiseLedger]") else "external",
            "kind": "human_waiver" if human else "evidence" if classification else "external",
            "classification": classification,
            "approver": (comment.get("user") or {}).get("email"),
        }

    def _normalize_slack_message(self, message: dict[str, Any]) -> dict[str, Any]:
        raw_text = message.get("text", "")
        marker = re.search(
            r"(?m)^PromiseLedger-ID:\s*(promiseledger:digest:[A-Za-z0-9._:-]+)\s*$",
            raw_text,
        )
        text = raw_text
        idempotency_key = None
        if marker:
            idempotency_key = marker.group(1)
            text = (raw_text[: marker.start()] + raw_text[marker.end() :]).strip()
        return {
            "id": message.get("ts"),
            "channel_id": self.slack_channel_id,
            "text": text,
            "idempotency_key": idempotency_key,
            "client_msg_id": message.get("client_msg_id"),
            "author": message.get("user") or message.get("bot_id"),
        }

    @staticmethod
    def _headers(payload: dict[str, Any]) -> dict[str, str]:
        return {
            item["name"].lower(): item["value"]
            for item in payload.get("payload", {}).get("headers", [])
        }

    @staticmethod
    def _recipient_addresses(headers: dict[str, str]) -> set[str]:
        values = [
            value
            for value in [
                headers.get("to", ""),
                headers.get("cc", ""),
                headers.get("bcc", ""),
            ]
            if value
        ]
        return {
            address.lower()
            for _name, address in getaddresses(values)
            if address
        }

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        chunks: list[str] = []

        def visit(part: dict[str, Any]) -> None:
            data = part.get("body", {}).get("data")
            if data and part.get("mimeType", "text/plain").startswith("text/"):
                padding = "=" * (-len(data) % 4)
                try:
                    chunks.append(
                        base64.urlsafe_b64decode(data + padding).decode(errors="replace")
                    )
                except (ValueError, TypeError):
                    pass
            for child in part.get("parts", []):
                visit(child)

        visit(message.get("payload", {}))
        return "\n".join(chunks) or message.get("snippet", "")
