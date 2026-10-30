from __future__ import annotations

import copy
import json
import unittest
from collections import Counter
from pathlib import Path

from aletheia_lite.case_generator_contracts import (
    CaseGeneratorContractError,
    basket_content_hash,
    load_contract_schema,
    validate_case_generator_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "case_generator"
    / "fixtures"
    / "case002_offline_fixture_v1.json"
)


class CaseGeneratorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_contract_schema()
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_offline_fixture_satisfies_schema_and_cross_record_contract(self):
        validate_case_generator_fixture(self.fixture, schema=self.schema)

    def test_fixture_is_balanced_across_languages_phases_and_slots(self):
        articles = self.fixture["basket"]["articles"]
        self.assertEqual(len(articles), 27)
        self.assertEqual(Counter(article["language"] for article in articles), {
            "he": 9,
            "en": 9,
            "ar": 9,
        })
        self.assertEqual(Counter(article["sample_phase"] for article in articles), {
            "INITIAL_REPORT": 9,
            "EARLY_UPDATE": 9,
            "LATER_INVESTIGATION": 9,
        })
        slot_counts = Counter(article["publisher_slot"] for article in articles)
        self.assertEqual(len(slot_counts), 9)
        self.assertEqual(set(slot_counts.values()), {3})

    def test_selection_payload_rejects_result_scores(self):
        payload = copy.deepcopy(self.fixture)
        payload["candidate"]["truth_score"] = 4
        with self.assertRaisesRegex(CaseGeneratorContractError, "forbidden score field"):
            validate_case_generator_fixture(payload, schema=self.schema)

    def test_basket_below_hard_floor_is_rejected(self):
        payload = copy.deepcopy(self.fixture)
        payload["basket"]["articles"] = payload["basket"]["articles"][:24]
        with self.assertRaisesRegex(CaseGeneratorContractError, "schema violation"):
            validate_case_generator_fixture(payload, schema=self.schema)

    def test_topic_approval_must_match_candidate_hash(self):
        payload = copy.deepcopy(self.fixture)
        payload["topic_decision"]["target_hash"] = "0" * 64
        with self.assertRaisesRegex(CaseGeneratorContractError, "candidate hash"):
            validate_case_generator_fixture(payload, schema=self.schema)

    def test_third_outlet_in_one_slot_is_rejected(self):
        payload = copy.deepcopy(self.fixture)
        slot_articles = [
            article
            for article in payload["basket"]["articles"]
            if article["publisher_slot"] == "he-local-a"
        ]
        for index, article in enumerate(slot_articles):
            article["source_id"] = f"he-source-{index}"
        payload["basket"]["basket_hash"] = basket_content_hash(payload["basket"])
        with self.assertRaisesRegex(CaseGeneratorContractError, "more than two outlets"):
            validate_case_generator_fixture(payload, schema=self.schema)

    def test_two_plus_one_requires_matching_profile_and_human_flag(self):
        payload = copy.deepcopy(self.fixture)
        replacement = next(
            article
            for article in payload["basket"]["articles"]
            if article["article_id"] == "EN-A-LATER"
        )
        replacement["source_id"] = "en-source-a-substitute"
        replacement["source_name"] = "English Outlet A Substitute"
        replacement["replacement_kind"] = "SIMILAR_OUTLET"
        replacement["substitute_for_source_id"] = "en-source-a"
        replacement["requires_human_approval"] = True
        payload["basket"]["basket_hash"] = basket_content_hash(payload["basket"])
        validate_case_generator_fixture(payload, schema=self.schema)

        replacement["editorial_profile"] = "DIFFERENT_PROFILE"
        payload["basket"]["basket_hash"] = basket_content_hash(payload["basket"])
        with self.assertRaisesRegex(CaseGeneratorContractError, "different editorial profile"):
            validate_case_generator_fixture(payload, schema=self.schema)

    def test_two_substitute_articles_plus_one_primary_are_allowed_for_gate_review(self):
        payload = copy.deepcopy(self.fixture)
        replacements = [
            article
            for article in payload["basket"]["articles"]
            if article["publisher_slot"] == "en-local-a"
        ][:2]
        for replacement in replacements:
            replacement["source_id"] = "en-source-a-substitute"
            replacement["source_name"] = "English Outlet A Substitute"
            replacement["replacement_kind"] = "SIMILAR_OUTLET"
            replacement["substitute_for_source_id"] = "en-source-a"
            replacement["requires_human_approval"] = True
        payload["basket"]["basket_hash"] = basket_content_hash(payload["basket"])

        validate_case_generator_fixture(payload, schema=self.schema)

    def test_two_substitute_outlets_without_primary_are_allowed_for_gate_review(self):
        payload = copy.deepcopy(self.fixture)
        replacements = [
            article
            for article in payload["basket"]["articles"]
            if article["publisher_slot"] == "en-local-a"
        ]
        for index, replacement in enumerate(replacements):
            replacement["source_id"] = (
                "en-source-a-substitute-one"
                if index < 2
                else "en-source-a-substitute-two"
            )
            replacement["source_name"] = "English Substitute"
            replacement["replacement_kind"] = "SIMILAR_OUTLET"
            replacement["substitute_for_source_id"] = "en-source-a"
            replacement["requires_human_approval"] = True
        payload["basket"]["basket_hash"] = basket_content_hash(payload["basket"])

        validate_case_generator_fixture(payload, schema=self.schema)


if __name__ == "__main__":
    unittest.main()
