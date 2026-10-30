#!/usr/bin/env python3
"""Materialize CASE-001 anchor v1 from immutable drafts and adjudications."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRAFT = ROOT / "anchors" / "case001_factual_anchors_draft.jsonl"
DEFAULT_REVIEWS = [
    ROOT / "anchors" / "model_reviews" / "case001_vertex_reviews.jsonl",
    ROOT / "anchors" / "model_reviews" / "case001_vertex_attribution_rereview.jsonl",
]
DEFAULT_OVERRIDE = ROOT / "anchors" / "case001_human_override_pij.json"
DEFAULT_ANCHORS_OUTPUT = ROOT / "anchors" / "case001_factual_anchors_v1.jsonl"
DEFAULT_ADJUDICATIONS_OUTPUT = ROOT / "anchors" / "case001_adjudications_v1.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
    )
    path.write_text(text + "\n")


def review_ref(path: Path, anchor_id: str) -> str:
    return f"{path.relative_to(ROOT)}#{anchor_id}"


def load_latest_reviews(paths: list[Path]) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    for path in paths:
        for record in read_jsonl(path):
            for assessment in record["model_output"]["assessment_reviews"]:
                reviews[assessment["assessment_id"]] = {
                    "path": path,
                    "record": record,
                    "assessment": assessment,
                }
    return reviews


def strip_materialization(record: dict[str, Any]) -> dict[str, Any]:
    clean = copy.deepcopy(record)
    clean.pop("materialization", None)
    return clean


def model_adjudication(
    anchor: dict[str, Any],
    assessment: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    record = review["record"]
    item = review["assessment"]
    if item["verdict"] != "APPROVE":
        raise ValueError(f"{assessment['assessment_id']} is not model-approved")
    if item["recommended_epistemic_status"] != assessment["epistemic_status"]:
        raise ValueError(f"status mismatch for {assessment['assessment_id']}")
    if item["recommended_confidence"] != assessment["confidence"]:
        raise ValueError(f"confidence mismatch for {assessment['assessment_id']}")
    evidence_ids = list(dict.fromkeys(ref["source_id"] for ref in assessment["evidence_refs"]))
    return {
        "schema_version": "0.1",
        "adjudication_id": f"CASE001-ADJ-{assessment['assessment_id'].removeprefix('CASE001-')}",
        "case_id": anchor["case_id"],
        "anchor_id": anchor["anchor_id"],
        "assessment_id": assessment["assessment_id"],
        "outcome": "MODEL_APPROVED",
        "decided_at": {
            "value": record["reviewed_at"],
            "precision": "DATETIME",
        },
        "decided_by": f"vertex:{record['model']}",
        "basis_category": "MODEL_REVIEW_ONLY",
        "rationale": item["rationale"],
        "preserved_model_review_refs": [review_ref(review["path"], anchor["anchor_id"])],
        "evidence_source_ids": evidence_ids,
        "resulting_epistemic_status": assessment["epistemic_status"],
        "resulting_confidence": assessment["confidence"],
        "notes": "Materialized only because the current draft status and confidence exactly match the saved Vertex approval.",
    }


def materialize(
    draft_path: Path,
    review_paths: list[Path],
    override_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anchors = read_jsonl(draft_path)
    reviews = load_latest_reviews(review_paths)
    override = json.loads(override_path.read_text())
    override_assessment_id = override["assessment_id"]
    adjudications: list[dict[str, Any]] = []

    for anchor in anchors:
        for assessment in anchor["assessments"]:
            assessment_id = assessment["assessment_id"]
            if assessment_id == override_assessment_id:
                patch = override["materialization"]
                assessment["epistemic_status"] = override["resulting_epistemic_status"]
                assessment["confidence"] = override["resulting_confidence"]
                assessment["basis_summary"] = patch["basis_summary"]
                known_sources = {ref["source_id"] for ref in assessment["evidence_refs"]}
                for evidence_ref in patch["additional_evidence_refs"]:
                    if evidence_ref["source_id"] not in known_sources:
                        assessment["evidence_refs"].append(evidence_ref)
                assessment["review_status"] = "HUMAN_OVERRIDE"
                assessment["reviewed_by"] = override["decided_by"]
                assessment["reviewed_at"] = override["decided_at"]
                assessment["review_notes"] = (
                    f"See {override['adjudication_id']}; saved Vertex disagreement is preserved."
                )
                adjudications.append(strip_materialization(override))
                continue

            review = reviews.get(assessment_id)
            if review is None:
                raise ValueError(f"missing Vertex review for {assessment_id}")
            adjudication = model_adjudication(anchor, assessment, review)
            assessment["review_status"] = "MODEL_APPROVED"
            assessment["reviewed_by"] = adjudication["decided_by"]
            assessment["reviewed_at"] = adjudication["decided_at"]
            assessment["review_notes"] = (
                f"See {adjudication['adjudication_id']} and "
                f"{adjudication['preserved_model_review_refs'][0]}."
            )
            adjudications.append(adjudication)

    return anchors, adjudications


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate without writing")
    args = parser.parse_args()
    anchors, adjudications = materialize(DEFAULT_DRAFT, DEFAULT_REVIEWS, DEFAULT_OVERRIDE)
    if not args.check:
        write_jsonl(DEFAULT_ANCHORS_OUTPUT, anchors)
        write_jsonl(DEFAULT_ADJUDICATIONS_OUTPUT, adjudications)
    print(
        json.dumps(
            {
                "valid": True,
                "anchors": len(anchors),
                "assessments": sum(len(anchor["assessments"]) for anchor in anchors),
                "model_approved": sum(
                    row["outcome"] == "MODEL_APPROVED" for row in adjudications
                ),
                "human_overrides": sum(
                    row["outcome"] == "HUMAN_OVERRIDE" for row in adjudications
                ),
                "written": not args.check,
            }
        )
    )


if __name__ == "__main__":
    main()
