from __future__ import annotations

import unittest

from promiseledger.demo_api import list_scenarios, preview_scenario, run_scenario


class DemoApiTests(unittest.TestCase):
    def test_catalog_includes_the_demo_storyboard(self) -> None:
        ids = {item["id"] for item in list_scenarios()}
        self.assertTrue({"s2-desync", "s6-ambiguous-contact", "s10-silent-200"} <= ids)

    def test_desync_run_reopens_and_drafts_without_sending(self) -> None:
        result = run_scenario("s2-desync")
        self.assertTrue(result["grade"]["passed"])
        ticket = result["final"]["linear"]["tickets"][0]
        self.assertEqual("Reopened", ticket["status"])
        self.assertEqual(["DRAFT"], result["final"]["gmail"]["drafts"][0]["labels"])
        self.assertEqual([], result["final"]["gmail"]["messages"])

    def test_silent_200_is_a_grader_failure(self) -> None:
        result = run_scenario("s10-silent-200")
        self.assertFalse(result["grade"]["passed"])
        self.assertEqual("Done", result["final"]["linear"]["tickets"][0]["status"])
        self.assertFalse(result["report"]["verified"])

    def test_preview_does_not_mutate_the_world(self) -> None:
        preview = preview_scenario("s2-desync")
        self.assertEqual("Done", preview["initial"]["linear"]["tickets"][0]["status"])
        self.assertEqual([], preview["initial"]["gmail"]["drafts"])

    def test_run_emits_visible_steps(self) -> None:
        result = run_scenario("s2-desync")
        kinds = [step["kind"] for step in result["steps"]]
        self.assertIn("read", kinds)
        self.assertIn("write", kinds)
        self.assertTrue(any("gmail.create_draft" in step["text"] for step in result["steps"]))

    def test_desync_draft_stays_unsent(self) -> None:
        result = run_scenario("s2-desync")
        draft = result["final"]["gmail"]["drafts"][0]
        self.assertIn("UNSENT", draft["body"])
        self.assertIn("PL-102", draft["body"])
        self.assertEqual(["DRAFT"], draft["labels"])


if __name__ == "__main__":
    unittest.main()
