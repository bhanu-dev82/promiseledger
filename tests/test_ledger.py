from __future__ import annotations

import copy
import unittest

from promiseledger.adapters.fixture import FixtureAdapter
from promiseledger.ledger import build_ledger, contains_exact_key, identity_refusals
from promiseledger.models import Classification
from promiseledger.seed import load_scenario


class LedgerTests(unittest.TestCase):
    def test_exact_ticket_key_does_not_match_neighbor(self) -> None:
        self.assertTrue(contains_exact_key("Ships PL-10.", "PL-10"))
        self.assertFalse(contains_exact_key("Ships PL-100.", "PL-10"))
        self.assertFalse(contains_exact_key("Ships XPL-10.", "PL-10"))

    def test_done_requires_configured_proof_type(self) -> None:
        world = load_scenario("s5-missing-notice")
        entry = build_ledger(world)[0]
        self.assertEqual(Classification.DESYNC, entry.classification)
        self.assertEqual(["merged_pr"], [artifact.kind for artifact in entry.artifacts])
        self.assertFalse(entry.has_required_proof)

    def test_sent_evidence_requires_exact_customer_id(self) -> None:
        world = load_scenario("s1-healthy")
        world["gmail"]["messages"][0]["customer_id"] = "cust-lookalike"
        world["linear"]["tickets"][0]["required_proof"] = "sent_email"
        entry = build_ledger(world)[0]
        self.assertEqual(Classification.DESYNC, entry.classification)

    def test_sent_evidence_requires_exact_recipient(self) -> None:
        world = load_scenario("s1-healthy")
        world["linear"]["tickets"][0]["required_proof"] = "sent_email"
        world["gmail"]["messages"][0]["to"] = "attacker@example.test"
        entry = build_ledger(world)[0]
        self.assertEqual(Classification.DESYNC, entry.classification)

    def test_same_name_without_customer_id_refuses(self) -> None:
        world = load_scenario("s6-ambiguous-contact")
        reasons = identity_refusals(world, build_ledger(world))
        self.assertEqual(1, len(reasons))
        self.assertIn("names are never join keys", reasons[0])

    def test_human_waiver_requires_human_marker_and_approved_draft(self) -> None:
        world = load_scenario("s2-desync")
        adapter = FixtureAdapter(world)
        adapter.create_draft(
            "lin-102",
            "cust-102",
            "owner102@example.test",
            "Review",
            "Unsent",
            "draft-key",
        )
        adapter.record_human_waiver(
            "lin-102", "draft-lin-102", "operator@example.test"
        )
        waived = adapter.snapshot()
        self.assertTrue(build_ledger(waived)[0].waiver)

        forged = copy.deepcopy(waived)
        forged["linear"]["tickets"][0]["comments"][-1]["author_type"] = "agent"
        self.assertFalse(build_ledger(forged)[0].waiver)

        misaddressed = copy.deepcopy(waived)
        misaddressed["gmail"]["drafts"][0]["to"] = "attacker@example.test"
        self.assertFalse(build_ledger(misaddressed)[0].waiver)


if __name__ == "__main__":
    unittest.main()
