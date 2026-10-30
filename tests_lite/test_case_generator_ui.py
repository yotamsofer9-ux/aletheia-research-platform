from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aletheia_lite.case_generator_contracts import candidate_content_hash
from aletheia_lite.case_generator_ui import (
    approve_topic_candidate,
    decide_article_basket,
    reject_topic_candidate,
    replacement_slot_decision_copy,
)
from aletheia_lite.case_registry import CaseRegistry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "case_generator/fixtures/case002_offline_fixture_v1.json").read_text(
        encoding="utf-8"
    )
)


class CaseGeneratorUIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project_root = Path(self.temporary.name)
        self.registry = CaseRegistry(
            self.project_root / "data" / "case_registry.sqlite3"
        )

    def test_approval_allocates_case_and_materializes_manifest(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="מאושר למבחן.",
        )
        self.assertEqual(case_id, "CASE-002")
        case = self.registry.get_case(case_id)
        self.assertEqual(case["current_state"], "BASKET_DISCOVERY")
        self.assertEqual(case["manifest"]["keywords"], candidate["proposed_keywords"])
        self.assertEqual(case["manifest"]["max_project_cost_nis"], 100)

    def test_rejection_stays_in_audit_and_does_not_allocate_case(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        candidate["candidate_id"] = "CAND-UI-REJECT"
        candidate["candidate_hash"] = candidate_content_hash(candidate)
        self.registry.register_candidate(candidate)
        reject_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="אינו מוסיף שונות למדגם.",
        )
        self.assertEqual(self.registry.next_case_id(), "CASE-002")
        self.assertEqual(self.registry.list_candidates("REJECTED")[0]["candidate_id"], "CAND-UI-REJECT")
        self.assertEqual(self.registry.list_selection_audit()[0]["reason"], "אינו מוסיף שונות למדגם.")

    def test_rejection_requires_research_rationale(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        with self.assertRaisesRegex(ValueError, "rationale"):
            reject_topic_candidate(
                self.registry,
                candidate,
                actor="principal-researcher",
                rationale="   ",
            )

    def test_second_gate_freezes_exact_basket_hash(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="מאושר.",
        )
        basket = copy.deepcopy(FIXTURE["basket"])
        basket["case_id"] = case_id
        basket["basket_id"] = f"BASKET-{case_id}-V1"
        from aletheia_lite.case_generator_contracts import basket_content_hash

        basket["basket_hash"] = basket_content_hash(basket)
        self.registry.save_basket(basket)
        decide_article_basket(
            self.registry,
            case_id,
            decision="APPROVE",
            actor="principal-researcher",
            rationale="הסל מאוזן ומאושר.",
        )
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "READY_TO_RUN")
        self.assertEqual(self.registry.get_basket(case_id)["registry_status"], "APPROVED")

    def test_change_request_returns_case_to_local_discovery(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="מאושר.",
        )
        basket = copy.deepcopy(FIXTURE["basket"])
        basket["case_id"] = case_id
        basket["basket_id"] = f"BASKET-{case_id}-V1"
        from aletheia_lite.case_generator_contracts import basket_content_hash

        basket["basket_hash"] = basket_content_hash(basket)
        self.registry.save_basket(basket)
        decide_article_basket(
            self.registry,
            case_id,
            decision="REQUEST_CHANGES",
            actor="principal-researcher",
            rationale="יש להחליף את הכתבה המאוחרת במשבצת אחת.",
        )
        self.assertEqual(
            self.registry.get_case(case_id)["current_state"], "BASKET_DISCOVERY"
        )

    def test_v12_gate_two_requires_passing_audit_for_exact_basket_hash(self):
        candidate = copy.deepcopy(FIXTURE["candidate"])
        candidate["candidate_id"] = "CAND-TOPIC-AUDIT"
        candidate["methodology_version"] = "case-generator-contract-v1.2"
        candidate["candidate_hash"] = candidate_content_hash(candidate)
        self.registry.register_candidate(candidate)
        case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="מאושר לבדיקת השער.",
        )
        basket = copy.deepcopy(FIXTURE["basket"])
        basket["case_id"] = case_id
        basket["basket_id"] = f"BASKET-{case_id}-V1"
        from aletheia_lite.case_generator_contracts import basket_content_hash

        basket["basket_hash"] = basket_content_hash(basket)
        self.registry.save_basket(basket)
        with self.assertRaisesRegex(ValueError, "topic relevance audit"):
            decide_article_basket(
                self.registry,
                case_id,
                decision="APPROVE",
                actor="principal-researcher",
                rationale="",
            )

        audit_path = (
            self.project_root
            / "analysis/case_generator"
            / case_id.lower()
            / "topic_relevance_audit_v2.json"
        )
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(
                {"basket_hash": basket["basket_hash"], "status": "PASS"}
            ),
            encoding="utf-8",
        )
        decide_article_basket(
            self.registry,
            case_id,
            decision="APPROVE",
            actor="principal-researcher",
            rationale="",
        )
        self.assertEqual("READY_TO_RUN", self.registry.get_case(case_id)["current_state"])

    def test_replacement_copy_distinguishes_full_slot_from_mixed_slot(self):
        full_slot = [
            {"source_name": "N12"},
            {"source_name": "N12"},
            {"source_name": "N12"},
        ]
        mixed_slot = [
            {"source_name": "Al Araby TV"},
            {"source_name": "Al Araby TV"},
            {"source_name": "Al-Quds Al-Arabi"},
        ]
        self.assertIn(
            "החלפה מלאה",
            replacement_slot_decision_copy("זמן ישראל", full_slot),
        )
        mixed_copy = replacement_slot_decision_copy("אל־ערבי", mixed_slot)
        self.assertIn("הרכב המעורב", mixed_copy)
        self.assertNotIn("החלפה מלאה", mixed_copy)


if __name__ == "__main__":
    unittest.main()
