import unittest

from aletheia_lite.second_review_resolution import resolve_second_review


def ballot(score, *, anchors=None, quote="exact exhibit"):
    return {
        "proposed_score": score,
        "proposed_na_reason": "NOT_APPLICABLE" if score is None else None,
        "confidence": "HIGH",
        "evidence_sufficiency": "SUFFICIENT",
        "anchor_refs": anchors or [],
        "exact_quote_used": None if score is None else quote,
        "residual_risks": [],
    }


def resolve_one(
    *, component, model_score, challenge_score, review, challenge_anchors=None
):
    dilemma_id = f"CASE-999:doc:{component}"
    debate = {
        "case_id": "CASE-999",
        "dilemmas": [
            {
                "dilemma_id": dilemma_id,
                "document_id": "doc",
                "component": component,
                "model_score": model_score,
                "model_na_reason": (
                    "NO_ACTIVE_ANCHOR_FOR_CENTRAL_CLAIM"
                    if model_score is None
                    else None
                ),
                "full_dilemma": {
                    "dilemma_id": dilemma_id,
                    "document_id": "doc",
                    "component": component,
                    "challenge": {
                        "score": challenge_score,
                        "anchor_refs": challenge_anchors or [],
                    },
                },
            }
        ],
    }
    reconciled = {
        "documents": [{"document_id": "doc", "decisions": [{
            "dilemma_id": dilemma_id,
            **review,
        }]}]
    }
    return resolve_second_review(debate, reconciled)


class SecondReviewResolutionTests(unittest.TestCase):
    def test_strong_stable_t1_numeric_na_reclassification_is_automatic(self):
        review = {
            "order_stable": True,
            "proposed_score": 4,
            "proposed_na_reason": None,
            "ballot_1": ballot(4, anchors=["A1"]),
            "ballot_2": ballot(4, anchors=["A1"]),
        }
        result = resolve_one(
            component="T1",
            model_score=None,
            challenge_score=4,
            challenge_anchors=["A1"],
            review=review,
        )
        self.assertEqual(result["summary"]["human_required"], 0)
        self.assertEqual(result["auto_resolutions"][0]["final_score"], 4)
        self.assertEqual(
            result["auto_resolutions"][0]["reason"],
            "STABLE_HIGH_CONFIDENCE_NUMERIC_NA_RECLASSIFICATION",
        )

    def test_unanchored_t1_numeric_challenge_is_rejected(self):
        review = {
            "order_stable": True,
            "proposed_score": 4,
            "proposed_na_reason": None,
            "ballot_1": ballot(4),
            "ballot_2": ballot(4),
        }
        result = resolve_one(
            component="T1",
            model_score=None,
            challenge_score=4,
            review=review,
        )
        self.assertEqual(result["summary"]["human_required"], 0)
        self.assertIsNone(result["auto_resolutions"][0]["final_score"])
        self.assertEqual(
            result["auto_resolutions"][0]["resolution"],
            "UNGROUNDED_T1_NUMERIC_CHALLENGE_REJECTED",
        )

    def test_n3_zero_na_order_effect_retains_original(self):
        review = {
            "order_stable": False,
            "proposed_score": None,
            "proposed_na_reason": None,
            "ballot_1": ballot(0),
            "ballot_2": ballot(None),
        }
        result = resolve_one(
            component="N3",
            model_score=0,
            challenge_score=None,
            review=review,
        )
        self.assertEqual(result["summary"]["human_required"], 0)
        self.assertEqual(result["auto_resolutions"][0]["final_score"], 0)
        self.assertEqual(
            result["auto_resolutions"][0]["reason"],
            "ORDER_SENSITIVE_ZERO_NA_EQUIVALENCE",
        )

    def test_two_point_n3_order_conflict_still_requires_human(self):
        review = {
            "order_stable": False,
            "proposed_score": None,
            "proposed_na_reason": None,
            "ballot_1": ballot(0),
            "ballot_2": ballot(2, anchors=["A1"]),
        }
        result = resolve_one(
            component="N3",
            model_score=0,
            challenge_score=2,
            challenge_anchors=["A1"],
            review=review,
        )
        self.assertEqual(result["summary"]["human_required"], 1)


if __name__ == "__main__":
    unittest.main()
