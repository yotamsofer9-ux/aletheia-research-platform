from __future__ import annotations

import unittest

from aletheia_lite.topic_relevance import (
    _apply_adjudication,
    _reconcile,
    _validate_assessments,
)


def row(label: str, confidence: str = "HIGH") -> dict:
    return {
        "article_id": "ARTICLE-1",
        "label": label,
        "confidence": confidence,
        "primary_subject": "subject",
        "explicit_causal_link": False,
        "evidence_quote": "quote",
        "rationale": "reason",
    }


class TopicRelevanceTests(unittest.TestCase):
    def test_two_independent_passes_are_required(self):
        self.assertEqual(
            "PASS", _reconcile(row("PRIMARY_EVENT"), row("DIRECT_REACTION"))["disposition"]
        )
        self.assertEqual(
            "HUMAN_REVIEW",
            _reconcile(row("PRIMARY_EVENT"), row("INCIDENTAL_CONTEXT"))["disposition"],
        )
        self.assertEqual(
            "REPAIR_REQUIRED",
            _reconcile(row("INCIDENTAL_CONTEXT"), row("DIFFERENT_EVENT"))["disposition"],
        )

    def test_low_confidence_never_auto_passes(self):
        result = _reconcile(
            row("PRIMARY_EVENT", "LOW"), row("PRIMARY_EVENT", "HIGH")
        )
        self.assertEqual("HUMAN_REVIEW", result["disposition"])

    def test_third_review_resolves_ordinary_disagreement(self):
        disputed = _reconcile(row("PRIMARY_EVENT"), row("INCIDENTAL_CONTEXT"))
        resolved = _apply_adjudication(disputed, row("DIRECT_CONSEQUENCE", "HIGH"))
        self.assertEqual("PASS", resolved["disposition"])
        rejected = _apply_adjudication(disputed, row("DIFFERENT_EVENT", "MEDIUM"))
        self.assertEqual("REPAIR_REQUIRED", rejected["disposition"])

    def test_uncertain_third_review_still_routes_human(self):
        disputed = _reconcile(row("PRIMARY_EVENT"), row("INCIDENTAL_CONTEXT"))
        resolved = _apply_adjudication(disputed, row("UNDECIDABLE", "LOW"))
        self.assertEqual("HUMAN_REVIEW", resolved["disposition"])

    def test_high_confidence_missing_body_routes_repair(self):
        disputed = _reconcile(row("PRIMARY_EVENT"), row("INCIDENTAL_CONTEXT"))
        adjudication = row("UNDECIDABLE", "HIGH")
        adjudication["evidence_quote"] = None
        resolved = _apply_adjudication(disputed, adjudication)
        self.assertEqual("REPAIR_REQUIRED", resolved["disposition"])
        self.assertEqual(
            "UNUSABLE_OR_MISSING_ARTICLE_BODY", resolved["repair_reason"]
        )

    def test_headline_is_valid_exact_evidence(self):
        documents = [
            {"article_id": "ARTICLE-1", "title": "Exact headline", "text": "Body"}
        ]
        validated = _validate_assessments(
            [
                {
                    **row("PRIMARY_EVENT", "HIGH"),
                    "evidence_quote": "Exact headline",
                }
            ],
            documents,
        )
        self.assertEqual("PRIMARY_EVENT", validated[0]["label"])

    def test_minor_source_spacing_is_recovered_as_exact_evidence(self):
        documents = [
            {
                "article_id": "ARTICLE-1",
                "title": "Headline",
                "text": "Missiles were aimed at Israel , whose government responded.",
            }
        ]
        validated = _validate_assessments(
            [
                {
                    **row("DIRECT_CONSEQUENCE", "HIGH"),
                    "evidence_quote": (
                        "Missiles were aimed at Israel, whose government responded."
                    ),
                }
            ],
            documents,
        )
        self.assertEqual("DIRECT_CONSEQUENCE", validated[0]["label"])
        self.assertEqual(
            "Missiles were aimed at Israel , whose government responded.",
            validated[0]["evidence_quote"],
        )

    def test_stray_intra_word_spacing_in_arabic_is_recovered(self):
        documents = [
            {
                "article_id": "ARTICLE-1",
                "title": "عنوان",
                "text": (
                    "تحدث جيش الاحتلال عن مقتل رئيس المكتب السياسي "
                    "لحركة حماس ي حيى السنوار خلال الاشتباك."
                ),
            }
        ]
        validated = _validate_assessments(
            [
                {
                    **row("PRIMARY_EVENT", "HIGH"),
                    "evidence_quote": (
                        "تحدث جيش الاحتلال عن مقتل رئيس المكتب السياسي "
                        "لحركة حماس يحيى السنوار خلال الاشتباك."
                    ),
                }
            ],
            documents,
        )
        self.assertEqual("PRIMARY_EVENT", validated[0]["label"])
        self.assertIn("ي حيى السنوار", validated[0]["evidence_quote"])


if __name__ == "__main__":
    unittest.main()
