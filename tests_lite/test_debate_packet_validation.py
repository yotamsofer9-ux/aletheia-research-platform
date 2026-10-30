import unittest

from scripts.build_m5_debate_packets import _validate_and_materialize


class DebatePacketValidationTests(unittest.TestCase):
    def _dossier(self, *, component="T1", language="ar"):
        return {
            "dilemma_id": f"CASE-TEST:doc:{component}",
            "document_id": "doc",
            "component": component,
            "language": language,
            "routing_reasons": ["TEST_REVIEW"],
            "model_position": {
                "score": 0,
                "na_reason": None,
                "evidence_span": {
                    "quote_original": "model quote",
                    "location": "LEAD",
                },
                "anchor_refs": [],
            },
            "eligible_exact_exhibits": [
                {"quote_original": "challenge quote", "location": "BODY"}
            ],
            "claim_context": [
                {
                    "quote_original": "challenge quote",
                    "location": "BODY",
                    "anchor_ids": [],
                }
            ],
        }

    def _generated(self, *, component="T1", challenge_score=2, anchors=None):
        return {
            "records": [{
                "dilemma_id": f"CASE-TEST:doc:{component}",
                "question_he": "שאלה",
                "bench_note_he": "הערה",
                "challenge_score": challenge_score,
                "challenge_na_reason": None,
                "challenge_label_he": "השגה",
                "challenge_argument_he": "טיעון",
                "challenge_anchor_refs": anchors or [],
                "decisive_exhibit_quote_original": "challenge quote",
                "decisive_exhibit_location": "BODY",
                "curation_note_he": "הקטע מציג את הטענה שבמחלוקת.",
                "translation_he": "תרגום טענת ההשגה",
                "translation_note_he": "",
                "glossary": [],
            }]
        }

    def test_challenge_exhibit_is_not_overwritten_by_model_exhibit(self):
        queue = {"batch_id": "B", "prompt_version": "P", "policy_version": "V"}
        packet, translations = _validate_and_materialize(
            queue,
            [self._dossier()],
            self._generated(),
            case_id="CASE-TEST",
        )
        dilemma = packet["dilemmas"][0]
        self.assertEqual(
            "challenge quote", dilemma["curated_exhibit"]["quote_original"]
        )
        self.assertEqual(
            "challenge quote", translations["records"][0]["source_text"]
        )
        self.assertEqual(
            "תרגום טענת ההשגה", translations["records"][0]["translation_he"]
        )

    def test_ungrounded_n3_challenge_is_resolved_without_dispute(self):
        queue = {"batch_id": "B", "prompt_version": "P", "policy_version": "V"}
        packet, translations = _validate_and_materialize(
            queue,
            [self._dossier(component="N3", language="he")],
            self._generated(component="N3", challenge_score=2),
            case_id="CASE-TEST",
        )
        self.assertEqual([], packet["dilemmas"])
        self.assertEqual([], translations["records"])
        self.assertEqual(
            "UNGROUNDED_COUNTEREVIDENCE_CHALLENGE_REJECTED",
            packet["resolved_without_dispute"][0]["resolution"],
        )

    def test_numeric_string_challenge_score_is_normalized(self):
        queue = {"batch_id": "B", "prompt_version": "P", "policy_version": "V"}
        packet, _ = _validate_and_materialize(
            queue,
            [self._dossier()],
            self._generated(challenge_score="3"),
            case_id="CASE-TEST",
        )
        self.assertEqual(3, packet["dilemmas"][0]["challenge"]["score"])


if __name__ == "__main__":
    unittest.main()
