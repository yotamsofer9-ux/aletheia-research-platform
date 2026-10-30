#!/usr/bin/env python3
"""Build a deterministic focused-review queue from article measurements."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "analysis" / "runs" / "case001_article_measurements_v1.jsonl"
DEFAULT_OUTPUT = ROOT / "analysis" / "runs" / "case001_article_review_queue_v1.json"
DEFAULT_PROMPT_VERSION = "case001-article-analysis-v1.0.12"
POLICY_VERSION = "case001-hybrid-acceptance-v3"
AUDIT_SAMPLE_SEED = "case001-hybrid-acceptance-v1"
TRUTH_COMPONENTS = ("T1", "T2", "T3")
PRESSURE_COMPONENTS = ("N1", "N2", "N3", "N4", "N5")


def load_latest_successes(path: Path, prompt_version: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if (
            record.get("status") == "SUCCESS"
            and record.get("prompt_version") == prompt_version
        ):
            latest[record["document_id"]] = record
    return list(latest.values())


def selected_for_audit(document_id: str) -> bool:
    # Keep the original deterministic audit sample stable. A review-threshold
    # change must not silently redraw which documents belong to the audit.
    digest = hashlib.sha256(
        f"{AUDIT_SAMPLE_SEED}:{document_id}".encode("utf-8")
    ).hexdigest()
    return int(digest, 16) % 50 == 0


def classify_component(
    code: str,
    component: dict[str, Any],
    audit_selected: bool = False,
    extra_reasons: list[str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = list(extra_reasons or [])
    score = component.get("score")
    confidence = component.get("confidence")
    if component.get("needs_review_reasons"):
        reasons.extend(
            f"EXPLICIT_{reason}" for reason in component["needs_review_reasons"]
        )
    if score is None:
        reasons.append("NOT_APPLICABLE_OR_UNSCORED")
    if confidence == "LOW":
        reasons.append(f"{confidence}_CONFIDENCE")
    if code in TRUTH_COMPONENTS and isinstance(score, int):
        if score <= 1:
            reasons.append("CRITICAL_LOW_TRUTH_SCORE")
    if code in PRESSURE_COMPONENTS and isinstance(score, int):
        if score >= 3:
            reasons.append("CRITICAL_HIGH_PRESSURE_SCORE")
    reasons = list(dict.fromkeys(reasons))
    evidence_spans = component.get("evidence_spans", [])
    return {
        "component": code,
        "decision": "HUMAN_REVIEW" if reasons else "PROVISIONAL_ACCEPT",
        "reasons": reasons,
        "score": score,
        "confidence": confidence,
        "rationale": component.get("rationale"),
        "evidence_span": evidence_spans[0] if evidence_spans else None,
        "anchor_refs": component.get("anchor_refs", []),
    }


def build_document_queue(
    record: dict[str, Any],
    baseline_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    measurement = record["measurement"]
    document_id = record["document_id"]
    audit_selected = selected_for_audit(document_id)
    asserted_adopted_causal_conflict = any(
        claim.get("centrality") == "CENTRAL"
        and claim.get("claim_kind") == "CAUSE_OR_RESPONSIBILITY"
        and claim.get("outlet_stance") == "ADOPTS"
        and claim.get("stance_toward_anchors") == "CONFLICTS"
        and claim.get("certainty") == "ASSERTED"
        for claim in measurement.get("relevant_claims", [])
    )
    baseline_components = (
        baseline_record.get("measurement", {}).get("components", {})
        if baseline_record else {}
    )
    components = []
    version_comparison = []
    for code in (*TRUTH_COMPONENTS, *PRESSURE_COMPONENTS):
        extra_reasons = []
        if asserted_adopted_causal_conflict and code in {"T1", "T3", "N2"}:
            extra_reasons.append("CROSS_FIELD_CAUSAL_CONFLICT")
        baseline_component = baseline_components.get(code)
        current_score = measurement["components"][code].get("score")
        baseline_score = (
            baseline_component.get("score") if baseline_component else None
        )
        if baseline_component is not None and baseline_score != current_score:
            hard_delta = (
                baseline_score is None or current_score is None
                or abs(current_score - baseline_score) >= 2
            )
            if hard_delta:
                extra_reasons.append("HARD_VERSION_ALIGNMENT_DELTA")
            version_comparison.append({
                "component": code,
                "baseline_score": baseline_score,
                "current_score": current_score,
                "delta": (
                    current_score - baseline_score
                    if isinstance(current_score, int)
                    and isinstance(baseline_score, int)
                    else None
                ),
            })
        components.append(
            classify_component(
                code,
                measurement["components"][code],
                audit_selected,
                extra_reasons,
            )
        )
    routed = [row["component"] for row in components if row["decision"] == "HUMAN_REVIEW"]
    return {
        "document_id": document_id,
        "source_id": record.get("source_id"),
        "language": record.get("language"),
        "sample_phase": record.get("sample_phase"),
        "prompt_version": record.get("prompt_version"),
        "analysis_status": measurement.get("analysis_status"),
        "global_review_reasons": measurement.get("needs_review", {}).get(
            "reasons", []
        ),
        "quality_audit_selected": audit_selected,
        "baseline_prompt_version": (
            baseline_record.get("prompt_version") if baseline_record else None
        ),
        "version_comparison": version_comparison,
        "document_decision": (
            "FOCUSED_HUMAN_REVIEW"
            if routed
            else (
                "QUALITY_AUDIT_SIGNOFF"
                if audit_selected else "BATCH_SIGNOFF_ONLY"
            )
        ),
        "routed_components": routed,
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--baseline-prompt-version")
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument(
        "--roster",
        type=Path,
        help="Frozen analysis roster; when supplied, historical documents are excluded.",
    )
    args = parser.parse_args()

    records = load_latest_successes(args.input, args.prompt_version)
    baseline = {
        record["document_id"]: record
        for record in load_latest_successes(
            args.input, args.baseline_prompt_version
        )
    } if args.baseline_prompt_version else {}
    if args.roster is not None:
        roster = json.loads(args.roster.read_text(encoding="utf-8"))
        roster_ids = list(roster["document_ids"])
        if len(roster_ids) != 27 or len(set(roster_ids)) != 27:
            raise ValueError("review queue roster must contain exactly 27 unique documents")
        by_id = {record["document_id"]: record for record in records}
        missing = [document_id for document_id in roster_ids if document_id not in by_id]
        if missing:
            raise ValueError(f"review queue is missing current successful documents: {missing}")
        records = [by_id[document_id] for document_id in roster_ids]
    if args.document_id:
        wanted = set(args.document_id)
        records = [record for record in records if record["document_id"] in wanted]
    documents = [
        build_document_queue(record, baseline.get(record["document_id"]))
        for record in records
    ]
    routed_components = sum(len(row["routed_components"]) for row in documents)
    payload = {
        "schema_version": "1.0",
        "policy_version": POLICY_VERSION,
        "prompt_version": args.prompt_version,
        "baseline_prompt_version": args.baseline_prompt_version,
        "documents": documents,
        "summary": {
            "documents": len(documents),
            "focused_review_documents": sum(
                row["document_decision"] == "FOCUSED_HUMAN_REVIEW"
                for row in documents
            ),
            "batch_signoff_only_documents": sum(
                row["document_decision"] == "BATCH_SIGNOFF_ONLY"
                for row in documents
            ),
            "quality_audit_documents": sum(
                row["document_decision"] == "QUALITY_AUDIT_SIGNOFF"
                for row in documents
            ),
            "routed_components": routed_components,
            "provisionally_accepted_components": len(documents) * 8
            - routed_components,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
