#!/usr/bin/env python3
"""Initialize approved T4 N/A rules and build deterministic CASE-001 outputs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_results import (
    build_tf_score_records,
    build_scoring_readiness,
    build_t4_trajectories,
)
from aletheia_lite.judgment_room import (
    JudgmentStore,
    build_effective_rows,
    load_case_material,
)


DATABASE = ROOT / "data/judgment_room.sqlite3"
READINESS_PATH = ROOT / "analysis/case001_scoring_readiness_v1.json"
RESULTS_PATH = ROOT / "analysis/case001_final_results_v1.json"
T4_PACKET_PATH = ROOT / "analysis/case001_t4_review_packet_v1.json"
CLOSEOUT_PATH = ROOT / "analysis/case001_closeout_v1.json"
FRAME_RESULTS_PATH = ROOT / "analysis/case001_frame_intensity_results_v1.json"


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--initialize-approved-na", action="store_true",
        help="Save the two already-approved split-slot T4 N/A decisions.",
    )
    args = parser.parse_args()

    material = load_case_material(ROOT)
    store = JudgmentStore(DATABASE)
    store.sync_score_ledger(material)
    current = store.current_decisions()
    effective_rows = build_effective_rows(material, current)
    trajectories = build_t4_trajectories(ROOT, material, effective_rows)

    if args.initialize_approved_na:
        existing = store.current_trajectory_decisions()
        for trajectory in trajectories:
            proposal = trajectory["proposal"]
            if not trajectory["split_slot"] or trajectory["trajectory_id"] in existing:
                continue
            store.save_trajectory_decision(
                trajectory,
                score=None,
                na_reason=proposal["proposed_na_reason"],
                rationale=proposal["rationale_he"],
                decided_by="APPROVED_METHOD_RULE",
            )

    trajectory_decisions = store.current_trajectory_decisions()
    readiness = build_scoring_readiness(
        material, effective_rows, trajectories, trajectory_decisions
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    if readiness["status"] == "READY":
        # Once the case is closed, its canonical timestamp is the final current
        # T4 judgment rather than the wall clock of a later reproducibility run.
        generated_at = max(
            decision["decided_at"] for decision in trajectory_decisions.values()
        )
    readiness["generated_at"] = generated_at
    _write(READINESS_PATH, readiness)
    _write(T4_PACKET_PATH, {
        "schema_version": "1.0",
        "case_id": material.case_id,
        "generated_at": readiness["generated_at"],
        "trajectories": trajectories,
    })

    output = {"readiness": readiness, "results_written": False}
    if readiness["status"] == "READY":
        frame_results = json.loads(FRAME_RESULTS_PATH.read_text(encoding="utf-8"))
        records = build_tf_score_records(
            ROOT,
            material,
            effective_rows,
            trajectory_decisions,
            frame_results,
            case_label="הפיצוץ בבית החולים אל־אהלי",
        )
        payload = {
            "schema_version": "1.0",
            "case_id": material.case_id,
            "status": "CLOSED",
            "generated_at": readiness["generated_at"],
            "methodology_version": "TF-2D/1.0",
            "outcome_architecture": "T_AND_F_INDEPENDENT_NO_COMPOSITE",
            "formula": {
                "T": "100 * weighted mean of T1-T4 over available T weight",
                "F": "100 * (F1 + F2 + F3 + F4 + F5) / 20",
                "composite": None,
            },
            "legacy_fields": "NULL_FOR_SCHEMA_COMPATIBILITY",
            "records": [record.to_dict() for record in records],
        }
        _write(RESULTS_PATH, payload)
        case_record = next(
            record for record in records if record.level == "CASE"
        )
        _write(CLOSEOUT_PATH, {
            "schema_version": "1.0",
            "case_id": material.case_id,
            "status": "CLOSED",
            "closed_at": readiness["generated_at"],
            "article_components": readiness["effective_article_components"],
            "trajectory_decisions": readiness["trajectory_decisions"],
            "score_records": len(records),
            "critical_flags": case_record.critical_flags,
            "case_scores": {
                "truth_score": case_record.truth_score,
                "frame_intensity": case_record.details["frame_intensity"]["score"],
                "composite_score": None,
                "overall_coverage": case_record.overall_coverage,
            },
            "artifacts": {
                "results": str(RESULTS_PATH.relative_to(ROOT)),
                "results_sha256": _sha256(RESULTS_PATH),
                "readiness": str(READINESS_PATH.relative_to(ROOT)),
                "t4_packet": str(T4_PACKET_PATH.relative_to(ROOT)),
            },
        })
        output["results_written"] = True
        output["case_closed"] = True
        output["score_records"] = len(records)
    else:
        for path in (RESULTS_PATH, CLOSEOUT_PATH):
            if path.exists():
                path.unlink()

    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
