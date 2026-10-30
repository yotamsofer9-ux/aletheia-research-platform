from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .case_pipeline import PipelineStage, StageContext, StageResult
from .case_workspace import CaseWorkspace


PRICE_SOURCE = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
FX_SOURCE = "https://www.boi.org.il/roles/markets/exchangerates"
INPUT_USD_PER_MILLION = 0.15
THINKING_OUTPUT_USD_PER_MILLION = 3.50
USD_ILS = 3.0780
PRICE_EFFECTIVE_DATE = "2026-10-13"
FX_EFFECTIVE_DATE = "2026-10-12"
RECEIPT_VERSION = "multicase-run-receipt-v1"


class CaseRunReceiptError(ValueError):
    """Raised when produced artifacts cannot support a real stage checkpoint."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _usage_total(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for record in records:
        usage = record.get("usage") or {}
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    return totals


def _estimate_cost(usage: Mapping[str, int]) -> dict[str, Any]:
    prompt = int(usage.get("prompt_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    # Vertex's total can include thinking tokens not exposed in output_tokens.
    # Treat every non-input token at the higher thinking-output rate.  This is
    # deliberately a cap estimate, not a claim about the final Cloud invoice.
    billed_output_equivalent = max(output, total - prompt, 0)
    usd = (
        prompt * INPUT_USD_PER_MILLION
        + billed_output_equivalent * THINKING_OUTPUT_USD_PER_MILLION
    ) / 1_000_000
    return {
        "prompt_tokens": prompt,
        "reported_output_tokens": output,
        "reported_total_tokens": total,
        "billed_output_equivalent_tokens": billed_output_equivalent,
        "upper_bound_usd": round(usd, 6),
        "upper_bound_nis": round(usd * USD_ILS, 6),
    }


def _relative(workspace: CaseWorkspace, path: Path) -> str:
    return str(path.relative_to(workspace.project_root))


@dataclass(frozen=True)
class ExistingRunValidation:
    receipt: dict[str, Any]
    stage_results: dict[PipelineStage, StageResult]


def validate_existing_run(workspace: CaseWorkspace) -> ExistingRunValidation:
    receipt = _read_json(workspace.receipt_path)
    basket = _read_json(workspace.basket_path)
    manifest = _read_json(workspace.manifest_path)
    if receipt.get("case_id") != workspace.case_id:
        raise CaseRunReceiptError("workspace receipt has the wrong case_id")
    if receipt.get("basket_hash") != basket.get("basket_hash"):
        raise CaseRunReceiptError("workspace receipt is not bound to the approved basket")
    if int(receipt.get("article_count") or 0) != 27:
        raise CaseRunReceiptError("collection receipt does not contain 27 articles")

    database = sqlite3.connect(workspace.research_db)
    try:
        ready = int(
            database.execute(
                "SELECT COUNT(*) FROM documents WHERE case_id = ? AND status = 'READY'",
                (workspace.case_id,),
            ).fetchone()[0]
        )
        if ready != 27:
            raise CaseRunReceiptError(f"corpus has {ready} READY documents, expected 27")
        documents = database.execute(
            "SELECT snapshot_ref, snapshot_hash FROM documents WHERE case_id = ?",
            (workspace.case_id,),
        ).fetchall()
    finally:
        database.close()
    for snapshot_ref, expected_hash in documents:
        prefix = "local-snapshot://"
        if not str(snapshot_ref).startswith(prefix):
            raise CaseRunReceiptError("corpus contains a non-local snapshot reference")
        relative = Path(str(snapshot_ref)[len(prefix) :])
        if relative.is_absolute() or ".." in relative.parts:
            raise CaseRunReceiptError("corpus contains an unsafe snapshot reference")
        raw = gzip.decompress((workspace.snapshot_root / relative).read_bytes())
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise CaseRunReceiptError("corpus snapshot hash verification failed")

    anchors_path = workspace.anchors_dir / "factual_anchors_v1.jsonl"
    anchors = _read_jsonl(anchors_path)
    anchor_summary = _read_json(workspace.anchors_dir / "model_review_summary.json")
    anchor_count = int(anchor_summary.get("anchors_reviewed") or 0)
    if (
        anchor_count < 1
        or len(anchors) != anchor_count
        or len({row.get("anchor_id") for row in anchors}) != anchor_count
        or any(row.get("case_id") != workspace.case_id for row in anchors)
    ):
        raise CaseRunReceiptError("reviewed anchor set is incomplete or belongs to another case")
    anchor_reviews_path = workspace.anchors_dir / "model_reviews.jsonl"
    anchor_reviews = _read_jsonl(anchor_reviews_path)
    if not {row["anchor_id"] for row in anchors}.issubset(
        {row.get("anchor_id") for row in anchor_reviews}
    ):
        raise CaseRunReceiptError("anchor review set is incomplete")

    measurements_path = workspace.analysis_dir / "article_measurements_v1.jsonl"
    measurements = _read_jsonl(measurements_path)
    latest = {row["document_id"]: row for row in measurements}
    if len(latest) != 27 or any(row.get("status") != "SUCCESS" for row in latest.values()):
        raise CaseRunReceiptError("article analysis does not contain 27 successful documents")
    for row in latest.values():
        components = (row.get("measurement") or {}).get("components") or {}
        if set(components) != {"T1", "T2", "T3", "N1", "N2", "N3", "N4", "N5"}:
            raise CaseRunReceiptError("article analysis is missing a required component")

    debate_path = workspace.analysis_dir / "debate_candidate_v1.json"
    debate = _read_json(debate_path)
    ballots_path = workspace.adjudication_dir / "second_review_ballots_v1.jsonl"
    ballots = _read_jsonl(ballots_path)
    successful_ballots = {
        (row["document_id"], int(row["ballot_number"])): row
        for row in ballots
        if row.get("status") == "SUCCESS"
    }
    reconciled = _read_json(
        workspace.adjudication_dir / "second_review_reconciled_v1.json"
    )
    principal_path = workspace.adjudication_dir / "principal_review_v1.json"
    principal = _read_json(principal_path)
    summary = principal.get("summary") or {}
    reviewed_dilemmas = int(summary.get("reviewed_dilemmas") or 0)
    auto_resolved = int(summary.get("auto_resolved") or 0)
    human_required = int(summary.get("human_required") or 0)
    reconciled_summary = reconciled.get("summary") or {}
    reviewed_documents = int(
        reconciled_summary.get("documents_with_two_ballots") or 0
    )
    if (
        reviewed_dilemmas < 1
        or auto_resolved + human_required != reviewed_dilemmas
        or int(reconciled_summary.get("decisions") or 0) != reviewed_dilemmas
        or len(successful_ballots) != reviewed_documents * 2
    ):
        raise CaseRunReceiptError("second-review result does not match the approved escalation policy")

    anchor_cost = _estimate_cost(_usage_total(anchor_reviews))
    analysis_usage_records = list(measurements)
    for filename in (
        "frame_intensity_full_first_pass_v1.jsonl",
        "frame_intensity_full_second_pass_v1.jsonl",
    ):
        path = workspace.analysis_dir / filename
        if path.exists():
            analysis_usage_records.extend(_read_jsonl(path))
    analysis_cost = _estimate_cost(_usage_total(analysis_usage_records))
    second_usage = _usage_total([{"usage": debate.get("usage") or {}}, *ballots])
    second_cost = _estimate_cost(second_usage)
    total_upper_bound = round(
        anchor_cost["upper_bound_nis"]
        + analysis_cost["upper_bound_nis"]
        + second_cost["upper_bound_nis"],
        6,
    )

    results_dir = workspace.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = results_dir / "run_receipt_v1.json"
    run_receipt = {
        "schema_version": "1.0",
        "receipt_version": RECEIPT_VERSION,
        "case_id": workspace.case_id,
        "basket_hash": basket["basket_hash"],
        "status": (
            "AWAITING_ADJUDICATION" if human_required > 0 else "ANALYSIS_COMPLETE"
        ),
        "collection": {"documents": 27, "snapshots_hash_verified": 27},
        "anchors": {"reviewed_anchors": anchor_count, "cost_estimate": anchor_cost},
        "analysis": {
            "successful_documents": 27,
            "scored_components": 216,
            "cost_estimate": analysis_cost,
        },
        "second_review": {
            "reviewed_dilemmas": reviewed_dilemmas,
            "ballots": len(successful_ballots),
            "auto_resolved": auto_resolved,
            "human_required": human_required,
            "cost_estimate": second_cost,
        },
        "cost_guard": {
            "kind": "CONSERVATIVE_UPPER_BOUND_ESTIMATE_NOT_INVOICE",
            "upper_bound_nis": total_upper_bound,
            "approved_cap_nis": float(manifest["max_project_cost_nis"]),
            "within_cap": total_upper_bound <= float(manifest["max_project_cost_nis"]),
            "input_usd_per_million": INPUT_USD_PER_MILLION,
            "thinking_output_usd_per_million": THINKING_OUTPUT_USD_PER_MILLION,
            "usd_ils": USD_ILS,
            "pricing_effective_date": PRICE_EFFECTIVE_DATE,
            "fx_effective_date": FX_EFFECTIVE_DATE,
            "pricing_source": PRICE_SOURCE,
            "fx_source": FX_SOURCE,
        },
        "recorded_at": _utc_now(),
    }
    receipt_path.write_text(
        json.dumps(run_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    stage_results = {
        PipelineStage.COLLECTION: StageResult(
            completed_units=27,
            total_units=27,
            cost_nis=0,
            artifact_refs=(
                _relative(workspace, workspace.receipt_path),
                _relative(workspace, workspace.research_db),
                _relative(workspace, workspace.analysis_dir / "corpus.csv"),
            ),
            summary="27 approved snapshots promoted and hash-verified",
        ),
        PipelineStage.ANCHORS: StageResult(
            completed_units=anchor_count,
            total_units=anchor_count,
            cost_nis=anchor_cost["upper_bound_nis"],
            artifact_refs=(
                _relative(workspace, anchors_path),
                _relative(workspace, workspace.anchors_dir / "model_review_summary.json"),
            ),
            summary=f"{anchor_count} factual anchors reviewed and materialized",
        ),
        PipelineStage.ANALYSIS: StageResult(
            completed_units=27,
            total_units=27,
            cost_nis=analysis_cost["upper_bound_nis"],
            artifact_refs=(
                _relative(workspace, measurements_path),
                _relative(workspace, workspace.analysis_dir / "article_review_queue_v1.json"),
            ),
            summary="27 articles scored across eight components",
        ),
        PipelineStage.SECOND_REVIEW: StageResult(
            completed_units=reviewed_dilemmas,
            total_units=reviewed_dilemmas,
            cost_nis=second_cost["upper_bound_nis"],
            artifact_refs=(
                _relative(workspace, principal_path),
                _relative(workspace, ballots_path),
                _relative(workspace, receipt_path),
            ),
            requires_adjudication=human_required > 0,
            summary=(
                f"{216 - human_required} of 216 components resolved automatically; "
                f"{human_required} hard conflicts require the principal researcher"
            ),
        ),
    }
    return ExistingRunValidation(receipt=run_receipt, stage_results=stage_results)


def existing_run_handlers(
    validation: ExistingRunValidation,
) -> dict[PipelineStage, Any]:
    def stage_handler(stage: PipelineStage):
        def handler(_: StageContext) -> StageResult:
            return validation.stage_results[stage]

        return handler

    def unavailable(stage: PipelineStage):
        def handler(_: StageContext) -> StageResult:
            raise CaseRunReceiptError(
                f"{stage.value} cannot run before the principal judgments are saved"
            )

        return handler

    return {
        PipelineStage.COLLECTION: stage_handler(PipelineStage.COLLECTION),
        PipelineStage.ANCHORS: stage_handler(PipelineStage.ANCHORS),
        PipelineStage.ANALYSIS: stage_handler(PipelineStage.ANALYSIS),
        PipelineStage.SECOND_REVIEW: stage_handler(PipelineStage.SECOND_REVIEW),
        PipelineStage.FINALIZATION: unavailable(PipelineStage.FINALIZATION),
        PipelineStage.CLOUD_SYNC: unavailable(PipelineStage.CLOUD_SYNC),
        PipelineStage.DASHBOARD: unavailable(PipelineStage.DASHBOARD),
    }
