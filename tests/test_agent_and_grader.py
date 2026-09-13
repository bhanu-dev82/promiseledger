from __future__ import annotations

import copy
import unittest

from promiseledger.adapters.fixture import FixtureAdapter
from promiseledger.agent import PromiseLedgerAgent
from promiseledger.grader import canonical_hash, grade_worlds
from promiseledger.models import ForbiddenOperation
from promiseledger.seed import load_scenario, scenarios


def run_once(world: dict, run_id: str = "test-run") -> tuple[dict, dict]:
    adapter = FixtureAdapter(world)
    report = PromiseLedgerAgent(adapter, run_id).run()
    return adapter.snapshot(), report.as_dict()


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_meets_its_expected_contract(self) -> None:
        for name, initial in scenarios().items():
            with self.subTest(scenario=name):
                final, agent_report = run_once(initial, name)
                replay = None
                graded_final = final
                if initial["scenario"].get("replay"):
                    replay, _ = run_once(final, name + "-replay")
                grade = grade_worlds(initial, graded_final, replay)
                expected = initial["scenario"]["expected_grader"]
                if expected == "fail":
                    self.assertFalse(grade.passed)
                    self.assertFalse(agent_report["verified"])
                else:
                    self.assertTrue(grade.passed, grade.markdown())

    def test_silent_200_receipt_is_not_accepted_as_success(self) -> None:
        initial = load_scenario("s10-silent-200")
        final, report = run_once(initial)
        status_receipt = next(
            receipt
            for receipt in report["results"][0]["receipts"]
            if receipt["action"] == "update_status"
        )
        self.assertEqual(200, status_receipt["status_code"])
        self.assertFalse(status_receipt["applied"])
        self.assertEqual("Done", final["linear"]["tickets"][0]["status"])
        grade = grade_worlds(initial, final)
        self.assertFalse(grade.groups["G3 desync"].passed)
        self.assertFalse(grade.groups["G6 ledger"].passed)

    def test_comment_readback_requires_the_exact_generated_evidence(self) -> None:
        initial = load_scenario("s2-desync")
        initial["linear"]["tickets"][0]["comments"].append(
            {
                "id": "stale",
                "body": "[PromiseLedger] PL-999 classification=DESYNC",
                "author_type": "agent",
                "kind": "evidence",
                "classification": "DESYNC",
            }
        )
        initial["faults"]["linear.add_comment"] = {
            "mode": "http_200_noop",
            "targets": ["lin-102"],
        }
        final, report = run_once(initial)
        self.assertFalse(report["verified"])
        self.assertEqual("Done", final["linear"]["tickets"][0]["status"])
        self.assertEqual([], final["gmail"]["drafts"])

    def test_refusal_happens_before_every_write(self) -> None:
        for name in ["s6-ambiguous-contact", "s9-missing-id"]:
            with self.subTest(scenario=name):
                initial = load_scenario(name)
                final, report = run_once(initial)
                self.assertTrue(report["refused"])
                self.assertEqual(canonical_hash(initial), canonical_hash(final))
                self.assertEqual([], final["writes"])

    def test_missing_proof_policy_refuses_without_defaulting(self) -> None:
        initial = load_scenario("s2-desync")
        initial["linear"]["tickets"][0]["required_proof"] = None
        final, report = run_once(initial)
        self.assertTrue(report["refused"])
        self.assertIn("required_proof", report["refusal_reasons"][0])
        self.assertEqual(canonical_hash(initial), canonical_hash(final))

    def test_missing_ticket_id_refuses_instead_of_crashing(self) -> None:
        initial = load_scenario("s2-desync")
        del initial["linear"]["tickets"][0]["id"]
        final, report = run_once(initial)
        self.assertTrue(report["refused"])
        self.assertIn("immutable id", report["refusal_reasons"][0])
        self.assertEqual(canonical_hash(initial), canonical_hash(final))

    def test_malformed_or_duplicate_ticket_identity_refuses_without_writes(self) -> None:
        cases = []
        malformed = load_scenario("s2-desync")
        malformed["linear"]["tickets"][0]["key"] = "PL-01"
        cases.append(malformed)
        numeric_id = load_scenario("s2-desync")
        numeric_id["linear"]["tickets"][0]["id"] = 123
        cases.append(numeric_id)
        list_id = load_scenario("s2-desync")
        list_id["linear"]["tickets"][0]["id"] = ["invented"]
        cases.append(list_id)
        numeric_key = load_scenario("s2-desync")
        numeric_key["linear"]["tickets"][0]["key"] = 123
        cases.append(numeric_key)
        duplicate = load_scenario("s2-desync")
        duplicate["linear"]["tickets"].append(
            copy.deepcopy(duplicate["linear"]["tickets"][0])
        )
        cases.append(duplicate)

        for initial in cases:
            with self.subTest(tickets=initial["linear"]["tickets"]):
                final, report = run_once(initial)
                self.assertTrue(report["refused"])
                self.assertEqual([], final["writes"])
                self.assertEqual(canonical_hash(initial), canonical_hash(final))

    def test_conflicting_existing_draft_refuses_before_writes(self) -> None:
        initial = load_scenario("s2-desync")
        initial["gmail"]["drafts"].append(
            {
                "id": "draft-lin-102",
                "ticket_id": "lin-102",
                "customer_id": "cust-102",
                "to": "attacker@example.test",
                "labels": ["DRAFT"],
                "idempotency_key": "promiseledger:lin-102:gmail-draft:v1:v1",
            }
        )
        final, report = run_once(initial)
        self.assertTrue(report["refused"])
        self.assertEqual([], final["writes"])
        self.assertEqual(canonical_hash(initial), canonical_hash(final))

    def test_partial_desync_bundle_is_completed_on_retry(self) -> None:
        initial = load_scenario("s2-desync")
        partial_adapter = FixtureAdapter(initial)
        partial_adapter.add_ticket_comment(
            "lin-102",
            "[PromiseLedger] PL-102 classification=DESYNC; planned_transition=Reopened.",
            "DESYNC",
            "promiseledger:lin-102:evidence:desync:v1",
        )
        partial_adapter.update_ticket_status(
            "lin-102",
            "Reopened",
            "promiseledger:lin-102:status:reopened:v1",
        )
        partial = partial_adapter.snapshot()

        final, report = run_once(partial, "retry")
        self.assertTrue(report["verified"])
        self.assertEqual(1, len(final["gmail"]["drafts"]))
        self.assertEqual(2, len(final["slack"]["messages"]))
        self.assertTrue(grade_worlds(initial, final).passed)
        self.assertTrue(grade_worlds(partial, final).passed)

    def test_retry_adds_only_missing_digest_for_partial_bundle(self) -> None:
        initial = load_scenario("s2-desync")
        partial_adapter = FixtureAdapter(initial)
        partial_adapter.add_ticket_comment(
            "lin-102",
            "[PromiseLedger] PL-102 classification=DESYNC; planned_transition=Reopened.",
            "DESYNC",
            "promiseledger:lin-102:evidence:desync:v1",
        )
        partial_adapter.update_ticket_status(
            "lin-102",
            "Reopened",
            "promiseledger:lin-102:status:reopened:v1",
        )
        partial_adapter.create_draft(
            "lin-102",
            "cust-102",
            "owner102@example.test",
            "Draft for human review: PL-102 commitment update",
            "UNSENT — human review required",
            "promiseledger:lin-102:gmail-draft:v1:v1",
        )
        partial = partial_adapter.snapshot()

        self.assertFalse(grade_worlds(partial, partial).groups["G3 desync"].passed)
        final, report = run_once(partial, "digest-retry")
        self.assertTrue(report["verified"])
        self.assertEqual(1, len(final["gmail"]["drafts"]))
        self.assertEqual(2, len(final["slack"]["messages"]))
        self.assertTrue(grade_worlds(partial, final).passed)

    def test_slack_silent_200_fails_agent_readback_and_grader(self) -> None:
        initial = load_scenario("s2-desync")
        initial["faults"]["slack.post_digest"] = {
            "mode": "http_200_noop",
            "targets": ["C-ENG-UPDATES"],
        }
        final, report = run_once(initial)
        self.assertFalse(report["verified"])
        self.assertFalse(grade_worlds(initial, final).groups["G3 desync"].passed)

    def test_batch_digest_waits_for_every_actionable_readback(self) -> None:
        initial = load_scenario("s2-desync")
        breach = load_scenario("s4-breached")
        initial["linear"]["tickets"].extend(breach["linear"]["tickets"])
        initial["gmail"]["contacts"].extend(breach["gmail"]["contacts"])
        initial["faults"]["linear.update_status"] = {
            "mode": "http_200_noop",
            "targets": ["lin-102"],
        }

        final, report = run_once(initial)
        self.assertFalse(report["verified"])
        self.assertEqual(initial["slack"]["messages"], final["slack"]["messages"])

    def test_grader_rejects_misaddressed_sent_proof(self) -> None:
        world = load_scenario("s1-healthy")
        world["linear"]["tickets"][0]["required_proof"] = "sent_email"
        world["gmail"]["messages"][0]["to"] = "attacker@example.test"
        report = grade_worlds(world, copy.deepcopy(world))
        self.assertFalse(report.groups["G3 desync"].passed)

    def test_grader_rejects_waiver_with_misaddressed_draft(self) -> None:
        world = load_scenario("s2-desync")
        world["gmail"]["drafts"].append(
            {
                "id": "draft-lin-102",
                "ticket_id": "lin-102",
                "customer_id": "cust-102",
                "to": "attacker@example.test",
                "labels": ["DRAFT"],
                "approved": True,
            }
        )
        world["linear"]["tickets"][0]["comments"].append(
            {
                "id": "waiver-lin-102",
                "body": (
                    "[PromiseLedger human-waiver] ticket=lin-102 "
                    "draft=draft-lin-102 approver=operator@example.test"
                ),
                "author_type": "human",
                "kind": "human_waiver",
                "draft_id": "draft-lin-102",
                "approver": "operator@example.test",
            }
        )
        report = grade_worlds(world, copy.deepcopy(world))
        self.assertFalse(report.groups["G3 desync"].passed)

    def test_replay_is_byte_identical_and_adds_no_receipts(self) -> None:
        initial = load_scenario("s7-replay")
        first, _ = run_once(initial, "first")
        second, _ = run_once(first, "second")
        self.assertEqual(canonical_hash(first), canonical_hash(second))
        self.assertTrue(grade_worlds(initial, first, second).groups["G8 replay"].passed)

    def test_slack_claim_is_not_treated_as_proof(self) -> None:
        initial = load_scenario("s2-desync")
        self.assertIn("done", initial["slack"]["messages"][0]["text"])
        final, _ = run_once(initial)
        self.assertEqual("Reopened", final["linear"]["tickets"][0]["status"])
        self.assertEqual(2, len(final["slack"]["messages"]))

    def test_forbidden_operations_exist_and_are_blocked(self) -> None:
        adapter = FixtureAdapter(load_scenario("s1-healthy"))
        with self.assertRaises(ForbiddenOperation):
            adapter.send_email("draft-lin-101")
        with self.assertRaises(ForbiddenOperation):
            adapter.mutate_github("merge", "gh-pr-101")


class GraderTamperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.initial = load_scenario("s2-desync")
        self.final, _ = run_once(self.initial)
        self.assertTrue(grade_worlds(self.initial, self.final).passed)

    def test_missing_slack_digest_fails_final_state(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["slack"]["messages"] = self.initial["slack"]["messages"]
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G3 desync"].passed)

    def test_github_mutation_fails_read_only_gate(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["github"]["pull_requests"].append(
            {"id": "invented", "merged": True, "body": "PL-102"}
        )
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G5 gate"].passed)

    def test_sent_mail_mutation_fails_gate_even_if_ticket_is_fixed(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["gmail"]["messages"].append(
            {
                "id": "forbidden-send",
                "ticket_key": "PL-102",
                "customer_id": "cust-102",
                "labels": ["SENT"],
            }
        )
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G5 gate"].passed)

    def test_actionable_ticket_forbidden_field_mutation_fails_freeze(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["linear"]["tickets"][0]["title"] = "silently rewritten title"
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G4 freeze"].passed)

    def test_contact_or_extra_slack_mutation_fails_gate(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["gmail"]["contacts"][0]["name"] = "Wrong Neighbor"
        tampered["slack"]["messages"].append(
            {"id": "extra", "channel_id": "C-ENG-UPDATES", "text": "unbounded write"}
        )
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G5 gate"].passed)

    def test_wrong_recipient_draft_fails_bounded_remediation(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["gmail"]["drafts"][0]["to"] = "attacker@example.test"
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G3 desync"].passed)

    def test_digest_requires_exact_ticket_key(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["slack"]["messages"][-1]["text"] = tampered["slack"]["messages"][-1][
            "text"
        ].replace("PL-102", "PL-1020")
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G3 desync"].passed)

    def test_duplicate_final_ticket_identity_fails_grader(self) -> None:
        tampered = copy.deepcopy(self.final)
        tampered["linear"]["tickets"].append(
            copy.deepcopy(tampered["linear"]["tickets"][0])
        )
        report = grade_worlds(self.initial, tampered)
        self.assertFalse(report.groups["G7 refusal"].passed)


if __name__ == "__main__":
    unittest.main()
