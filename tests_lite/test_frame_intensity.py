from __future__ import annotations

import copy
import csv
import json
import unittest
from pathlib import Path

from aletheia_lite.frame_intensity import (
    COMPONENTS,
    FrameMeasurement,
    build_score_gap_review,
    frame_score,
    load_schema,
    normalize_frame_output_limits,
    resolve_verbatim_cues,
    validate_frame_measurement,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(ROOT / "measurement" / "frame_intensity_schema.json")


def valid_payload(*, language: str = "he") -> tuple[dict, dict]:
    quotes = {
        "he": [
            "האירוע הוגדר כאסון הומניטרי",
            "הדיווח ייחס אחריות לצבא",
            "המעשה תואר כבלתי מוסרי",
            "הכתבה קראה לחקירה",
            "הביטוי הופיע שוב ושוב",
        ],
        "en": [
            "The event was framed as a humanitarian disaster",
            "The report assigned responsibility to the military",
            "The act was described as immoral",
            "The article called for an investigation",
            "The phrase was repeated throughout the article",
        ],
        "ar": [
            "وُصف الحدث بأنه كارثة إنسانية",
            "نسب التقرير المسؤولية إلى الجيش",
            "وُصف الفعل بأنه غير أخلاقي",
            "دعا التقرير إلى إجراء تحقيق",
            "تكررت العبارة في أنحاء المقال",
        ],
    }[language]
    translations = [None] * 5 if language == "he" else [
        "האירוע הוצג כאסון הומניטרי",
        "הדיווח ייחס אחריות לצבא",
        "המעשה תואר כבלתי מוסרי",
        "הכתבה קראה לחקירה",
        "הביטוי חזר לאורך הכתבה",
    ]
    functions = [
        "PROBLEM_DEFINITION",
        "AGENCY_CAUSALITY",
        "MORAL_EVALUATION",
        "TREATMENT_DIRECTION",
        "RHETORICAL_REINFORCEMENT",
    ]
    locations = ["HEADLINE", "LEAD", "HEADLINE", "LEAD", "BODY"]
    cues = []
    for index, (quote, translation, function, location) in enumerate(
        zip(quotes, translations, functions, locations), start=1
    ):
        cues.append(
            {
                "cue_id": f"cue_{index}",
                "quote_original": quote,
                "location": location,
                "owner": "OUTLET",
                "function": function,
                "device": "PLAIN_ASSERTION",
                "reviewer_translation_he": translation,
            }
        )
    components = {
        code: {
            "score": score,
            "confidence": "HIGH",
            "rationale_he": f"נימוק בדיקה עבור {code}",
            "evidence_cue_ids": [f"cue_{index}"],
            "needs_review_reasons": [],
        }
        for index, (code, score) in enumerate(zip(COMPONENTS, [2, 2, 3, 3, 2]), start=1)
    }
    payload = {
        "schema_version": "0.1.0",
        "methodology_version": "frame-intensity-v0.1.0",
        "prompt_version": "case002-frame-intensity-v0.1.0",
        "case_id": "CASE-002",
        "document_id": "doc_test",
        "language": language,
        "analysis_status": "COMPLETE",
        "coverage_scope": "FULL_TEXT",
        "frame_statement_he": "הכתבה ממסגרת את האירוע באמצעות אחריות, מוסר ודרישה לחקירה.",
        "frame_families": ["HUMANITARIAN", "RESPONSIBILITY"],
        "frame_cues": cues,
        "components": components,
        "needs_review": {"required": False, "reasons": [], "note_he": None},
    }
    document = {
        "case_id": "CASE-002",
        "document_id": "doc_test",
        "language": language,
        "title": f"{quotes[0]} — {quotes[2]}",
        "text": "\n".join(quotes[1:]),
    }
    return payload, document


class FrameIntensityContractTests(unittest.TestCase):
    def test_valid_measurement_passes_and_score_is_equal_weighted(self):
        payload, document = valid_payload()
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsInstance(measurement, FrameMeasurement)
        self.assertEqual(frame_score(measurement), 60.0)

    def test_non_hebrew_evidence_requires_translation(self):
        payload, document = valid_payload(language="en")
        payload["frame_cues"][0]["reviewer_translation_he"] = None
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn("cues:cue_1:non-Hebrew evidence requires translation", errors)

    def test_exact_evidence_span_must_exist_in_source(self):
        payload, document = valid_payload()
        payload["frame_cues"][1]["quote_original"] = "ציטוט שאינו קיים במקור"
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn("cues:cue_2:exact evidence span not found", errors)

    def test_unresolved_f5_cue_is_replaced_by_other_selected_exact_cue(self):
        payload, document = valid_payload()
        payload["frame_cues"][4]["quote_original"] = "ניסוח מחובר שאינו מופיע במקור"
        payload["components"]["F5"]["evidence_cue_ids"] = ["cue_5", "cue_1"]
        corrected, log = resolve_verbatim_cues(payload, document=document)
        cited = set(corrected["components"]["F5"]["evidence_cue_ids"])
        cues = {cue["cue_id"]: cue for cue in corrected["frame_cues"]}
        self.assertTrue(
            all(cues[cue_id]["quote_original"] in document["title"] + "\n" + document["text"] for cue_id in cited)
        )
        self.assertTrue(
            any(
                item.get("method")
                == "REPLACE_UNRESOLVED_F5_WITH_SELECTED_EXACT_CUE"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_positive_component_requires_matching_frame_function(self):
        payload, document = valid_payload()
        payload["components"]["F2"]["evidence_cue_ids"] = ["cue_1"]
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn("components:F2:positive score lacks AGENCY_CAUSALITY cue", errors)

    def test_headline_evidence_must_come_from_title(self):
        payload, document = valid_payload()
        payload["frame_cues"][0]["quote_original"] = document["text"].splitlines()[0]
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn("cues:cue_1:HEADLINE evidence not found in title", errors)

    def test_low_confidence_must_route_human_review(self):
        payload, document = valid_payload()
        payload["components"]["F2"]["confidence"] = "LOW"
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn(
            "components:F2:LOW confidence must route LOW_CONFIDENCE review", errors
        )

    def test_unclear_voice_must_route_human_review(self):
        payload, document = valid_payload()
        payload["frame_cues"][0]["owner"] = "UNCLEAR"
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn(
            "cues:UNCLEAR ownership must route SOURCE_VOICE_AMBIGUITY", errors
        )

    def test_near_exact_long_span_is_replaced_by_exact_source_and_logged(self):
        payload, document = valid_payload(language="en")
        exact = (
            "The event was framed as a humanitarian disaster with a clear and "
            "repeated account of the consequences"
        )
        document["title"] = exact
        payload["frame_cues"][0]["quote_original"] = exact + "."
        corrected, log = resolve_verbatim_cues(payload, document=document)
        self.assertEqual(corrected["frame_cues"][0]["quote_original"], exact)
        self.assertEqual(log[0]["method"], "LONGEST_EXACT_SOURCE_SUBSTRING")
        self.assertFalse(log[0]["requires_review"])

    def test_short_headline_cue_with_one_letter_difference_is_made_verbatim(self):
        payload, document = valid_payload()
        document["title"] = "המתקפה האיראנית: כך נראה הלילה הדרמטי בבור של חיל האוויר"
        payload["frame_cues"][0]["quote_original"] = "הבור של חיל האוויר"

        corrected, log = resolve_verbatim_cues(payload, document=document)

        self.assertEqual(
            corrected["frame_cues"][0]["quote_original"], "בבור של חיל האוויר"
        )
        self.assertEqual(
            log[0]["method"], "HIGH_SIMILARITY_VERBATIM_HEADLINE_WINDOW"
        )
        self.assertFalse(log[0]["requires_review"])

    def test_unresolved_cue_is_dropped_only_when_exact_function_evidence_remains(self):
        payload, document = valid_payload()
        redundant = copy.deepcopy(payload["frame_cues"][1])
        redundant["cue_id"] = "cue_6"
        redundant["quote_original"] = "ניסוח שאינו קיים כלל במקור"
        payload["frame_cues"].append(redundant)
        payload["components"]["F2"]["evidence_cue_ids"] = ["cue_2", "cue_6"]

        corrected, log = resolve_verbatim_cues(payload, document=document)
        self.assertNotIn(
            "cue_6", {cue["cue_id"] for cue in corrected["frame_cues"]}
        )
        self.assertEqual(
            corrected["components"]["F2"]["evidence_cue_ids"], ["cue_2"]
        )
        self.assertTrue(
            any(
                item.get("method") == "DROP_UNRESOLVED_REDUNDANT_CUE"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_low_ratio_repair_is_dropped_when_it_is_not_decisive(self):
        payload, document = valid_payload(language="en")
        exact = (
            "The report assigned responsibility to the military and described "
            "the sequence of decisions in a precise and fully attributed account"
        )
        original = payload["frame_cues"][1]["quote_original"]
        payload["frame_cues"][1]["quote_original"] = exact
        document["text"] = document["text"].replace(original, exact)
        redundant = copy.deepcopy(payload["frame_cues"][1])
        redundant["cue_id"] = "cue_6"
        redundant["quote_original"] = exact + " plus absent text"
        payload["frame_cues"].append(redundant)
        payload["components"]["F2"]["evidence_cue_ids"] = ["cue_2", "cue_6"]

        corrected, log = resolve_verbatim_cues(payload, document=document)
        self.assertNotIn(
            "cue_6", {cue["cue_id"] for cue in corrected["frame_cues"]}
        )
        self.assertFalse(corrected["needs_review"]["required"])
        self.assertTrue(
            any(
                item.get("method") == "DROP_LOW_RATIO_REDUNDANT_CUE"
                for item in log
            )
        )

    def test_output_limits_are_normalized_without_rewriting_words(self):
        payload, _ = valid_payload()
        long_rationale = "משפט ראשון שמסביר את הציון. " + ("המשך ארוך " * 60)
        payload["components"]["F3"]["rationale_he"] = long_rationale
        payload["components"]["F3"]["evidence_cue_ids"] = [
            "cue_1",
            "cue_2",
            "cue_3",
            "cue_4",
            "cue_5",
            "cue_6",
            "cue_7",
        ]
        corrected, log = normalize_frame_output_limits(payload)
        rationale = corrected["components"]["F3"]["rationale_he"]
        self.assertLessEqual(len(rationale), 420)
        self.assertTrue(long_rationale.startswith(rationale))
        self.assertEqual(
            corrected["components"]["F3"]["evidence_cue_ids"],
            ["cue_1", "cue_2", "cue_3", "cue_4", "cue_5", "cue_6"],
        )
        self.assertEqual(len(log), 2)

    def test_non_headline_selected_quote_owner_is_normalized(self):
        payload, document = valid_payload()
        payload["frame_cues"][3]["owner"] = "HEADLINE_SELECTED_QUOTE"
        corrected, log = normalize_frame_output_limits(payload)
        self.assertEqual(
            corrected["frame_cues"][3]["owner"], "ATTRIBUTED_ACTOR"
        )
        self.assertTrue(
            any(
                item.get("method")
                == "NORMALIZE_NON_HEADLINE_SELECTED_QUOTE_OWNER"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_long_exact_quote_is_shortened_to_exact_schema_safe_prefix(self):
        payload, document = valid_payload()
        long_quote = ("ראיה ארוכה מאוד שמופיעה במקור " * 25).strip()
        payload["frame_cues"][1]["quote_original"] = long_quote
        document["text"] = long_quote + "\n" + document["text"]
        corrected, log = normalize_frame_output_limits(payload)
        shortened = corrected["frame_cues"][1]["quote_original"]
        self.assertLessEqual(len(shortened), 500)
        self.assertTrue(long_quote.startswith(shortened))
        self.assertIn(shortened, document["text"])
        self.assertTrue(
            any(
                item.get("method") == "EXACT_PREFIX_TO_SCHEMA_QUOTE_LIMIT"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_zero_component_may_have_no_positive_evidence(self):
        payload, document = valid_payload()
        payload["components"]["F4"]["score"] = 0
        payload["components"]["F4"]["evidence_cue_ids"] = []
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_positive_f5_selected_evidence_gets_auditable_function_duplicate(self):
        payload, document = valid_payload()
        payload["components"]["F5"]["score"] = 3
        payload["components"]["F5"]["evidence_cue_ids"] = ["cue_1", "cue_2"]
        payload["frame_cues"] = [
            cue
            for cue in payload["frame_cues"]
            if cue["function"] != "RHETORICAL_REINFORCEMENT"
        ]
        document["text"] = "\n".join(
            cue["quote_original"] for cue in payload["frame_cues"][1:]
        )

        corrected, log = normalize_frame_output_limits(payload)
        reinforcement = [
            cue
            for cue in corrected["frame_cues"]
            if cue["function"] == "RHETORICAL_REINFORCEMENT"
        ]
        self.assertEqual(len(reinforcement), 1)
        self.assertIn(
            reinforcement[0]["cue_id"],
            corrected["components"]["F5"]["evidence_cue_ids"],
        )
        self.assertTrue(
            any(
                item.get("method") == "DUPLICATE_MODEL_SELECTED_F5_EVIDENCE"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_multifunction_component_evidence_gets_exact_duplicate(self):
        payload, document = valid_payload()
        payload["components"]["F3"]["score"] = 1
        payload["components"]["F3"]["evidence_cue_ids"] = ["cue_2"]

        corrected, log = normalize_frame_output_limits(payload)
        f3_ids = corrected["components"]["F3"]["evidence_cue_ids"]
        f3_cues = [
            cue for cue in corrected["frame_cues"] if cue["cue_id"] in f3_ids
        ]
        self.assertTrue(
            any(cue["function"] == "MORAL_EVALUATION" for cue in f3_cues)
        )
        self.assertTrue(
            any(
                item.get("method")
                == "DUPLICATE_MODEL_SELECTED_COMPONENT_EVIDENCE"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_obsolete_zero_evidence_schema_review_is_cleared_only_by_signature(self):
        payload, document = valid_payload()
        payload["components"]["F4"]["score"] = 0
        payload["components"]["F4"]["evidence_cue_ids"] = []
        payload["analysis_status"] = "NEEDS_REVIEW"
        payload["needs_review"] = {
            "required": True,
            "reasons": ["OTHER"],
            "note_he": "סתירה טכנית ב-minItems עבור ציון 0",
        }

        corrected, log = normalize_frame_output_limits(payload)
        self.assertEqual(corrected["analysis_status"], "COMPLETE")
        self.assertEqual(
            corrected["needs_review"],
            {"required": False, "reasons": [], "note_he": None},
        )
        self.assertTrue(
            any(
                item.get("method")
                == "CLEAR_OBSOLETE_ZERO_EVIDENCE_SCHEMA_CONTRADICTION"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_cue_capacity_trim_preserves_every_referenced_cue(self):
        payload, document = valid_payload()
        template = payload["frame_cues"][0]
        for number in range(6, 23):
            cue = copy.deepcopy(template)
            cue["cue_id"] = f"cue_{number}"
            cue["quote_original"] = f"סימן מסגור עודף מספר {number}"
            cue["location"] = "BODY"
            payload["frame_cues"].append(cue)
            document["text"] += "\n" + cue["quote_original"]
        payload["components"]["F1"]["evidence_cue_ids"] = ["cue_1", "cue_22"]

        corrected, log = normalize_frame_output_limits(payload)
        kept = {cue["cue_id"] for cue in corrected["frame_cues"]}
        self.assertEqual(len(corrected["frame_cues"]), 20)
        self.assertIn("cue_22", kept)
        self.assertTrue(
            any(
                item.get("method") == "DROP_UNCITED_CUES_TO_FROZEN_CAPACITY"
                for item in log
            )
        )

    def test_high_moral_score_rejects_attributed_body_only_evidence(self):
        payload, document = valid_payload()
        payload["frame_cues"][2]["owner"] = "ATTRIBUTED_ACTOR"
        payload["frame_cues"][2]["location"] = "BODY"
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertIsNone(measurement)
        self.assertIn(
            "components:F3:score 3-4 lacks prominent or repeated editorial evidence",
            errors,
        )

    def test_normalizer_caps_high_moral_score_without_qualifying_prominence(self):
        payload, document = valid_payload()
        payload["frame_cues"][2]["owner"] = "ATTRIBUTED_ACTOR"
        payload["frame_cues"][2]["location"] = "BODY"
        corrected, log = normalize_frame_output_limits(payload)
        self.assertEqual(corrected["components"]["F3"]["score"], 2)
        self.assertTrue(
            any(
                item.get("method")
                == "CAP_HIGH_SCORE_WITHOUT_QUALIFYING_PROMINENCE"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_normalizer_injects_missing_review_note_from_reasons(self):
        payload, document = valid_payload()
        payload["analysis_status"] = "NEEDS_REVIEW"
        payload["needs_review"] = {
            "required": True,
            "reasons": ["LOW_CONFIDENCE"],
            "note_he": None,
        }
        payload["components"]["F1"]["confidence"] = "LOW"
        payload["components"]["F1"]["needs_review_reasons"] = ["LOW_CONFIDENCE"]
        corrected, log = normalize_frame_output_limits(payload)
        self.assertIn("LOW_CONFIDENCE", corrected["needs_review"]["note_he"])
        self.assertTrue(
            any(
                item.get("method") == "INJECT_REVIEW_NOTE_FROM_MODEL_REASONS"
                for item in log
            )
        )
        measurement, errors = validate_frame_measurement(
            corrected, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_high_moral_score_accepts_repeated_structurally_selected_quotes(self):
        payload, document = valid_payload()
        payload["frame_cues"][2]["owner"] = "ATTRIBUTED_ACTOR"
        payload["frame_cues"][2]["location"] = "BODY"
        payload["frame_cues"][2]["device"] = "QUOTE_SELECTION"
        duplicate = copy.deepcopy(payload["frame_cues"][2])
        duplicate["cue_id"] = "cue_6"
        duplicate["quote_original"] = "הערכה מוסרית נוספת שנבחרה לציטוט"
        document["text"] += "\n" + duplicate["quote_original"]
        payload["frame_cues"].append(duplicate)
        payload["components"]["F3"]["evidence_cue_ids"].append("cue_6")
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_high_moral_score_accepts_prominent_attributed_cue_with_recurrence(self):
        payload, document = valid_payload()
        payload["frame_cues"][2]["owner"] = "ATTRIBUTED_ACTOR"
        payload["frame_cues"][2]["location"] = "LEAD"
        duplicate = copy.deepcopy(payload["frame_cues"][2])
        duplicate["cue_id"] = "cue_6"
        duplicate["quote_original"] = "הערכה מוסרית מיוחסת נוספת בגוף הכתבה"
        duplicate["location"] = "BODY"
        document["text"] += "\n" + duplicate["quote_original"]
        payload["frame_cues"].append(duplicate)
        payload["components"]["F3"]["evidence_cue_ids"].append("cue_6")
        measurement, errors = validate_frame_measurement(
            payload, schema=SCHEMA, document=document
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(measurement)

    def test_one_point_second_review_gap_is_accepted(self):
        first_raw, _ = valid_payload()
        second_raw = copy.deepcopy(first_raw)
        second_raw["components"]["F1"]["score"] = 3
        first = FrameMeasurement.model_validate(first_raw)
        second = FrameMeasurement.model_validate(second_raw)
        review = build_score_gap_review(first, second)
        self.assertFalse(review["requires_human_review"])
        self.assertEqual(review["components"]["F1"]["provisional_score"], 3)

    def test_two_point_second_review_gap_is_escalated(self):
        first_raw, _ = valid_payload()
        second_raw = copy.deepcopy(first_raw)
        second_raw["components"]["F1"]["score"] = 4
        first = FrameMeasurement.model_validate(first_raw)
        second = FrameMeasurement.model_validate(second_raw)
        review = build_score_gap_review(first, second)
        self.assertTrue(review["requires_human_review"])
        self.assertIn("SCORE_GAP", review["review_reasons"])
        self.assertIsNone(review["components"]["F1"]["provisional_score"])


@unittest.skipUnless(
    (ROOT / "data" / "cases" / "case-002" / "analysis" / "corpus.csv").exists(),
    "private CASE-002 integration corpus is not included in the public repository",
)
class FrameIntensityRosterTests(unittest.TestCase):
    def test_roster_covers_every_language_phase_cell_and_publisher_slot(self):
        corpus_path = ROOT / "data" / "cases" / "case-002" / "analysis" / "corpus.csv"
        roster_path = (
            ROOT
            / "data"
            / "cases"
            / "case-002"
            / "analysis"
            / "frame_calibration_roster_v1.json"
        )
        with corpus_path.open(encoding="utf-8-sig") as handle:
            corpus = {row["document_id"]: row for row in csv.DictReader(handle)}
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
        selected = [corpus[document_id] for document_id in roster["document_ids"]]
        self.assertEqual(len(selected), 9)
        self.assertEqual(len({row["publisher_slot"] for row in selected}), 9)
        self.assertEqual(
            {(row["language"], row["sample_phase"]) for row in selected},
            {
                (language, phase)
                for language in ("he", "ar", "en")
                for phase in ("INITIAL_REPORT", "EARLY_UPDATE", "LATER_INVESTIGATION")
            },
        )
        self.assertTrue(all(row["truncated"] == "0" for row in selected))

    def test_full_roster_is_exactly_the_frozen_27_article_corpus(self):
        corpus_path = ROOT / "data" / "cases" / "case-002" / "analysis" / "corpus.csv"
        roster_path = (
            ROOT
            / "data"
            / "cases"
            / "case-002"
            / "analysis"
            / "frame_full_roster_v1.json"
        )
        with corpus_path.open(encoding="utf-8-sig") as handle:
            corpus = {row["document_id"]: row for row in csv.DictReader(handle)}
        roster = json.loads(roster_path.read_text(encoding="utf-8"))

        self.assertEqual(roster["case_id"], "CASE-002")
        self.assertEqual(len(roster["document_ids"]), 27)
        self.assertEqual(len(set(roster["document_ids"])), 27)
        self.assertEqual(set(roster["document_ids"]), set(corpus))
        self.assertEqual(
            {(row["language"], row["sample_phase"]) for row in corpus.values()},
            {
                (language, phase)
                for language in ("he", "ar", "en")
                for phase in (
                    "INITIAL_REPORT",
                    "EARLY_UPDATE",
                    "LATER_INVESTIGATION",
                )
            },
        )


if __name__ == "__main__":
    unittest.main()
