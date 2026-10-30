from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .judgment_room import CaseMaterial
from .case_workspace import CaseWorkspace


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _resolved_rationale(
    source: dict[str, Any], resolution: dict[str, Any]
) -> str:
    """Return the rationale that actually supports the materialized value.

    A principal-review resolution may reject a numeric appellate proposal or
    retain the original value after order sensitivity.  In those cases the
    appellate ballot is audit evidence, not the rationale for the effective
    score.  Older loaders always copied ballot 1 and could therefore pair an
    N/A value with a rationale recommending 1 (or vice versa).
    """

    final_pair = (
        resolution.get("final_score"),
        resolution.get("final_na_reason"),
    )
    source_pair = (source.get("score"), source.get("na_reason"))
    resolution_code = str(resolution.get("resolution") or "")
    if final_pair == source_pair and (
        "ORIGINAL" in resolution_code
        or "REJECTED" in resolution_code
        or "RESTORED" in resolution_code
    ):
        return str(source.get("rationale") or "")

    second_review = resolution.get("second_review") or {}
    for ballot_name in ("ballot_1", "ballot_2"):
        ballot = second_review.get(ballot_name) or {}
        if (
            ballot.get("proposed_score"),
            ballot.get("proposed_na_reason"),
        ) == final_pair:
            rationale = str(ballot.get("rationale_he") or "").strip()
            if rationale:
                return rationale
    return str(source.get("rationale") or "")


def load_workspace_corpus(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    with (workspace.analysis_dir / "corpus.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if len(rows) != 27 or len({row["document_id"] for row in rows}) != 27:
        raise ValueError(f"{workspace.case_id} requires 27 unique corpus documents")
    rows.sort(
        key=lambda row: (
            row["language"],
            row["publisher_slot"],
            row["published_at"],
            row["document_id"],
        )
    )
    return rows


def load_multicase_material(workspace: CaseWorkspace) -> CaseMaterial:
    measurement_history = _read_jsonl(
        workspace.analysis_dir / "article_measurements_v1.jsonl"
    )
    latest_successes: dict[str, dict[str, Any]] = {}
    for record in measurement_history:
        document_id = record.get("document_id")
        if (
            document_id
            and record.get("status") == "SUCCESS"
            and isinstance(record.get("measurement"), dict)
        ):
            latest_successes[document_id] = record
    measurements = [latest_successes[key] for key in sorted(latest_successes)]
    if len(measurements) != 27:
        raise ValueError(
            f"{workspace.case_id} requires 27 current successful measurements; "
            f"found {len(measurements)}"
        )
    principal = _read_json(workspace.adjudication_dir / "principal_review_v1.json")
    corpus = {row["document_id"]: row for row in load_workspace_corpus(workspace)}
    auto = {item["dilemma_id"]: item for item in principal["auto_resolutions"]}
    human = {item["dilemma_id"]: item for item in principal["human_dilemmas"]}
    documents: list[dict[str, Any]] = []

    for record in measurements:
        document_id = record["document_id"]
        components: list[dict[str, Any]] = []
        for component_id, source in record["measurement"]["components"].items():
            component = {
                "component": component_id,
                "score": source.get("score"),
                "na_reason": source.get("na_reason"),
                "confidence": source.get("confidence"),
                "rationale": source.get("rationale", ""),
                "evidence_span": (source.get("evidence_spans") or [None])[0],
                "anchor_refs": source.get("anchor_refs") or [],
                "decision": "PROVISIONAL_ACCEPT",
            }
            dilemma_id = f"{workspace.case_id}:{document_id}:{component_id}"
            if dilemma_id in auto:
                resolution = auto[dilemma_id]
                component["score"] = resolution.get("final_score")
                component["na_reason"] = resolution.get("final_na_reason")
                component["rationale"] = _resolved_rationale(source, resolution)
                component["confidence"] = "HIGH"
            elif dilemma_id in human:
                component["decision"] = "HUMAN_REVIEW"
            components.append(component)
        documents.append(
            {
                "document_id": document_id,
                "source_id": record["source_id"],
                "language": record["language"],
                "sample_phase": record["sample_phase"],
                "prompt_version": record["prompt_version"],
                "components": components,
                "article": corpus[document_id],
            }
        )

    anchors = {
        row["anchor_id"]: row
        for row in _read_jsonl(workspace.anchors_dir / "factual_anchors_v1.jsonl")
    }
    return CaseMaterial(
        case_id=workspace.case_id,
        documents=documents,
        dilemmas=list(human.values()),
        anchors=anchors,
        reference_scores=[],
    )
