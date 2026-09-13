from __future__ import annotations

import copy
import unittest

from promiseledger.adapters.live import LiveAdapter
from promiseledger.models import ApiError, ForbiddenOperation


def live_adapter() -> LiveAdapter:
    return LiveAdapter(
        linear_key="test",
        linear_team_id="team",
        linear_status_ids={"Done": "done", "Reopened": "open", "Breached": "breach"},
        allowed_ticket_ids={"lin-1"},
        github_token="test",
        github_repository="owner/repo",
        gmail_token="test",
        slack_token="test",
        slack_channel_id="C1",
    )


class LiveContractTests(unittest.TestCase):
    def test_linear_http_200_graphql_errors_are_failures(self) -> None:
        adapter = live_adapter()
        adapter._request_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "errors": [{"message": "mutation did not apply"}],
            "data": {"issueUpdate": {"success": False}},
        }
        with self.assertRaisesRegex(ApiError, "HTTP success with errors"):
            adapter._linear("mutation Test { ok }", {})

    def test_live_allowlist_blocks_unknown_ticket_before_request(self) -> None:
        adapter = live_adapter()
        with self.assertRaises(ForbiddenOperation):
            adapter.update_ticket_status("invented-ticket", "Done", "key")

    def test_live_status_replay_skips_mutation_when_state_already_matches(self) -> None:
        adapter = live_adapter()
        adapter.get_ticket = lambda _ticket_id: {"id": "lin-1", "status": "Reopened"}  # type: ignore[method-assign]
        adapter._linear = lambda *_args, **_kwargs: self.fail("unexpected Linear mutation")  # type: ignore[method-assign]
        receipt = adapter.update_ticket_status("lin-1", "Reopened", "status-key")
        self.assertTrue(receipt.duplicate)
        self.assertFalse(receipt.applied)

    def test_live_has_no_authorized_send_or_github_write(self) -> None:
        adapter = live_adapter()
        with self.assertRaises(ForbiddenOperation):
            adapter.send_email("anything")
        with self.assertRaises(ForbiddenOperation):
            adapter.mutate_github("anything")

    def test_live_gmail_join_uses_exact_mailbox_not_substring(self) -> None:
        adapter = live_adapter()
        adapter._contacts = [
            {
                "id": "contact-1",
                "customer_id": "cust-1",
                "name": "Owner",
                "email": "owner@example.test",
            }
        ]
        adapter._ticket_keys = {"PL-1"}
        responses = iter(
            [
                {"messages": [{"id": "message-1"}]},
                {
                    "id": "message-1",
                    "labelIds": ["SENT"],
                    "snippet": "PL-1 is complete",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "PL-1 delivered"},
                            {"name": "To", "value": "notowner@example.test"},
                        ]
                    },
                },
            ]
        )
        adapter._gmail_get = lambda _suffix: next(responses)  # type: ignore[method-assign]
        message = adapter.list_sent_messages()[0]
        self.assertIsNone(message["customer_id"])
        self.assertTrue(message["join_ambiguous"])

    def test_live_gmail_join_rejects_additional_recipients(self) -> None:
        adapter = live_adapter()
        adapter._contacts = [
            {
                "id": "contact-1",
                "customer_id": "cust-1",
                "name": "Owner",
                "email": "owner@example.test",
            }
        ]
        adapter._ticket_keys = {"PL-1"}
        responses = iter(
            [
                {"messages": [{"id": "message-1"}]},
                {
                    "id": "message-1",
                    "labelIds": ["SENT"],
                    "snippet": "PL-1 is complete",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "PL-1 delivered"},
                            {"name": "To", "value": "owner@example.test"},
                            {"name": "Cc", "value": "attacker@example.test"},
                        ]
                    },
                },
            ]
        )
        adapter._gmail_get = lambda _suffix: next(responses)  # type: ignore[method-assign]
        message = adapter.list_sent_messages()[0]
        self.assertTrue(message["join_ambiguous"])
        self.assertEqual("", message["to"])

    def test_existing_draft_with_wrong_recipient_is_rejected(self) -> None:
        adapter = live_adapter()
        responses = iter(
            [
                {"drafts": [{"id": "gmail-draft-1"}]},
                {
                    "message": {
                        "labelIds": ["DRAFT"],
                        "payload": {
                            "headers": [
                                {"name": "To", "value": "attacker@example.test"},
                            ]
                        },
                    }
                },
            ]
        )
        adapter._gmail_get = lambda _suffix: next(responses)  # type: ignore[method-assign]
        with self.assertRaisesRegex(ApiError, "different recipient"):
            adapter._discover_one_draft(
                "lin-1",
                "cust-1",
                "owner@example.test",
                "promiseledger:lin-1:gmail-draft:v1:v1",
            )

    def test_slack_digest_replay_is_durable_across_adapter_processes(self) -> None:
        history: list[dict] = [
            {
                "ts": "1710000000.000001",
                "text": "unrelated channel conversation",
                "user": "U1",
            }
        ]
        post_count = 0

        def backend(method: str, url: str, *, headers: dict, body: dict | None = None) -> dict:
            nonlocal post_count
            if "conversations.history" in url:
                return {
                    "ok": True,
                    "messages": copy.deepcopy(history),
                    "response_metadata": {"next_cursor": ""},
                }
            if "chat.postMessage" in url:
                post_count += 1
                assert body is not None
                history.append(
                    {
                        "ts": "1720000000.000001",
                        "text": body["text"],
                        "client_msg_id": body["client_msg_id"],
                        "bot_id": "B1",
                    }
                )
                return {"ok": True, "ts": "1720000000.000001"}
            raise AssertionError(f"unexpected request: {method} {url}")

        key = "promiseledger:digest:1234abcd:v1"
        first = live_adapter()
        first._request_json = backend  # type: ignore[method-assign]
        self.assertTrue(first.post_digest("PromiseLedger digest for PL-1", key).applied)

        fresh = live_adapter()
        fresh._request_json = backend  # type: ignore[method-assign]
        replay = fresh.post_digest("PromiseLedger digest for PL-1", key)
        self.assertTrue(replay.duplicate)
        self.assertFalse(replay.applied)
        self.assertEqual(1, post_count)
        self.assertEqual(key, fresh.list_slack_messages()[0]["idempotency_key"])

    def test_live_github_snapshot_omits_unrelated_prs_and_body_text(self) -> None:
        adapter = live_adapter()
        adapter._ticket_keys = {"PL-1"}
        adapter._request_json = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
            {
                "id": 1,
                "number": 1,
                "body": "Implements PL-1 with private implementation details",
                "merged_at": "2026-09-10T12:00:00Z",
                "html_url": "https://github.example/pull/1",
            },
            {
                "id": 2,
                "number": 2,
                "body": "Unrelated private work",
                "merged_at": None,
                "html_url": "https://github.example/pull/2",
            },
        ]
        pull_requests = adapter.list_pull_requests()
        self.assertEqual(1, len(pull_requests))
        self.assertEqual(["PL-1"], pull_requests[0]["linked_ticket_keys"])
        self.assertNotIn("body", pull_requests[0])

    def test_slack_http_success_without_persistence_is_rejected(self) -> None:
        adapter = live_adapter()

        def backend(method: str, url: str, *, headers: dict, body: dict | None = None) -> dict:
            if "conversations.history" in url:
                return {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}}
            if "chat.postMessage" in url:
                return {"ok": True, "ts": "1720000000.000001"}
            raise AssertionError(f"unexpected request: {method} {url}")

        adapter._request_json = backend  # type: ignore[method-assign]
        with self.assertRaisesRegex(ApiError, "read-back did not match"):
            adapter.post_digest(
                "PromiseLedger digest for PL-1",
                "promiseledger:digest:1234abcd:v1",
            )


if __name__ == "__main__":
    unittest.main()
