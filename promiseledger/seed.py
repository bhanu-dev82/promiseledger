from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


NOW = "2026-09-13"


def ticket(
    suffix: int,
    *,
    status: str,
    due_date: str,
    proof: str = "merged_pr",
    customer_id: str | None = None,
    customer_name: str = "Acme Owner",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"lin-{suffix}",
        "key": f"PL-{suffix}",
        "title": title or f"Promise {suffix}",
        "status": status,
        "due_date": due_date,
        "required_proof": proof,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "comments": [],
    }


def contact(customer_id: str, name: str, email: str, suffix: str = "") -> dict[str, Any]:
    return {
        "id": f"contact-{customer_id}{suffix}",
        "customer_id": customer_id,
        "name": name,
        "email": email,
    }


def merged_pr(suffix: int, key: str) -> dict[str, Any]:
    return {
        "id": f"gh-pr-{suffix}",
        "number": suffix,
        "title": f"Deliver {key}",
        "body": f"Implements exact commitment {key}.",
        "merged": True,
        "merged_at": "2026-09-10T12:00:00Z",
        "url": f"https://github.example/promiseledger/pull/{suffix}",
    }


def sent_message(
    suffix: int, key: str, customer_id: str, recipient: str
) -> dict[str, Any]:
    return {
        "id": f"gmail-sent-{suffix}",
        "ticket_key": key,
        "customer_id": customer_id,
        "to": recipient,
        "subject": f"Delivered: {key}",
        "body": f"The commitment tracked as {key} is complete.",
        "labels": ["SENT"],
    }


def world(
    scenario_id: str,
    tickets: list[dict[str, Any]],
    *,
    prs: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    protected: list[str] | None = None,
    expected: str = "pass",
    faults: dict[str, Any] | None = None,
    slack_messages: list[dict[str, Any]] | None = None,
    replay: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario": {
            "id": scenario_id,
            "now": NOW,
            "expected_grader": expected,
            "protected_ticket_ids": protected or [],
            "replay": replay,
        },
        "linear": {"tickets": tickets},
        "github": {"pull_requests": prs or []},
        "gmail": {
            "contacts": contacts or [],
            "messages": messages or [],
            "drafts": [],
        },
        "slack": {"channel_id": "C-ENG-UPDATES", "messages": slack_messages or []},
        "faults": faults or {},
        "writes": [],
    }


def scenarios() -> dict[str, dict[str, Any]]:
    healthy = ticket(
        101,
        status="Done",
        due_date="2026-09-10",
        customer_id="cust-101",
        title="Ship SSO audit export",
    )
    desync = ticket(
        102,
        status="Done",
        due_date="2026-09-20",
        customer_id="cust-102",
        title="Deliver retention export",
    )
    done_unrecorded = ticket(
        103,
        status="In Progress",
        due_date="2026-09-20",
        customer_id="cust-103",
        title="Add audit log filters",
    )
    breached = ticket(
        104,
        status="In Progress",
        due_date="2026-09-08",
        customer_id="cust-104",
        title="Fix enterprise CSV timeout",
    )
    missing_notice = ticket(
        105,
        status="Done",
        due_date="2026-09-10",
        proof="sent_email",
        customer_id="cust-105",
        title="Notify customer about migration",
    )
    ambiguous = ticket(
        106,
        status="Done",
        due_date="2026-09-20",
        customer_id=None,
        customer_name="Alex Kim",
        title="Deliver Alex Kim data export",
    )
    neighbor_bad = ticket(
        108,
        status="Done",
        due_date="2026-09-20",
        customer_id="cust-108",
        title="Deliver tenant metrics",
    )
    neighbor_good = ticket(
        208,
        status="Done",
        due_date="2026-09-10",
        customer_id="cust-208",
        title="Protected neighboring tenant export",
    )
    missing_id = ticket(
        109,
        status="Done",
        due_date="2026-09-20",
        customer_id="cust-does-not-exist",
        title="Deliver billing audit",
    )

    contacts = {
        number: contact(f"cust-{number}", f"Customer {number}", f"owner{number}@example.test")
        for number in [101, 102, 103, 104, 105, 107, 108, 208]
    }

    return {
        "s1-healthy": world(
            "s1-healthy",
            [copy.deepcopy(healthy)],
            prs=[merged_pr(101, "PL-101")],
            contacts=[contacts[101]],
            messages=[sent_message(101, "PL-101", "cust-101", "owner101@example.test")],
            protected=["lin-101"],
        ),
        "s2-desync": world(
            "s2-desync",
            [copy.deepcopy(desync)],
            contacts=[contacts[102]],
            slack_messages=[
                {
                    "id": "slack-claim-102",
                    "channel_id": "C-ENG-UPDATES",
                    "text": "PL-102 shipped and is done!",
                    "author": "operator",
                }
            ],
        ),
        "s3-done-unrecorded": world(
            "s3-done-unrecorded",
            [copy.deepcopy(done_unrecorded)],
            prs=[merged_pr(103, "PL-103")],
            contacts=[contacts[103]],
        ),
        "s4-breached": world(
            "s4-breached", [copy.deepcopy(breached)], contacts=[contacts[104]]
        ),
        "s5-missing-notice": world(
            "s5-missing-notice",
            [copy.deepcopy(missing_notice)],
            prs=[merged_pr(105, "PL-105")],
            contacts=[contacts[105]],
        ),
        "s6-ambiguous-contact": world(
            "s6-ambiguous-contact",
            [copy.deepcopy(ambiguous)],
            contacts=[
                contact("cust-alex-one", "Alex Kim", "alex.one@example.test", "-one"),
                contact("cust-alex-two", "Alex Kim", "alex.two@example.test", "-two"),
            ],
            expected="refuse",
        ),
        "s7-replay": world(
            "s7-replay",
            [
                ticket(
                    107,
                    status="In Progress",
                    due_date="2026-09-09",
                    customer_id="cust-107",
                    title="Restore webhook delivery",
                )
            ],
            contacts=[contacts[107]],
            replay=True,
        ),
        "s8-neighbor-freeze": world(
            "s8-neighbor-freeze",
            [copy.deepcopy(neighbor_bad), copy.deepcopy(neighbor_good)],
            prs=[merged_pr(208, "PL-208")],
            contacts=[contacts[108], contacts[208]],
            protected=["lin-208"],
        ),
        "s9-missing-id": world(
            "s9-missing-id",
            [copy.deepcopy(missing_id)],
            contacts=[],
            expected="refuse",
        ),
        "s10-silent-200": world(
            "s10-silent-200",
            [copy.deepcopy(desync)],
            contacts=[contacts[102]],
            expected="fail",
            faults={
                "linear.update_status": {
                    "mode": "http_200_noop",
                    "targets": ["lin-102"],
                }
            },
            slack_messages=[
                {
                    "id": "slack-claim-102",
                    "channel_id": "C-ENG-UPDATES",
                    "text": "PL-102 shipped and is done!",
                    "author": "operator",
                }
            ],
        ),
    }


def write_fixtures(root: str | Path) -> None:
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in scenarios().items():
        (destination / f"{name}.json").write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def load_scenario(name: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(scenarios()[name])
    except KeyError as error:
        choices = ", ".join(sorted(scenarios()))
        raise ValueError(f"unknown scenario {name!r}; choose one of: {choices}") from error
