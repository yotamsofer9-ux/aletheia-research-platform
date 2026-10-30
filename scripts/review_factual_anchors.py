from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]
ANCHORS_PATH = ROOT / "anchors" / "case001_factual_anchors_draft.jsonl"
MATRIX_PATH = ROOT / "anchors" / "case001_evidence_matrix.json"
RESEARCH_SOURCES_PATH = ROOT / "bibliography" / "research_sources.jsonl"
JOURNALISTIC_SOURCES_PATH = ROOT / "bibliography" / "journalistic_sources.jsonl"
DEFAULT_OUTPUT_PATH = (
    ROOT / "anchors" / "model_reviews" / "case001_vertex_reviews.jsonl"
)
DEFAULT_SUMMARY_PATH = (
    ROOT / "anchors" / "model_reviews" / "case001_vertex_review_summary.json"
)
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_PROMPT_VERSION = "case001-anchor-review-v0.1"

SYSTEM_PROMPT = """You are the epistemic reviewer for an academic media-analysis project.
Review only whether each proposed time-indexed assessment follows from the supplied local evidence bundle.
Do not use unstated external facts or treat the number of URLs as independent confirmation.
Respect the source dependency groups, limitations, temporal availability, and the narrow scope of each proposition.
An official or interested-party statement can establish that a claim was published, but cannot by itself prove the underlying event claim.
APPROVE means the proposed epistemic status and confidence should remain exactly as drafted.
REVISE means the anchor is useful but its status, confidence, wording, timing, or scope should change.
REJECT means it should not enter the factual-anchor set.
INSUFFICIENT_EVIDENCE means the supplied bundle does not permit a responsible review.
Use only source IDs present in the bundle. Return concise, auditable reasoning in English.
"""


class AssessmentReview(BaseModel):
    assessment_id: str
    verdict: Literal["APPROVE", "REVISE", "REJECT", "INSUFFICIENT_EVIDENCE"]
    recommended_epistemic_status: Literal[
        "SUPPORTED", "CONTRADICTED", "DISPUTED", "UNRESOLVED"
    ]
    recommended_confidence: Literal["HIGH", "MEDIUM", "LOW"]
    rationale: str = Field(min_length=20, max_length=1200)
    evidence_source_ids: list[str] = Field(min_length=1)
    dependency_warning: bool
    critical_issue: str | None = Field(default=None, max_length=500)


