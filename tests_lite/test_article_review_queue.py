import unittest

from scripts.build_article_review_queue import classify_component


def component(score, confidence="HIGH", review_reasons=None):
    return {
        "score": score,
        "confidence": confidence if score is not None else None,
        "rationale": "Test rationale.",
        "evidence_spans": [],
        "anchor_refs": [],
        "needs_review_reasons": review_reasons or [],
    }


class ArticleReviewQueueTests(unittest.TestCase):
    def test_safe_high_confidence_component_is_provisional(self):
        result = classify_component("T2", component(3))
        self.assertEqual("PROVISIONAL_ACCEPT", result["decision"])
        self.assertEqual([], result["reasons"])

    def test_low_truth_score_requires_review(self):
        result = classify_component("T1", component(1))
        self.assertEqual("HUMAN_REVIEW", result["decision"])
        self.assertIn("CRITICAL_LOW_TRUTH_SCORE", result["reasons"])

    def test_high_pressure_score_requires_review(self):
        result = classify_component("N3", component(3))
        self.assertEqual("HUMAN_REVIEW", result["decision"])
        self.assertIn("CRITICAL_HIGH_PRESSURE_SCORE", result["reasons"])

    def test_medium_confidence_alone_is_provisional_under_v3(self):
        result = classify_component("N5", component(1, confidence="MEDIUM"))
        self.assertEqual("PROVISIONAL_ACCEPT", result["decision"])
        self.assertEqual([], result["reasons"])

    def test_explicit_reason_is_preserved_without_component_level_audit_routing(self):
        result = classify_component(
            "T2",
            component(4, review_reasons=["PRIVILEGED_ACCESS_LIMITATION"]),
            audit_selected=True,
        )
        self.assertIn(
            "EXPLICIT_PRIVILEGED_ACCESS_LIMITATION", result["reasons"]
        )
        self.assertNotIn("QUALITY_AUDIT_SAMPLE", result["reasons"])

    def test_cross_field_reason_forces_review(self):
        result = classify_component(
            "T1", component(4), extra_reasons=["CROSS_FIELD_CAUSAL_CONFLICT"]
        )
        self.assertEqual("HUMAN_REVIEW", result["decision"])
        self.assertIn("CROSS_FIELD_CAUSAL_CONFLICT", result["reasons"])


if __name__ == "__main__":
    unittest.main()
