import unittest

from aletheia_lite.second_review_resolution import (
    repair_open_human_docket,
    resolve_second_review,
)


class SecondReviewResolutionTests(unittest.TestCase):
    def _packet(self, model_score=3, component="N2", challenge=None):
        full = {
            "dilemma_id": "D1",
            "document_id": "doc",
            "component": component,
            "challenge": challenge or {"score": 1, "anchor_refs": []},
        }
        return {
            "case_id": "CASE-002",
            "dilemmas": [{
                "dilemma_id": "D1", "document_id": "doc", "component": component,
                "model_score": model_score, "model_na_reason": None,
                "full_dilemma": full,
            }],
        }

    def _review(self, score, stable=True):
        decision = {
            "dilemma_id": "D1", "order_stable": stable,
            "proposed_score": score if stable else None,
            "proposed_na_reason": None,
            "ballot_1": {"proposed_score": score},
            "ballot_2": {"proposed_score": score},
        }
        return {"documents": [{"decisions": [decision]}]}

    def test_one_point_gap_is_automatic(self):
        result = resolve_second_review(self._packet(3), self._review(2))
        self.assertEqual(1, result["summary"]["auto_resolved"])
        self.assertEqual(2, result["auto_resolutions"][0]["final_score"])

    def test_two_point_gap_requires_human(self):
        result = resolve_second_review(self._packet(3), self._review(1))
        self.assertEqual(1, result["summary"]["human_required"])

    def test_ungrounded_n3_challenge_is_rejected_before_human_escalation(self):
        packet = self._packet(
            0,
            component="N3",
            challenge={"score": 2, "anchor_refs": []},
        )
        result = resolve_second_review(packet, self._review(2))
        self.assertEqual(1, result["summary"]["auto_resolved"])
        self.assertEqual(0, result["auto_resolutions"][0]["final_score"])
        self.assertEqual(
            "COUNTEREVIDENCE_ANCHOR_REQUIRED",
            result["auto_resolutions"][0]["reason"],
        )

    def test_grounded_n3_two_point_gap_still_requires_human(self):
        packet = self._packet(
            0,
            component="N3",
            challenge={"score": 2, "anchor_refs": ["ANCHOR-1"]},
        )
        result = resolve_second_review(packet, self._review(2))
        self.assertEqual(1, result["summary"]["human_required"])

    def test_open_docket_repair_preserves_existing_human_decision(self):
        dilemma = {
            "dilemma_id": "D1",
            "document_id": "doc",
            "component": "N3",
            "challenge": {"score": 2, "anchor_refs": []},
            "model": {"score": 0, "na_reason": None},
            "second_review": {},
        }
        principal = {
            "auto_resolutions": [],
            "human_dilemmas": [dilemma],
            "summary": {"reviewed_dilemmas": 1, "auto_resolved": 0, "human_required": 1},
        }
        repaired, moved = repair_open_human_docket(
            principal, decided_dilemma_ids={"D1"}
        )
        self.assertEqual([], moved)
        self.assertEqual(1, repaired["summary"]["human_required"])

    def test_open_docket_repair_moves_only_undecided_invalid_challenge(self):
        dilemma = {
            "dilemma_id": "D1",
            "document_id": "doc",
            "component": "N3",
            "challenge": {"score": 2, "anchor_refs": []},
            "model": {"score": 0, "na_reason": None},
            "second_review": {},
        }
        principal = {
            "auto_resolutions": [],
            "human_dilemmas": [dilemma],
            "summary": {"reviewed_dilemmas": 1, "auto_resolved": 0, "human_required": 1},
        }
        repaired, moved = repair_open_human_docket(
            principal, decided_dilemma_ids=set()
        )
        self.assertEqual(["D1"], [row["dilemma_id"] for row in moved])
        self.assertEqual(0, repaired["summary"]["human_required"])
        self.assertEqual(1, repaired["summary"]["auto_resolved"])


if __name__ == "__main__":
    unittest.main()