class AnchorReviewResult(BaseModel):
    anchor_id: str
    overall_verdict: Literal[
        "APPROVE", "REVISE", "REJECT", "INSUFFICIENT_EVIDENCE"
    ]
    assessment_reviews: list[AssessmentReview] = Field(min_length=1)
    concise_anchor_note: str = Field(min_length=20, max_length=1000)


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_inputs(
    anchors_path: Path = ANCHORS_PATH,
    matrix_path: Path = MATRIX_PATH,
    research_sources_path: Path = RESEARCH_SOURCES_PATH,
    journalistic_sources_path: Path = JOURNALISTIC_SOURCES_PATH,
) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    anchors = load_jsonl(anchors_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix_sources = {item["source_id"]: item for item in matrix["sources"]}
    registries = load_jsonl(research_sources_path) + load_jsonl(
        journalistic_sources_path
    )
    registry_sources = {item["source_id"]: item for item in registries}
    return anchors, matrix_sources, registry_sources


def build_evidence_bundle(
    anchor: dict,
    matrix_sources: dict[str, dict],
    registry_sources: dict[str, dict],
) -> dict:
    referenced_ids = sorted(
        {
            evidence["source_id"]
            for assessment in anchor["assessments"]
            for evidence in assessment["evidence_refs"]
        }
    )
    missing_matrix = set(referenced_ids) - matrix_sources.keys()
    missing_registry = set(referenced_ids) - registry_sources.keys()
    if missing_matrix or missing_registry:
        raise ValueError(
            f"{anchor['anchor_id']} has unresolved sources: "
            f"matrix={sorted(missing_matrix)}, registry={sorted(missing_registry)}"
        )

    evidence_sources = []
    for source_id in referenced_ids:
        matrix_source = matrix_sources[source_id]
        registry_source = registry_sources[source_id]
        evidence_sources.append(
            {
                "source_id": source_id,
                "title": registry_source["title"],
                "organization": registry_source["authors_or_organization"],
                "source_type": registry_source["source_type"],
                "quality_tier": registry_source["quality_tier"],
                "published_at": matrix_source["published_at"],
                "source_role": matrix_source["source_role"],
                "independence_group": matrix_source["independence_group"],
                "contributes": matrix_source["contributes"],
                "limitations": matrix_source["limitations"],
                "quality_notes": registry_source["quality_notes"],
            }
        )

    return {
        "case_id": anchor["case_id"],
        "anchor": anchor,
        "evidence_sources": evidence_sources,
        "review_instruction": (
            "Return one assessment_review for every assessment_id, in the same "
            "order. Evaluate the proposed epistemic_status and confidence as of "
            "the stated date. Do not rewrite a narrow proposition into a broader one."
        ),
    }


def validate_model_result(
    result: AnchorReviewResult, anchor: dict, allowed_source_ids: set[str]
) -> None:
    if result.anchor_id != anchor["anchor_id"]:
        raise ValueError(
            f"model returned anchor_id={result.anchor_id}, expected={anchor['anchor_id']}"
        )
    expected = [item["assessment_id"] for item in anchor["assessments"]]
    actual = [item.assessment_id for item in result.assessment_reviews]
    if actual != expected:
        raise ValueError(f"assessment IDs differ: expected={expected}, actual={actual}")

    proposed_by_id = {
        item["assessment_id"]: item for item in anchor["assessments"]
    }
    for review in result.assessment_reviews:
        unknown_sources = set(review.evidence_source_ids) - allowed_source_ids
        if unknown_sources:
            raise ValueError(
                f"{review.assessment_id} cites unavailable sources: "
                f"{sorted(unknown_sources)}"
            )
        proposed = proposed_by_id[review.assessment_id]
        if review.verdict == "APPROVE" and (
            review.recommended_epistemic_status != proposed["epistemic_status"]
            or review.recommended_confidence != proposed["confidence"]
        ):
            raise ValueError(
                f"{review.assessment_id} cannot APPROVE while changing status/confidence"
            )


def input_hash(bundle: dict, model: str, prompt_version: str) -> str:
    payload = json.dumps(
        {
            "prompt_version": prompt_version,
            "model": model,
            "system_prompt": SYSTEM_PROMPT,
            "bundle": bundle,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cached_reviews(path: Path) -> dict[tuple[str, str, str], dict]:
    if not path.exists():
        return {}
    records = load_jsonl(path)
    return {
        (record["anchor_id"], record["input_hash"], record["model"]): record
        for record in records
    }


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def usage_dict(response: object) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }


def write_summary(
    path: Path,
    output_path: Path,
    model: str,
    prompt_version: str,
    evidence_basis: str,
    case_id: str = "CASE-001",
) -> dict:
    records = load_jsonl(output_path) if output_path.exists() else []
    current = [
        record
        for record in records
        if record["model"] == model and record["prompt_version"] == prompt_version
    ]
    latest_by_anchor = {record["anchor_id"]: record for record in current}
    verdict_counts: dict[str, int] = {}
    assessment_verdict_counts: dict[str, int] = {}
    total_tokens = 0
    for record in latest_by_anchor.values():
        verdict = record["model_output"]["overall_verdict"]
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        for review in record["model_output"]["assessment_reviews"]:
            item_verdict = review["verdict"]
            assessment_verdict_counts[item_verdict] = (
                assessment_verdict_counts.get(item_verdict, 0) + 1
            )
        total_tokens += record["usage"].get("total_tokens") or 0

    summary = {
        "schema_version": "0.1",
        "case_id": case_id,
        "review_kind": "MODEL_REVIEW",
        "model": model,
        "prompt_version": prompt_version,
        "evidence_basis": evidence_basis,
        "anchors_reviewed": len(latest_by_anchor),
        "anchor_verdict_counts": verdict_counts,
        "assessment_verdict_counts": assessment_verdict_counts,
        "total_tokens": total_tokens,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "human_approval_claimed": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review one case's factual anchors sequentially with Vertex AI."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=os.getenv("GCP_REGION", DEFAULT_LOCATION))
    parser.add_argument("--model", default=os.getenv("VERTEX_AI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--case-id", default="CASE-001")
    parser.add_argument("--anchors", type=Path, default=ANCHORS_PATH)
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument(
        "--research-sources", type=Path, default=RESEARCH_SOURCES_PATH
    )
    parser.add_argument(
        "--journalistic-sources", type=Path, default=JOURNALISTIC_SOURCES_PATH
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--max-anchors", type=int, default=None)
    parser.add_argument("--anchor-id", action="append", default=[])
    parser.add_argument("--supplemental-evidence", type=Path, default=None)
    args = parser.parse_args()

    anchors, matrix_sources, registry_sources = load_inputs(
        args.anchors,
        args.matrix,
        args.research_sources,
        args.journalistic_sources,
    )
    case_ids = {anchor.get("case_id") for anchor in anchors}
    if case_ids != {args.case_id}:
        raise ValueError(
            f"anchor case IDs differ from --case-id: {sorted(str(x) for x in case_ids)}"
        )
    if args.anchor_id:
        requested_ids = set(args.anchor_id)
        known_ids = {anchor["anchor_id"] for anchor in anchors}
        unknown_ids = requested_ids - known_ids
        if unknown_ids:
            raise ValueError(f"unknown --anchor-id values: {sorted(unknown_ids)}")
        anchors = [
            anchor for anchor in anchors if anchor["anchor_id"] in requested_ids
        ]
    if args.max_anchors is not None:
        if args.max_anchors < 1:
            raise ValueError("--max-anchors must be positive")
        anchors = anchors[: args.max_anchors]

    bundles = [
        build_evidence_bundle(anchor, matrix_sources, registry_sources)
        for anchor in anchors
    ]
    if args.supplemental_evidence is not None:
        supplement = json.loads(
            args.supplemental_evidence.read_text(encoding="utf-8")
        )
        supplement_ids = {
            item["source_id"] for item in supplement["evidence_items"]
        }
        missing_supplement_sources = supplement_ids - registry_sources.keys()
        if missing_supplement_sources:
            raise ValueError(
                "supplemental evidence has unregistered sources: "
                f"{sorted(missing_supplement_sources)}"
            )
        for bundle in bundles:
            bundle["supplemental_evidence"] = supplement
    evidence_basis = (
        "LOCAL_ANCHOR_RECORD_EVIDENCE_MATRIX_AND_SUPPLEMENTAL_EVIDENCE"
        if args.supplemental_evidence is not None
        else "LOCAL_ANCHOR_RECORD_AND_EVIDENCE_MATRIX"
    )
    if not args.live:
        print(
            json.dumps(
                {
                    "valid": True,
                    "live": False,
                    "anchors": len(bundles),
                    "assessments": sum(
                        len(bundle["anchor"]["assessments"]) for bundle in bundles
                    ),
                    "model": args.model,
                    "prompt_version": args.prompt_version,
                    "evidence_basis": evidence_basis,
                }
            )
        )
        return 0

    client = genai.Client(
        vertexai=True,
        project=args.project,
        location=args.location,
    )
    cached = load_cached_reviews(args.output)
    completed = 0
    skipped = 0
    for bundle in bundles:
        anchor = bundle["anchor"]
        digest = input_hash(bundle, args.model, args.prompt_version)
        cache_key = (anchor["anchor_id"], digest, args.model)
        if cache_key in cached:
            skipped += 1
            continue

        prompt = (
            "Review this factual-anchor evidence bundle.\n\n"
            + json.dumps(bundle, ensure_ascii=False, indent=2)
        )
        response = client.models.generate_content(
            model=args.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=AnchorReviewResult,
                temperature=0.0,
                max_output_tokens=4096,
            ),
        )
        result = AnchorReviewResult.model_validate_json(response.text)
        allowed_source_ids = {
            source["source_id"] for source in bundle["evidence_sources"]
        }
        if "supplemental_evidence" in bundle:
            allowed_source_ids.update(
                item["source_id"]
                for item in bundle["supplemental_evidence"]["evidence_items"]
            )
        validate_model_result(result, anchor, allowed_source_ids)
        record = {
            "schema_version": "0.1",
            "case_id": anchor["case_id"],
            "anchor_id": anchor["anchor_id"],
            "review_kind": "MODEL_REVIEW",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "project": args.project,
            "location": args.location,
            "prompt_version": args.prompt_version,
            "input_hash": digest,
            "evidence_basis": evidence_basis,
            "human_approval_claimed": False,
            "usage": usage_dict(response),
            "model_output": result.model_dump(mode="json"),
        }
        append_jsonl(args.output, record)
        completed += 1
        print(
            json.dumps(
                {
                    "anchor_id": anchor["anchor_id"],
                    "verdict": result.overall_verdict,
                    "usage": record["usage"],
                }
            ),
            flush=True,
        )

    summary = write_summary(
        args.summary,
        args.output,
        args.model,
        args.prompt_version,
        evidence_basis,
        args.case_id,
    )
    print(
        json.dumps(
            {
                "completed": completed,
                "cached": skipped,
                "summary": summary,
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
