from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aletheia_lite.judgment_room import (
    JudgmentStore,
    apply_staged_court_navigation,
    next_actionable_dilemma_id,
    principal_review_assessment,
    stage_court_navigation,
    split_dilemmas_by_status,
)


class TestJudgmentRoomNavigation(unittest.TestCase):
    def setUp(self):
        self.dilemmas = [
            {"dilemma_id": "D1"},
            {"dilemma_id": "D2"},
            {"dilemma_id": "D3"},
        ]

    def test_open_and_decided_cases_are_partitioned(self):
        decisions = {
            "D1": {"outcome": "ACCEPT_MODEL"},
            "D2": {"outcome": "DEFER"},
        }

        actionable, decided = split_dilemmas_by_status(
            self.dilemmas, decisions
        )

        self.assertEqual(
            [item["dilemma_id"] for item in actionable], ["D2", "D3"]
        )
        self.assertEqual(
            [item["dilemma_id"] for item in decided], ["D1"]
        )

    def test_below_threshold_open_case_is_not_sent_to_principal(self):
        dilemmas = [
            {"dilemma_id": "D1", "principal_review_required": False},
            {"dilemma_id": "D2", "principal_review_required": True},
        ]

        actionable, decided = split_dilemmas_by_status(dilemmas, {})

        self.assertEqual([item["dilemma_id"] for item in actionable], ["D2"])
        self.assertEqual(decided, [])

    def test_below_threshold_decided_case_stays_in_history(self):
        dilemmas = [
            {"dilemma_id": "D1", "principal_review_required": False},
        ]

        actionable, decided = split_dilemmas_by_status(
            dilemmas, {"D1": {"outcome": "ACCEPT_MODEL"}}
        )

        self.assertEqual(actionable, [])
        self.assertEqual([item["dilemma_id"] for item in decided], ["D1"])

    def test_principal_threshold_requires_two_points_or_numeric_na(self):
        self.assertFalse(principal_review_assessment(4, 3)["required"])
        self.assertFalse(principal_review_assessment(3, 4)["required"])
        self.assertTrue(principal_review_assessment(1, 3)["required"])
        self.assertTrue(principal_review_assessment(None, 2)["required"])
        self.assertFalse(principal_review_assessment(None, None)["required"])

    def test_next_case_skips_the_case_just_handled(self):
        decisions = {
            "D1": {"outcome": "ACCEPT_MODEL"},
            "D2": {"outcome": "DEFER"},
        }

        self.assertEqual(
            next_actionable_dilemma_id(
                self.dilemmas,
                decisions,
                current_dilemma_id="D2",
            ),
            "D3",
        )

    def test_next_case_prefers_another_component_in_the_same_article(self):
        dilemmas = [
            {"dilemma_id": "D1", "document_id": "DOC-1"},
            {"dilemma_id": "D2", "document_id": "DOC-2"},
            {"dilemma_id": "D3", "document_id": "DOC-1"},
        ]
        decisions = {"D1": {"outcome": "ACCEPT_MODEL"}}

        self.assertEqual(
            next_actionable_dilemma_id(
                dilemmas,
                decisions,
                current_dilemma_id="D1",
            ),
            "D3",
        )

    def test_deferred_case_remains_selected_when_it_is_the_only_open_case(self):
        decisions = {
            "D1": {"outcome": "ACCEPT_MODEL"},
            "D2": {"outcome": "DEFER"},
            "D3": {"outcome": "ACCEPT_CHALLENGE"},
        }

        self.assertEqual(
            next_actionable_dilemma_id(
                self.dilemmas,
                decisions,
                current_dilemma_id="D2",
            ),
            "D2",
        )

    def test_no_next_case_after_every_case_is_decided(self):
        decisions = {
            dilemma["dilemma_id"]: {"outcome": "CUSTOM"}
            for dilemma in self.dilemmas
        }

        self.assertIsNone(
            next_actionable_dilemma_id(
                self.dilemmas,
                decisions,
                current_dilemma_id="D3",
            )
        )

    def test_navigation_is_staged_without_mutating_live_widget_keys(self):
        state = {
            "court_list_view": "ACTIONABLE",
            "court_actionable_case_id": "D1",
        }

        stage_court_navigation(state, "D2")

        self.assertEqual(state["court_list_view"], "ACTIONABLE")
        self.assertEqual(state["court_actionable_case_id"], "D1")
        self.assertEqual(state["_court_actionable_case_id_next"], "D2")

        apply_staged_court_navigation(state)

        self.assertEqual(state["court_list_view"], "ACTIONABLE")
        self.assertEqual(state["court_actionable_case_id"], "D2")
        self.assertEqual(
            state["_court_actionable_document_sync_case_id"], "D2"
        )
        self.assertNotIn("_court_actionable_case_id_next", state)

    def test_three_sequential_verdict_transitions_end_with_an_empty_queue(self):
        state = {
            "court_list_view": "ACTIONABLE",
            "court_actionable_case_id": "D1",
        }

        for next_id in ("D2", "D3"):
            stage_court_navigation(state, next_id)
            apply_staged_court_navigation(state)
            self.assertEqual(state["court_actionable_case_id"], next_id)

        stage_court_navigation(state, None)
        self.assertEqual(state["court_actionable_case_id"], "D3")
        apply_staged_court_navigation(state)

        self.assertEqual(state["court_list_view"], "ACTIONABLE")
        self.assertNotIn("court_actionable_case_id", state)

    def test_three_real_saves_advance_without_overwriting_widget_state(self):
        dilemmas = [
            {
                "dilemma_id": f"D{index}",
                "case_id": "CASE-TEST",
                "document_id": f"DOC-{index}",
                "component": "N1",
                "issue_tags": ["MATERIAL_OMISSION"],
                "question_he": "שאלת בדיקה",
                "bench_note_he": "הערת בדיקה",
                "prompt_version": "test-v1",
                "model": {"score": 2, "evidence_span": None},
                "challenge": {
                    "score": 0,
                    "na_reason": None,
                    "label_he": "טיעון בדיקה",
                    "argument_he": "נימוק בדיקה",
                },
                "article": {
                    "source_id": "source",
                    "source_name": "Source",
                    "title": "Title",
                    "published_at": "2026-10-04",
                    "content_hash": "content",
                    "snapshot_hash": "snapshot",
                },
                "anchors": [],
            }
            for index in range(1, 4)
        ]
        state = {
            "court_list_view": "ACTIONABLE",
            "court_actionable_case_id": "D1",
        }
        with TemporaryDirectory() as temporary:
            store = JudgmentStore(Path(temporary) / "judgments.sqlite3")
            for index, dilemma in enumerate(dilemmas):
                current_widget_value = state.get("court_actionable_case_id")
                store.save_decision(
                    dilemma,
                    outcome="ACCEPT_CHALLENGE",
                    rationale=f"reason-{index}",
                    decided_by="test-judge",
                    precedent_mode="CASE_ONLY",
                )
                decisions = store.current_decisions()
                next_id = next_actionable_dilemma_id(
                    dilemmas,
                    decisions,
                    current_dilemma_id=dilemma["dilemma_id"],
                )
                stage_court_navigation(state, next_id)
                self.assertEqual(
                    state.get("court_actionable_case_id"), current_widget_value
                )
                apply_staged_court_navigation(state)

            self.assertEqual(len(store.current_decisions()), 3)
            self.assertNotIn("court_actionable_case_id", state)


if __name__ == "__main__":
    unittest.main()
