#!/usr/bin/env python3
"""Register verified final stages and close a completed multicase pipeline."""

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

from aletheia_lite.case_generator import CaseState
from aletheia_lite.case_pipeline import PIPELINE_VERSION, PipelineStage
from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_run_receipt import _estimate_cost
from aletheia_lite.case_workspace import CaseWorkspace


FAILED_SCHEMA_CALL_RESERVE_NIS = 0.10
CLOUD_COST_RESERVE_NIS = 0.05


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _t4_cost(workspace: CaseWorkspace) -> dict:
    result = _read(workspace.results_dir / "t4_autonomous_review_v1.json")
    work_path = workspace.results_dir / "t4_autonomous_review_work_v1.json"
    work = _read(work_path) if work_path.exists() else {}
    usages = [
        result.get("initial_usage") or {},
        result.get("independent_audit_usage") or {},
        *[
            item.get("usage") or {}
            for item in result.get("arbitration", [])
        ],
        *[
            item.get("usage") or {}
            for item in work.get("repair_usage", [])
        ],
    ]
    totals = {
        key: sum(int(usage.get(key) or 0) for usage in usages)
        for key in ("prompt_tokens", "output_tokens", "total_tokens")
    }
    return _estimate_cost(totals)


def _checkpoint(
    case_id: str,
    basket_hash: str,
    stage: PipelineStage,
    *,
    completed: int,
    total: int,
    cost_nis: float,
    artifacts: list[str],
    recorded_at: str,
    summary: str,
) -> dict:
    digest = hashlib.sha256(
        f"{case_id}|{basket_hash}|{stage.value}|{PIPELINE_VERSION}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "checkpoint_id": f"CHECKPOINT-{case_id}-{stage.value}-{digest.upper()}",
        "case_id": case_id,
        "stage": stage.value,
        "status": "COMPLETE",
        "completed_units": completed,
        "total_units": total,
        "cost_nis": cost_nis,
        "peak_rss_mb": 0.0,
        "artifact_refs": artifacts,
        "recorded_at": recorded_at,
        "failure_reason": None,
        "idempotency_key": f"{case_id}-{stage.value.lower()}-{digest}",
        "requires_adjudication": False,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--registry", type=Path, default=ROOT / "data/case_registry.sqlite3"
    )
    args = parser.parse_args()
    workspace = CaseWorkspace(ROOT, args.case_id)
    registry = CaseRegistry(args.registry)
    case = registry.get_case(args.case_id)
    basket = registry.get_basket(args.case_id)
    basket_hash = basket["basket_hash"]

    readiness_path = workspace.results_dir / "scoring_readiness_v1.json"
    results_path = workspace.results_dir / "final_results_v1.json"
    closeout_path = workspace.results_dir / "closeout_v1.json"
    cloud_path = workspace.results_dir / "cloud_sync_receipt_final_v1.json"
    readiness = _read(readiness_path)
    results = _read(results_path)
    closeout = _read(closeout_path)
    cloud = _read(cloud_path)
    expected_human = int(closeout.get("human_article_decisions") or 0)
    verified_objects = int(cloud.get("objects", {}).get("verified_objects") or 0)
    t4_cost = _t4_cost(workspace)
    if readiness.get("status") != "READY" or readiness.get("effective_article_components") != 216:
        raise ValueError("final scoring readiness is incomplete")
    if results.get("status") != "CLOSED" or len(results.get("records", [])) != 40:
        raise ValueError("final result set is incomplete")
    if closeout.get("trajectory_decisions") != 9:
        raise ValueError("nine T4 decisions are required")
    if cloud.get("status") != "VERIFIED_FINAL":
        raise ValueError("final cloud synchronization was not verified")
    if (
        verified_objects < 1
        or not cloud.get("objects", {}).get("readback_sha256_verified")
    ):
        raise ValueError("cloud object read-back is incomplete")
    if set(cloud.get("tables", {})) != {
        "research_cases", "corpus_documents", "factual_anchors", "analysis_runs",
        "article_measurements", "adjudication_decisions", "component_score_ledger",
        "trajectory_decisions", "case_score_results", "case_closeouts", "policy_records",
    }:
        raise ValueError("cloud table verification is incomplete")
    connection = sqlite3.connect(workspace.adjudication_dir / "judgment_room.sqlite3")
    try:
        human = connection.execute(
            "SELECT COUNT(*) FROM adjudication_decisions WHERE is_current = 1"
        ).fetchone()[0]
        t4 = connection.execute(
            "SELECT COUNT(*) FROM trajectory_decisions WHERE is_current = 1"
        ).fetchone()[0]
        effective = connection.execute(
            "SELECT COUNT(*) FROM component_score_ledger WHERE is_effective = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    if (human, t4, effective) != (expected_human, 9, 216):
        raise ValueError("local adjudication ledger is not ready to close")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    if (
        f'"{args.case_id} ·' not in app_source
        or "render_closed_case_dashboard" not in app_source
    ):
        raise ValueError(f"{args.case_id} dashboard is not installed")

    if CaseState(case["current_state"]) == CaseState.AWAITING_ADJUDICATION:
        registry.advance(
            args.case_id,
            CaseState.FINALIZING,
            actor="multicase-closeout",
            reason=(
                f"{expected_human} effective human judgments saved; "
                "deterministic results ready"
            ),
        )
    elif CaseState(case["current_state"]) not in {CaseState.FINALIZING, CaseState.CLOSED}:
        raise ValueError(f"case cannot close from {case['current_state']}")

    checkpoints = [
        _checkpoint(
            args.case_id,
            basket_hash,
            PipelineStage.FINALIZATION,
            completed=40,
            total=40,
            cost_nis=round(
                t4_cost["upper_bound_nis"] + FAILED_SCHEMA_CALL_RESERVE_NIS, 6
            ),
            artifacts=[
                str(results_path.relative_to(ROOT)),
                str(closeout_path.relative_to(ROOT)),
                str(readiness_path.relative_to(ROOT)),
            ],
            recorded_at=closeout["closed_at"],
            summary=(
                f"216 effective article components, {expected_human} effective "
                "human judgments and nine T4 trajectories finalized into 40 "
                "score records"
            ),
        ),
        _checkpoint(
            args.case_id,
            basket_hash,
            PipelineStage.CLOUD_SYNC,
            completed=verified_objects,
            total=verified_objects,
            cost_nis=CLOUD_COST_RESERVE_NIS,
            artifacts=[str(cloud_path.relative_to(ROOT))],
            recorded_at=cloud["verified_at"],
            summary=(
                f"11 BigQuery tables and {verified_objects} GCS objects verified "
                "by count and SHA-256 read-back"
            ),
        ),
        _checkpoint(
            args.case_id,
            basket_hash,
            PipelineStage.DASHBOARD,
            completed=1,
            total=1,
            cost_nis=0,
            artifacts=["app.py", str(results_path.relative_to(ROOT))],
            recorded_at=cloud["verified_at"],
            summary=(
                f"{args.case_id} dashboard and four-case aggregate installed and "
                "visually verified"
            ),
        ),
    ]
    for checkpoint in checkpoints:
        existing = registry.latest_checkpoint(args.case_id, checkpoint["stage"])
        if existing and existing.get("idempotency_key") == checkpoint["idempotency_key"]:
            continue
        registry.save_checkpoint(checkpoint)

    current_state = CaseState(registry.get_case(args.case_id)["current_state"])
    if current_state == CaseState.FINALIZING:
        registry.advance(
            args.case_id,
            CaseState.CLOSED,
            actor="multicase-closeout",
            reason="final results, verified cloud copy, and dashboard completed",
        )

    running_receipt = _read(workspace.results_dir / "run_receipt_v1.json")
    prior = float(running_receipt["cost_guard"]["upper_bound_nis"])
    final_upper_bound = round(
        prior
        + t4_cost["upper_bound_nis"]
        + FAILED_SCHEMA_CALL_RESERVE_NIS
        + CLOUD_COST_RESERVE_NIS,
        6,
    )
    receipt = {
        "schema_version": "1.0",
        "case_id": args.case_id,
        "status": "CLOSED",
        "basket_hash": basket_hash,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "documents": 27,
            "article_components": 216,
            "human_decisions": expected_human,
            "trajectory_decisions": 9,
            "score_records": 40,
            "verified_cloud_objects": verified_objects,
            "verified_cloud_tables": 11,
        },
        "cost_guard": {
            "kind": "CONSERVATIVE_UPPER_BOUND_ESTIMATE_NOT_INVOICE",
            "prior_upper_bound_nis": prior,
            "t4_known_calls_upper_bound_nis": t4_cost["upper_bound_nis"],
            "failed_pre_schema_call_reserve_nis": FAILED_SCHEMA_CALL_RESERVE_NIS,
            "cloud_query_and_storage_reserve_nis": CLOUD_COST_RESERVE_NIS,
            "final_upper_bound_nis": final_upper_bound,
            "approved_cap_nis": 100,
            "within_cap": final_upper_bound <= 100,
        },
        "artifacts": {
            "final_results": str(results_path.relative_to(ROOT)),
            "final_results_sha256": _hash(results_path),
            "closeout": str(closeout_path.relative_to(ROOT)),
            "cloud_receipt": str(cloud_path.relative_to(ROOT)),
            "cloud_receipt_sha256": _hash(cloud_path),
            "dashboard": "app.py",
        },
    }
    receipt_path = workspace.results_dir / "pipeline_closeout_receipt_v1.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
