from __future__ import annotations

import copy
from typing import Any

from promiseledger.adapters.fixture import FixtureAdapter
from promiseledger.agent import PromiseLedgerAgent
from promiseledger.grader import grade_worlds
from promiseledger.seed import load_scenario, scenarios


SCENARIO_CARDS: dict[str, dict[str, str]] = {
    "s1-healthy": {
        "title": "Healthy Done",
        "blurb": "Merged PR + sent notice already exist. Agent must prove a no-op.",
        "expect": "PASS · no writes",
    },
    "s2-desync": {
        "title": "Claimed-but-not-done",
        "blurb": "Linear says Done. GitHub has no merged PR. Slack already claimed it shipped.",
        "expect": "PASS · reopen + unsent Gmail draft",
    },
    "s3-done-unrecorded": {
        "title": "PR merged, ticket still open",
        "blurb": "Proof exists in GitHub; Linear never closed. Reconcile the ticket.",
        "expect": "PASS · mark Done with evidence",
    },
    "s4-breached": {
        "title": "Overdue promise",
        "blurb": "Due date passed, no backing PR. Bounded breach + draft, never send.",
        "expect": "PASS · Breached + draft",
    },
    "s5-missing-notice": {
        "title": "Missing customer notice",
        "blurb": "Policy requires a sent email. None exists. Reopen and draft.",
        "expect": "PASS · reopen + draft",
    },
    "s6-ambiguous-contact": {
        "title": "Two people named Alex Kim",
        "blurb": "Same display name, no customer ID. Names are never join keys.",
        "expect": "REFUSE · zero writes",
    },
    "s7-replay": {
        "title": "Run it twice",
        "blurb": "Second pass must be byte-identical. Idempotency is the product.",
        "expect": "PASS · replay adds nothing",
    },
    "s8-neighbor-freeze": {
        "title": "Don't touch the neighbor",
        "blurb": "One bad ticket beside a healthy protected tenant. Neighbor hash frozen.",
        "expect": "PASS · neighbor unchanged",
    },
    "s9-missing-id": {
        "title": "Unknown customer ID",
        "blurb": "customer_id points at nobody. Inventing a recipient is forbidden.",
        "expect": "REFUSE · zero writes",
    },
    "s10-silent-200": {
        "title": "HTTP 200, world unchanged",
        "blurb": "Linear returns 200 but does not reopen. Agent must not claim success.",
        "expect": "EXPECTED FAIL · grader catches it",
    },
}


def list_scenarios() -> list[dict[str, str]]:
    return [
        {"id": name, **SCENARIO_CARDS[name]}
        for name in scenarios()
        if name in SCENARIO_CARDS
    ]


def preview_scenario(name: str) -> dict[str, Any]:
    initial = load_scenario(name)
    card = SCENARIO_CARDS.get(name, {"title": name, "blurb": "", "expect": ""})
    return {
        "scenario": name,
        "title": card["title"],
        "blurb": card["blurb"],
        "expect": card["expect"],
        "initial": _public_world(initial),
    }


def run_scenario(name: str) -> dict[str, Any]:
    initial = load_scenario(name)
    adapter = FixtureAdapter(copy.deepcopy(initial))
    report = PromiseLedgerAgent(adapter, f"demo-{name}").run()
    final = adapter.snapshot()
    replay = None
    if initial["scenario"].get("replay"):
        replay_adapter = FixtureAdapter(copy.deepcopy(final))
        PromiseLedgerAgent(replay_adapter, f"demo-{name}-replay").run()
        replay = replay_adapter.snapshot()
    grade = grade_worlds(initial, final, replay)
    card = SCENARIO_CARDS.get(name, {"title": name, "blurb": "", "expect": ""})
    return {
        "scenario": name,
        "title": card["title"],
        "blurb": card["blurb"],
        "expect": card["expect"],
        "initial": _public_world(initial),
        "final": _public_world(final),
        "replay": _public_world(replay) if replay is not None else None,
        "report": report.as_dict(),
        "grade": grade.as_dict(),
        "grade_markdown": grade.markdown(),
        "steps": _steps(report.as_dict(), grade.passed),
    }


def _steps(report: dict[str, Any], grade_passed: bool) -> list[dict[str, str]]:
    steps = [{"kind": "read", "text": "Read Linear, GitHub, Gmail, Slack"}]
    if report.get("refused"):
        for reason in report.get("refusal_reasons", []):
            steps.append({"kind": "refuse", "text": reason})
        steps.append({"kind": "done", "text": "Zero writes. World unchanged."})
        return steps
    for result in report.get("results", []):
        steps.append(
            {
                "kind": "class",
                "text": f"{result['key']} classified {result['classification']}",
            }
        )
        if not result.get("receipts"):
            steps.append({"kind": "skip", "text": f"{result['key']} already consistent — no writes"})
            continue
        for receipt in result["receipts"]:
            applied = "applied" if receipt.get("applied") else "HTTP 200, mutation missing"
            steps.append(
                {
                    "kind": "write" if receipt.get("applied") else "fault",
                    "text": f"{receipt['app']}.{receipt['action']} → {applied}",
                }
            )
        steps.append(
            {
                "kind": "check" if result.get("verified") else "fault",
                "text": f"Read-back: {result.get('reason') or result['key']}",
            }
        )
    steps.append(
        {
            "kind": "grade" if grade_passed else "fault",
            "text": "Independent grader PASS" if grade_passed else "Independent grader FAIL",
        }
    )
    return steps


def _public_world(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario": world.get("scenario", {}),
        "linear": world.get("linear", {}),
        "github": world.get("github", {}),
        "gmail": world.get("gmail", {}),
        "slack": world.get("slack", {}),
        "faults": world.get("faults", {}),
        "writes": world.get("writes", []),
    }
