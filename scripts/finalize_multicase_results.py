#!/usr/bin/env python3
"""Build deterministic final score artifacts for an adjudicated multicase case."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_results import (
    build_tf_score_records,
    build_scoring_readiness,
    build_t4_trajectories,
    load_t4_candidates,
)
from aletheia_lite.case_workspace import CaseWorkspace
from aletheia_lite.judgment_room import JudgmentStore, build_effective_rows
from aletheia_lite.multicase_material import (
    load_multicase_material,
    load_workspace_corpus,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_title(workspace: CaseWorkspace) -> str:
    connection = sqlite3.connect(workspace.research_db)
    try:
        row = connection.execute(
            "SELECT title FROM cases WHERE case_id = ?", (workspace.case_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not str(row[0]).strip():
        raise ValueError("case title is missing")
    return str(row[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args()

    workspace = CaseWorkspace(ROOT, args.case_id)
    workspace.results_dir.mkdir(parents=True, exist_ok=True)
    readiness_path = workspace.results_dir / "scoring_readiness_v1.json"
    results_path = workspace.results_dir / "final_results_v1.json"
    packet_path = workspace.results_dir / "t4_review_packet_v1.json"
    closeout_path = workspace.results_dir / "closeout_v1.json"
    candidates_path = workspace.results_dir / "t4_candidates_v1.json"
    frame_path = workspace.results_dir / "frame_intensity_results_v1.json"

    material = load_multicase_material(workspace)
    corpus = load_workspace_corpus(workspace)
    store = JudgmentStore(workspace.adjudication_dir / "judgment_room.sqlite3")
    store.sync_score_ledger(material)
    article_decisions = store.current_decisions()
    effective_rows = build_effective_rows(material, article_decisions)
    candidates = load_t4_candidates(ROOT, candidates_path)
    trajectories = build_t4_trajectories(
        ROOT,
        material,
        effective_rows,
        corpus=corpus,
        candidates=candidates,
    )
    trajectory_decisions = store.current_trajectory_decisions()
    frame_results = json.loads(frame_path.read_text(encoding="utf-8"))
    records = build_tf_score_records(
        ROOT,
        material,
        effective_rows,
        trajectory_decisions,
        frame_results,
        corpus=corpus,
        case_label=_case_title(workspace),
    )
    if len(records) != 40:
        raise ValueError(f"expected 40 score records, found {len(records)}")
    case_record = next(record for record in records if record.level == "CASE")
    readiness = build_scoring_readiness(
        material, effective_rows, trajectories, trajectory_decisions
    )
    # N remains available in the audit ledger, but it cannot create an active
    # result flag under TF-2D/1.0.  Only T-derived flags survive closeout.
    readiness["critical_flags"] = case_record.critical_flags
    if readiness["effective_article_components"] != 216:
        raise ValueError(
            "multicase closeout requires 216 effective article components"
        )
    if readiness["status"] != "READY":
        raise ValueError(f"case is not ready for scoring: {readiness}")

    generated_at = max(
        [item["decided_at"] for item in article_decisions.values()]
        + [item["decided_at"] for item in trajectory_decisions.values()]
    )
    readiness["generated_at"] = generated_at
    _write(readiness_path, readiness)
    _write(
        packet_path,
        {
            "schema_version": "1.0",
            "case_id": material.case_id,
            "generated_at": generated_at,
            "trajectories": trajectories,
        },
    )

    payload = {
        "schema_version": "1.0",
        "methodology_version": "TF-2D/1.0",
        "case_id": material.case_id,
        "status": "CLOSED",
        "generated_at": generated_at,
        "outcome_architecture": "T_AND_F_INDEPENDENT_NO_COMPOSITE",
        "formula": None,
        "legacy_fields": {
            "narrative_pressure": "NOT_AGGREGATED_DIAGNOSTIC_ONLY",
            "aletheia_65_35": "RETIRED_NOT_COMPUTED",
            "aletheia_50_50": "RETIRED_NOT_COMPUTED",
            "aletheia_75_25": "RETIRED_NOT_COMPUTED",
        },
        "records": [record.to_dict() for record in records],
    }
    _write(results_path, payload)
    _write(
        closeout_path,
        {
            "schema_version": "1.0",
            "case_id": material.case_id,
            "status": "CLOSED",
            "closed_at": generated_at,
            "article_components": readiness["effective_article_components"],
            "human_article_decisions": readiness["human_article_decisions"],
            "trajectory_decisions": readiness["trajectory_decisions"],
            "score_records": len(records),
            "critical_flags": case_record.critical_flags,
            "case_scores": {
                "truth_score": case_record.truth_score,
                "frame_intensity": case_record.details["frame_intensity"]["score"],
                "truth_coverage": case_record.truth_coverage,
                "frame_coverage": case_record.details["frame_intensity"]["coverage"],
            },
            "artifacts": {
                "results": str(results_path.relative_to(ROOT)),
                "results_sha256": _sha256(results_path),
                "readiness": str(readiness_path.relative_to(ROOT)),
                "readiness_sha256": _sha256(readiness_path),
                "t4_packet": str(packet_path.relative_to(ROOT)),
                "t4_packet_sha256": _sha256(packet_path),
                "frame_results": str(frame_path.relative_to(ROOT)),
                "frame_results_sha256": _sha256(frame_path),
            },
        },
    )
    print(
        json.dumps(
            {
                "case_id": material.case_id,
                "status": "CLOSED",
                "article_components": readiness["effective_article_components"],
                "human_article_decisions": readiness["human_article_decisions"],
                "trajectory_decisions": readiness["trajectory_decisions"],
                "score_records": len(records),
                "case_scores": json.loads(closeout_path.read_text())["case_scores"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
