from __future__ import annotations

import unittest

from aletheia_lite.anchor_review_materializer import (
    AnchorReviewMaterializationError,
    materialize_model_reviewed_anchors,
)


class AnchorReviewMaterializerTests(unittest.TestCase):
    def _fixture(self, *, status="SUPPORTED", recommended="SUPPORTED", verdict="REVISE"):
        anchors = [{
            "anchor_id": "A",
            "assessments": [{
                "assessment_id": "A1",
                "epistemic_status": status,
                "confidence": "HIGH",
                "basis_summary": "old",
                "evidence_refs": [
                    {"source_id": "early"}, {"source_id": "later"}
                ],
                "review_status": "DRAFT",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_notes": "",
            }],
        }]
        reviews = [{
            "anchor_id": "A",
            "model": "test-model",
            "prompt_version": "v1",
            "input_hash": "hash",
            "model_output": {"assessment_reviews": [{
                "assessment_id": "A1",
                "verdict": verdict,
                "recommended_epistemic_status": recommended,
                "recommended_confidence": "HIGH",
                "evidence_source_ids": ["early"],
                "critical_issue": None,
            }]},
        }]
        return anchors, reviews

    def test_temporal_subset_revision_is_materialized(self):
        anchors, reviews = self._fixture()
        rows = materialize_model_reviewed_anchors(
            anchors,
            reviews,
            revised_basis_summaries={"A1": "early evidence only"},
            reviewed_at="2026-10-13T00:00:00+00:00",
        )
        assessment = rows[0]["assessments"][0]
        self.assertEqual(["early"], [x["source_id"] for x in assessment["evidence_refs"]])
        self.assertEqual("MODEL_APPROVED", assessment["review_status"])
        self.assertEqual("early evidence only", assessment["basis_summary"])

    def test_substantive_revision_is_not_automated(self):
        anchors, reviews = self._fixture(recommended="UNRESOLVED")
        with self.assertRaisesRegex(
            AnchorReviewMaterializationError, "substantive revision"
        ):
            materialize_model_reviewed_anchors(
                anchors,
                reviews,
                revised_basis_summaries={"A1": "unused"},
            )


if __name__ == "__main__":
    unittest.main()
