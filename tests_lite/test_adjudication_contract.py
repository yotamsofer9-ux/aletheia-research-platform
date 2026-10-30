import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "anchors" / "adjudication_record_schema.json"


class AdjudicationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def record(self, outcome: str, basis: str) -> dict:
        return {
            "schema_version": "0.1",
            "adjudication_id": "ADJ-001",
            "case_id": "CASE-001",
            "anchor_id": "CASE001-IMPACT-001",
            "assessment_id": "CASE001-IMPACT-001-A1",
            "outcome": outcome,
            "decided_at": {
                "value": "2026-09-29T09:00:00+03:00",
                "precision": "DATETIME",
            },
            "decided_by": "test-reviewer",
            "basis_category": basis,
            "rationale": "A sufficiently detailed rationale for contract validation.",
            "preserved_model_review_refs": ["review-input-hash"],
            "evidence_source_ids": ["source-1"],
            "resulting_epistemic_status": "SUPPORTED",
            "resulting_confidence": "HIGH",
            "notes": None,
        }

    def assert_valid(self, record: dict) -> None:
        self.assertEqual([], list(self.validator.iter_errors(record)))

    def test_model_approved_requires_model_basis_and_review_reference(self) -> None:
        self.assert_valid(self.record("MODEL_APPROVED", "MODEL_REVIEW_ONLY"))
        invalid = self.record("MODEL_APPROVED", "PUBLIC_INSPECTABLE_EVIDENCE")
        self.assertTrue(list(self.validator.iter_errors(invalid)))

    def test_human_override_preserves_non_model_basis(self) -> None:
        self.assert_valid(
            self.record("HUMAN_OVERRIDE", "PUBLIC_INSPECTABLE_EVIDENCE")
        )
        invalid = self.record("HUMAN_OVERRIDE", "MODEL_REVIEW_ONLY")
        self.assertTrue(list(self.validator.iter_errors(invalid)))

    def test_pending_outcomes_cannot_publish_a_factual_status(self) -> None:
        pending = self.record("NEEDS_MORE_EVIDENCE", "METHODOLOGICAL_JUDGMENT")
        pending["resulting_epistemic_status"] = None
        pending["resulting_confidence"] = None
        self.assert_valid(pending)

        pending["resulting_epistemic_status"] = "SUPPORTED"
        self.assertTrue(list(self.validator.iter_errors(pending)))


if __name__ == "__main__":
    unittest.main()
