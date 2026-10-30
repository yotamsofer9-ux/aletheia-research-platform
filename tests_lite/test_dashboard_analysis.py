from __future__ import annotations

import unittest

from aletheia_lite.case_results import ScoreRecord
from aletheia_lite.dashboard_analysis import (
    build_dashboard_dataset,
    dashboard_snapshot,
    flagged_articles,
    frame_family_prevalence,
    paired_trajectory_deltas,
)


def score_record(
    *,
    case_id: str,
    level: str,
    entity_id: str,
    label: str,
    truth: float,
    language: str | None,
    details: dict,
    flags: list[str] | None = None,
) -> ScoreRecord:
    return ScoreRecord(
        row_id=f"{case_id}:{level}:{entity_id}",
        case_id=case_id,
        level=level,
        entity_id=entity_id,
        language=language,
        label=label,
        truth_score=truth,
        narrative_pressure=81.0,
        aletheia_65_35=77.0,
        aletheia_50_50=76.0,
        aletheia_75_25=78.0,
        truth_coverage=1.0,
        narrative_coverage=1.0,
        overall_coverage=1.0,
        critical_flags=flags or [],
        details=details,
    )


def case_bundle(case_id: str, label: str, offset: float = 0.0):
    documents = []
    frames = []
    for document_id, phase, truth, frame in (
        (f"{case_id}-initial", "INITIAL_REPORT", 70.0 + offset, 80.0),
        (f"{case_id}-later", "LATER_INVESTIGATION", 90.0 + offset, 70.0),
    ):
        documents.append(
            score_record(
                case_id=case_id,
                level="DOCUMENT",
                entity_id=document_id,
                label=document_id,
                truth=truth,
                language="en",
                details={
                    "source_name": "Example News",
                    "publisher_slot": "western-example",
                    "phase": phase,
                    "component_scores": {"T1": 4, "T2": 3, "T3": 4},
                },
                flags=["CRITICAL_FACT_CONFLICT"]
                if phase == "INITIAL_REPORT" and case_id == "CASE-001"
                else [],
            )
        )
        frames.append(
            {
                "document_id": document_id,
                "frame_score": frame,
                "component_scores": {
                    "F1": 4,
                    "F2": 3,
                    "F3": 2,
                    "F4": 1,
                    "F5": 3,
                },
                "frame_families": ["CONFLICT", "RESPONSIBILITY"],
            }
        )
    slot = score_record(
        case_id=case_id,
        level="PUBLISHER_SLOT",
        entity_id="western-example",
        label="Example News",
        truth=80.0 + offset,
        language="en",
        details={"component_means": {"T4": 4}, "t4_na_reason": None},
    )
    case = score_record(
        case_id=case_id,
        level="CASE",
        entity_id=case_id,
        label=label,
        truth=80.0 + offset,
        language=None,
        details={"frame_intensity": {"score": 75.0}},
    )
    frame_payload = {
        "distribution": {"mean": 75.0},
        "records": frames,
    }
    return case_id, label, [*documents, slot, case], frame_payload


class DashboardAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.dataset = build_dashboard_dataset(
            [
                case_bundle("CASE-001", "קייס א"),
                case_bundle("CASE-002", "קייס ב", offset=5.0),
            ]
        )

    def test_builds_only_active_tf_columns(self):
        self.assertEqual(len(self.dataset.articles), 4)
        self.assertEqual(len(self.dataset.cases), 2)
        self.assertEqual(len(self.dataset.slots), 2)
        self.assertNotIn("N", self.dataset.articles.columns)
        self.assertNotIn("A", self.dataset.articles.columns)
        snapshot = dashboard_snapshot(self.dataset)
        self.assertEqual(snapshot["articles"], 4)
        self.assertEqual(snapshot["complete_F"], 4)

    def test_calculates_paired_initial_to_later_change(self):
        deltas = paired_trajectory_deltas(self.dataset)
        self.assertEqual(len(deltas), 2)
        self.assertEqual(set(deltas["delta_T"]), {20.0})
        self.assertEqual(set(deltas["delta_F"]), {-10.0})

    def test_frame_families_are_multilabel_prevalence(self):
        prevalence = frame_family_prevalence(self.dataset)
        values = prevalence.set_index("family")["share"].to_dict()
        self.assertEqual(values["CONFLICT"], 1.0)
        self.assertEqual(values["RESPONSIBILITY"], 1.0)

    def test_flags_are_audit_rows_not_scores(self):
        flags = flagged_articles(self.dataset)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags.iloc[0]["case_id"], "CASE-001")
        self.assertEqual(
            flags.iloc[0]["critical_flags"], ("CRITICAL_FACT_CONFLICT",)
        )


if __name__ == "__main__":
    unittest.main()
