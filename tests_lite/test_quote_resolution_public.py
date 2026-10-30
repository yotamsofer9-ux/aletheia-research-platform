from __future__ import annotations

import unittest

from scripts.run_article_analysis import resolve_verbatim_spans


class PublicQuoteResolutionTests(unittest.TestCase):
    def test_comma_separated_source_inventory_keeps_one_exact_fragment(self):
        first = "וסיפקה הכתבה תיאור מפורט של העדות שנמסרה לציבור"
        second = "והציגה את תגובת הגורם הרשמי במלואה"
        third = "והפנתה גם למסמך החקירה הפומבי"
        document = {
            "title": "דוח סינתטי",
            "text": f"{first}. {second}. {third}.",
        }
        raw = {
            "relevant_claims": [],
            "components": {
                "T2": {
                    "evidence_spans": [
                        {
                            "span_id": "e1",
                            "quote_original": f"{first}, {second}, {third}",
                        }
                    ]
                }
            },
        }

        corrected, log = resolve_verbatim_spans(raw, document)

        resolved = corrected["components"]["T2"]["evidence_spans"][0][
            "quote_original"
        ]
        self.assertIn(resolved, document["text"])
        self.assertEqual(
            "EXACT_SEGMENT_FROM_COMMA_SEPARATED_CITATION_LIST",
            log[0]["method"],
        )
        self.assertFalse(log[0]["requires_review"])

    def test_fuzzy_noncontiguous_quote_is_routed_to_review(self):
        exact_source = (
            "This synthetic source passage is deliberately long enough to preserve "
            "one exact evidentiary span while making no claim about surrounding text."
        )
        document = {
            "title": "Synthetic report",
            "text": exact_source + " A separate sentence follows in the article.",
        }
        raw = {
            "analysis_status": "COMPLETE",
            "needs_review": {"required": False, "reasons": [], "note": None},
            "relevant_claims": [
                {
                    "quote_original": exact_source
                    + " This ending was not contiguous in the source."
                }
            ],
            "components": {},
        }

        corrected, log = resolve_verbatim_spans(raw, document)

        resolved = corrected["relevant_claims"][0]["quote_original"]
        self.assertIn(resolved, document["text"])
        self.assertTrue(corrected["needs_review"]["required"])
        self.assertEqual("NEEDS_REVIEW", corrected["analysis_status"])
        self.assertIn("EVIDENCE_SPAN_NOT_FOUND", corrected["needs_review"]["reasons"])
        self.assertEqual(
            "LONGEST_EXACT_SOURCE_SUBSTRING_REQUIRES_REVIEW", log[0]["method"]
        )
        self.assertTrue(log[0]["requires_review"])


if __name__ == "__main__":
    unittest.main()
