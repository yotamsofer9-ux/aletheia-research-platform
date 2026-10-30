from __future__ import annotations

import unittest

from aletheia_lite.autonomous_review import (
    PROTOCOL_VERSION,
    build_blind_dilemma,
    promotion_gate,
    reconcile_ballots,
)


def dilemma():
    return {
        "dilemma_id": "CASE001:DOC:N1",
        "document_id": "DOC",
        "component": "N1",
        "language": "ar",
        "issue_tags": ["CRITICAL_HIGH_PRESSURE_SCORE"],
        "question_he": "שאלה",
        "bench_note_he": "בדיקה",
        "model": {
            "score": 4,
            "na_reason": None,
            "rationale": "model",
            "evidence_span": {"quote_original": "quote"},
            "anchor_refs": [],
        },
        "challenge": {
            "score": 3,
            "na_reason": None,
            "argument_he": "challenge",
            "anchor_refs": [],
        },
        "decisive_exhibit": {"quote_original": "quote"},
    }


class TestAutonomousReview(unittest.TestCase):
    def test_second_ballot_reverses_blinded_positions(self):
        first, first_mapping = build_blind_dilemma(dilemma(), 1)
        second, second_mapping = build_blind_dilemma(dilemma(), 2)

        self.assertEqual(first["positions"].keys(), second["positions"].keys())
        first_model = next(
            label for label, source in first_mapping.items()
            if source == "MODEL_PROPOSAL"
        )
        second_model = next(
            label for label, source in second_mapping.items()
            if source == "MODEL_PROPOSAL"
        )
        self.assertNotEqual(first_model, second_model)

    def test_order_stable_grounded_ballots_create_shadow_candidate(self):
        row = {
            "dilemma_id": "D1",
            "selection": "POSITION_A",
            "proposed_score": 3,
            "proposed_na_reason": None,
            "confidence": "HIGH",
            "evidence_sufficiency": "SUFFICIENT",
            "addressed_issue_tags": ["TAG"],
            "all_issue_tags": ["TAG"],
            "exact_quote_used": "quote",
            "anchor_refs": [],
            "rationale_he": "נימוק",
            "residual_risks": [],
        }
        first = {
            "protocol_version": PROTOCOL_VERSION,
            "document_id": "DOC",
            "ballot_number": 1,
            "decisions": [row],
        }
        second = {
            **first,
            "ballot_number": 2,
            "decisions": [{**row, "selection": "POSITION_B"}],
        }

        result = reconcile_ballots(first, second)

        self.assertTrue(result["decisions"][0]["shadow_auto_candidate"])

    def test_promotion_gate_stays_shadow_when_severe_error_exists(self):
        gate = promotion_gate({
            "candidate_count": 20,
            "exact_agreement": 0.95,
            "within_one_agreement": 0.98,
            "severe_errors": 1,
            "numeric_na_errors": 0,
            "minimum_language_exact_agreement": 0.90,
        })

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "SHADOW_ONLY")


if __name__ == "__main__":
    unittest.main()
