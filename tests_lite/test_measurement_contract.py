import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "measurement" / "article_measurement_schema.json").read_text()
)


def scored_component():
    return {
        "score": 2,
        "na_reason": None,
        "confidence": "MEDIUM",
        "rationale": "The article presents attributed competing claims.",
        "evidence_spans": [
            {
                "span_id": "S1",
                "quote_original": "officials said",
                "location": "BODY",
                "reviewer_translation_he": None
            }
        ],
        "anchor_refs": ["CASE001-ACTOR-001"],
        "needs_review_reasons": []
    }


def valid_result():
    return {
        "schema_version": "1.0.7",
        "prompt_version": "case001-article-analysis-v1.0.12",
        "case_id": "CASE-001",
        "document_id": "doc-test",
        "language": "en",
        "analysis_status": "COMPLETE",
        "dominant_frame": "INVESTIGATIVE",
        "secondary_frames": ["HUMANITARIAN"],
        "relevant_claims": [
            {
                "claim_id": "C1",
                "quote_original": "officials said",
                "location": "BODY",
                "voice": "ATTRIBUTED_CLAIM",
                "speaker": "officials",
                "centrality": "CENTRAL",
                "claim_kind": "CAUSE_OR_RESPONSIBILITY",
                "outlet_stance": "ADOPTS",
                "anchor_ids": ["CASE001-ACTOR-001"],
                "stance_toward_anchors": "MIXED",
                "certainty": "ATTRIBUTED",
                "evidence_access": "PUBLIC_BASIS",
                "reviewer_translation_he": None
            }
        ],
        "components": {
            code: scored_component()
            for code in ("T1", "T2", "T3", "N1", "N2", "N3", "N4", "N5")
        },
        "trajectory_component": {
            "code": "T4",
            "state": "PENDING_SEPARATE_TRAJECTORY_ANALYSIS",
            "reason": "T4 is scored across a three-phase publisher slot, never from one article."
        },
        "needs_review": {"required": False, "reasons": [], "note": None}
    }


class MeasurementContractTests(unittest.TestCase):
    def setUp(self):
        self.validator = Draft202012Validator(SCHEMA)

    def assert_valid(self, value):
        self.assertEqual([], list(self.validator.iter_errors(value)))

    def assert_invalid(self, value):
        self.assertNotEqual([], list(self.validator.iter_errors(value)))

    def test_valid_article_measurement(self):
        self.assert_valid(valid_result())

    def test_scored_component_requires_original_evidence_span(self):
        result = valid_result()
        result["components"]["T1"]["evidence_spans"] = []
        self.assert_invalid(result)

    def test_na_requires_reason_and_forbids_fake_evidence(self):
        result = valid_result()
        result["components"]["N3"] = {
            "score": None,
            "na_reason": "NO_RELEVANT_COUNTEREVIDENCE",
            "confidence": None,
            "rationale": "No relevant alternative was available at publication time.",
            "evidence_spans": [],
            "anchor_refs": [],
            "needs_review_reasons": []
        }
        self.assert_valid(result)
        result["components"]["N3"]["evidence_spans"] = [
            scored_component()["evidence_spans"][0]
        ]
        self.assert_invalid(result)

    def test_model_cannot_emit_t4_or_composite_fields(self):
        result = valid_result()
        result["components"]["T4"] = scored_component()
        self.assert_invalid(result)
        result = valid_result()
        result["aletheia_score"] = 82
        self.assert_invalid(result)

    def test_component_can_reference_up_to_eight_relevant_anchors(self):
        result = valid_result()
        result["components"]["T1"]["anchor_refs"] = [f"A{i}" for i in range(8)]
        self.assert_valid(result)
        result["components"]["T1"]["anchor_refs"].append("A8")
        self.assert_invalid(result)

    def test_claim_records_each_supported_access_class(self):
        for access_class in (
            "PUBLIC_BASIS",
            "DOCUMENTED_PRIVILEGED_BASIS",
            "ACCESS_NOT_DOCUMENTED",
        ):
            result = valid_result()
            result["relevant_claims"][0]["evidence_access"] = access_class
            self.assert_valid(result)

    def test_output_is_bounded_to_four_claims_and_one_span(self):
        result = valid_result()
        result["relevant_claims"] *= 4
        self.assert_valid(result)
        result["relevant_claims"].append(copy.deepcopy(result["relevant_claims"][0]))
        self.assert_invalid(result)

        result = valid_result()
        result["components"]["T2"]["evidence_spans"].append(
            copy.deepcopy(result["components"]["T2"]["evidence_spans"][0])
        )
        self.assert_invalid(result)

    def test_privileged_access_limitation_is_a_valid_review_reason(self):
        result = valid_result()
        result["analysis_status"] = "NEEDS_REVIEW"
        result["components"]["T2"]["needs_review_reasons"] = [
            "PRIVILEGED_ACCESS_LIMITATION"
        ]
        result["needs_review"] = {
            "required": True,
            "reasons": ["PRIVILEGED_ACCESS_LIMITATION"],
            "note": "The briefing is documented, but its contents cannot be checked."
        }
        self.assert_valid(result)

    def test_model_generated_translation_is_forbidden(self):
        result = valid_result()
        result["relevant_claims"][0]["reviewer_translation_he"] = "גורמים מסרו"
        self.assert_invalid(result)

    def test_all_language_adapters_share_the_same_contract(self):
        adapters = []
        for language in ("he", "en", "ar"):
            path = ROOT / "measurement" / "language_adapters" / f"{language}.json"
            adapter = json.loads(path.read_text())
            adapters.append(adapter)
            self.assertEqual(language, adapter["language"])
            self.assertEqual("case001-article-analysis-v1.0.12", adapter["shared_prompt_version"])
            self.assertEqual(
                "measurement/article_measurement_schema.json",
                adapter["measurement_schema"],
            )
            self.assertGreaterEqual(len(adapter["language_specific_rules"]), 4)
            self.assertGreaterEqual(len(adapter["examples"]), 2)
        self.assertEqual(3, len({row["adapter_version"] for row in adapters}))


if __name__ == "__main__":
    unittest.main()
