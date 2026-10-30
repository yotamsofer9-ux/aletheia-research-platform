from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aletheia_lite.case_registry import (
    CaseRegistry,
    CaseRegistryError,
    basket_content_hash,
)
from aletheia_lite.case_generator import manifest_from_candidate
from aletheia_lite.case_generator_contracts import candidate_content_hash


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "case_generator"
    / "fixtures"
    / "case002_offline_fixture_v1.json"
)


class CaseRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = CaseRegistry(Path(self.temporary.name) / "case_registry.sqlite3")
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def seed_to_basket_gate(self):
        candidate = self.fixture["candidate"]
        self.registry.register_candidate(candidate)
        self.registry.record_topic_decision(
            candidate["candidate_id"], candidate["version"], self.fixture["topic_decision"]
        )
        self.registry.register_manifest(self.fixture["manifest"])
        self.registry.save_basket(self.fixture["basket"])

    def basket_decision(self, *, key="fixture-basket-approval-v1"):
        return {
            "decision_id": f"DEC-BASKET-{key}",
            "case_id": self.fixture["manifest"]["case_id"],
            "gate": "BASKET",
            "target_hash": self.fixture["basket"]["basket_hash"],
            "decision": "APPROVE",
            "actor": "principal-researcher-fixture",
            "recorded_at": "2026-10-08T09:10:00Z",
            "rationale": "אישור סל סינתטי לבדיקת CG-1.",
            "idempotency_key": key,
        }

    def test_two_gates_are_required_before_ready_to_run(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        self.assertEqual(
            self.registry.get_case(case_id)["current_state"],
            "AWAITING_BASKET_APPROVAL",
        )
        with self.assertRaisesRegex(CaseRegistryError, "not allowed"):
            self.registry.advance(
                case_id, "RUNNING", actor="test", reason="attempt to bypass gate"
            )

        self.registry.record_basket_decision(case_id, self.basket_decision())
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "READY_TO_RUN")
        self.registry.advance(case_id, "RUNNING", actor="test", reason="start accepted run")
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "RUNNING")

    def test_topic_approval_is_hash_bound_and_idempotent(self):
        candidate = self.fixture["candidate"]
        decision = self.fixture["topic_decision"]
        self.registry.register_candidate(candidate)
        first = self.registry.record_topic_decision(
            candidate["candidate_id"], candidate["version"], decision
        )
        second = self.registry.record_topic_decision(
            candidate["candidate_id"], candidate["version"], decision
        )
        self.assertEqual(first, second)
        self.assertEqual(self.registry.count_decisions(decision["case_id"], "TOPIC"), 1)

        altered = copy.deepcopy(decision)
        altered["rationale"] = "different payload under the same key"
        with self.assertRaisesRegex(CaseRegistryError, "idempotency key was reused"):
            self.registry.record_topic_decision(
                candidate["candidate_id"], candidate["version"], altered
            )

    def test_stale_topic_hash_is_rejected(self):
        candidate = self.fixture["candidate"]
        self.registry.register_candidate(candidate)
        decision = copy.deepcopy(self.fixture["topic_decision"])
        decision["target_hash"] = "0" * 64
        with self.assertRaisesRegex(CaseRegistryError, "stale candidate hash"):
            self.registry.record_topic_decision(
                candidate["candidate_id"], candidate["version"], decision
            )

    def test_candidate_cannot_be_approved_as_two_cases(self):
        candidate = self.fixture["candidate"]
        self.registry.register_candidate(candidate)
        self.registry.record_topic_decision(
            candidate["candidate_id"], candidate["version"], self.fixture["topic_decision"]
        )
        duplicate = copy.deepcopy(self.fixture["topic_decision"])
        duplicate["decision_id"] = "DEC-FIXTURE-TOPIC-DUPLICATE"
        duplicate["case_id"] = "CASE-003-FIXTURE"
        duplicate["idempotency_key"] = "fixture-topic-approval-duplicate"
        with self.assertRaisesRegex(CaseRegistryError, "already approved"):
            self.registry.record_topic_decision(
                candidate["candidate_id"], candidate["version"], duplicate
            )

    def test_rejected_candidate_does_not_consume_a_case_identifier(self):
        first = self.fixture["candidate"]
        self.registry.register_candidate(first)
        rejection = copy.deepcopy(self.fixture["topic_decision"])
        rejection["decision_id"] = "DEC-FIXTURE-TOPIC-REJECT"
        rejection["case_id"] = None
        rejection["decision"] = "REJECT"
        rejection["rationale"] = "מועמד סינתטי אינו מתאים למדגם."
        rejection["idempotency_key"] = "fixture-topic-rejection-v1"
        self.registry.record_topic_decision(first["candidate_id"], first["version"], rejection)
        self.assertEqual(self.registry.next_case_id(), "CASE-002")
        self.assertEqual(self.registry.list_candidates("REJECTED")[0]["candidate_id"], first["candidate_id"])
        self.assertEqual(self.registry.list_selection_audit()[0]["action"], "REJECTED")

        second = copy.deepcopy(first)
        second["candidate_id"] = "CAND-FIXTURE-SECOND"
        second["title"] = "מועמד סינתטי שני"
        second["candidate_hash"] = candidate_content_hash(second)
        self.registry.register_candidate(second)
        approval = copy.deepcopy(self.fixture["topic_decision"])
        approval["decision_id"] = "DEC-FIXTURE-TOPIC-SECOND"
        approval["candidate_id"] = second["candidate_id"]
        approval["case_id"] = self.registry.next_case_id()
        approval["target_hash"] = second["candidate_hash"]
        approval["idempotency_key"] = "fixture-topic-second-v1"
        self.registry.record_topic_decision(second["candidate_id"], second["version"], approval)
        manifest = manifest_from_candidate(second, "CASE-002")
        self.registry.register_manifest(manifest)
        self.assertEqual(self.registry.get_case("CASE-002")["manifest"]["target_articles"], 27)

    def test_manifest_and_basket_require_topic_approval(self):
        self.registry.register_candidate(self.fixture["candidate"])
        with self.assertRaisesRegex(CaseRegistryError, "topic was not approved"):
            self.registry.register_manifest(self.fixture["manifest"])
        with self.assertRaisesRegex(CaseRegistryError, "topic was not approved"):
            self.registry.save_basket(self.fixture["basket"])

    def test_new_basket_version_invalidates_only_basket_approval(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        self.registry.record_basket_decision(case_id, self.basket_decision())

        version_two = copy.deepcopy(self.fixture["basket"])
        version_two["basket_id"] = "BASKET-CASE-002-FIXTURE-V2"
        version_two["version"] = 2
        version_two["created_at"] = "2026-10-08T09:20:00Z"
        version_two["change_reason"] = "החלפה בתוך אותו גוף לאחר אישור הסל."
        version_two["articles"][0]["title"] = "דיווח ראשון סינתטי א׳ — גרסה חלופית"
        version_two["basket_hash"] = basket_content_hash(version_two)

        self.registry.save_basket(version_two)
        case = self.registry.get_case(case_id)
        self.assertEqual(case["current_state"], "AWAITING_BASKET_APPROVAL")
        self.assertEqual(case["current_basket_hash"], version_two["basket_hash"])
        self.assertEqual(self.registry.count_decisions(case_id, "TOPIC"), 1)
        self.assertEqual(self.registry.count_decisions(case_id, "BASKET"), 1)
        self.assertEqual(
            self.registry.get_basket(case_id, 1)["registry_status"], "SUPERSEDED"
        )

    def test_topic_repair_can_reopen_gate_two_from_adjudication(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        self.registry.record_basket_decision(case_id, self.basket_decision())
        self.registry.advance(case_id, "RUNNING", actor="runner", reason="start")
        self.registry.advance(
            case_id,
            "AWAITING_ADJUDICATION",
            actor="runner",
            reason="analysis complete",
        )

        repaired = copy.deepcopy(self.fixture["basket"])
        repaired["basket_id"] = f"BASKET-{case_id}-FIXTURE-V2"
        repaired["version"] = 2
        repaired["created_at"] = "2026-10-08T09:20:00Z"
        repaired["change_reason"] = "תיקון רלוונטיות נושאית לאחר ביקורת הקייס."
        repaired["articles"][0]["title"] = "דיווח חלופי ממוקד בנושא הקייס"
        repaired["basket_hash"] = basket_content_hash(repaired)

        self.registry.save_basket(repaired)
        case = self.registry.get_case(case_id)
        self.assertEqual(case["current_state"], "AWAITING_BASKET_APPROVAL")
        self.assertEqual(case["current_basket_hash"], repaired["basket_hash"])
        self.assertEqual(self.registry.count_decisions(case_id, "TOPIC"), 1)
        self.assertEqual(self.registry.count_decisions(case_id, "BASKET"), 1)

    def test_requested_revision_cannot_republish_unchanged_articles(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        request = self.basket_decision(key="fixture-basket-change-v1")
        request["decision"] = "REQUEST_CHANGES"
        request["rationale"] = "יש להשלים כתבה חסרה."
        self.registry.record_basket_decision(case_id, request)

        unchanged = copy.deepcopy(self.fixture["basket"])
        unchanged["basket_id"] = f"BASKET-{case_id}-V2"
        unchanged["version"] = 2
        unchanged["created_at"] = "2026-10-08T09:20:00Z"
        unchanged["change_reason"] = "סבב השלמה שלא שינה את הסל."
        unchanged["basket_hash"] = basket_content_hash(unchanged)

        with self.assertRaisesRegex(CaseRegistryError, "materially change"):
            self.registry.save_basket(unchanged)
        self.assertEqual(
            self.registry.get_case(case_id)["current_state"], "BASKET_DISCOVERY"
        )
        self.assertEqual(self.registry.get_basket(case_id)["version"], 1)

    def test_basket_approval_is_idempotent_after_state_changes(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        decision = self.basket_decision()
        first = self.registry.record_basket_decision(case_id, decision)
        second = self.registry.record_basket_decision(case_id, decision)
        self.assertEqual(first, second)
        self.assertEqual(self.registry.count_decisions(case_id, "BASKET"), 1)

    def test_pause_failure_resume_and_checkpoint_are_recoverable(self):
        self.seed_to_basket_gate()
        case_id = self.fixture["manifest"]["case_id"]
        self.registry.record_basket_decision(case_id, self.basket_decision())
        self.registry.advance(case_id, "RUNNING", actor="runner", reason="start")

        checkpoint = {
            "checkpoint_id": "CHECKPOINT-FIXTURE-001",
            "case_id": case_id,
            "stage": "COLLECTION",
            "status": "RUNNING",
            "completed_units": 4,
            "total_units": 27,
            "cost_nis": 0.5,
            "peak_rss_mb": 82.0,
            "artifact_refs": ["staging/case-002-fixture"],
            "recorded_at": "2026-10-08T09:30:00Z",
            "failure_reason": None,
            "idempotency_key": "checkpoint-fixture-001",
        }
        first = self.registry.save_checkpoint(checkpoint)
        second = self.registry.save_checkpoint(checkpoint)
        self.assertEqual(first, second)
        self.assertEqual(
            self.registry.latest_checkpoint(case_id, "COLLECTION")["completed_units"], 4
        )

        self.registry.pause(case_id, actor="runner", reason="bounded pause")
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "PAUSED")
        self.registry.resume(case_id, actor="runner", reason="continue")
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "RUNNING")
        self.registry.fail(case_id, actor="runner", reason="synthetic error")
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "FAILED")
        self.registry.resume(case_id, actor="runner", reason="retry from checkpoint")
        self.assertEqual(self.registry.get_case(case_id)["current_state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
