#!/usr/bin/env python3
"""Promote a Gate-2-approved basket and open its bounded scoring run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_generator import CaseState
from aletheia_lite.case_pipeline import PIPELINE_VERSION
from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_workspace import (
    CaseWorkspace,
    initialize_case_workspace,
    materialize_analysis_inputs,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024 * 1024) if sys.platform == "darwin" else float(value) / 1024


def collection_idempotency_key(case_id: str, basket_hash: str) -> str:
    digest = hashlib.sha256(
        f"{case_id}|{basket_hash}|COLLECTION|{PIPELINE_VERSION}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{case_id}-collection-{digest}"


def start(case_id: str, registry_path: Path) -> dict[str, object]:
    registry = CaseRegistry(registry_path)
    record = registry.get_case(case_id)
    manifest = record.get("manifest")
    if not manifest:
        raise ValueError("case has no approved manifest")
    basket = registry.get_basket(case_id)
    if basket.get("registry_status") != "APPROVED":
        raise ValueError("case has not passed Gate 2")

    state = CaseState(record["current_state"])
    if state == CaseState.READY_TO_RUN:
        registry.advance(
            case_id,
            CaseState.RUNNING,
            actor="approved-case-starter",
            reason="Gate 2 approval starts the bounded scoring pipeline",
        )
    elif state != CaseState.RUNNING:
        raise ValueError(f"case cannot start scoring from {state.value}")

    workspace = CaseWorkspace(ROOT, case_id)
    receipt = initialize_case_workspace(
        workspace,
        manifest,
        basket,
        staging_root=ROOT / "data" / "case_staging",
    )
    inputs = materialize_analysis_inputs(workspace)
    key = collection_idempotency_key(case_id, str(basket["basket_hash"]))
    registry.save_checkpoint(
        {
            "checkpoint_id": f"CHECKPOINT-{case_id}-COLLECTION-{key.rsplit('-', 1)[-1].upper()}",
            "case_id": case_id,
            "stage": "COLLECTION",
            "status": "COMPLETE",
            "completed_units": int(inputs["documents"]),
            "total_units": int(inputs["documents"]),
            "cost_nis": 0,
            "peak_rss_mb": peak_rss_mb(),
            "artifact_refs": [
                str(workspace.receipt_path.relative_to(ROOT)),
                str(workspace.research_db.relative_to(ROOT)),
                str(inputs["corpus_path"]),
                str(inputs["roster_path"]),
            ],
            "recorded_at": utc_now(),
            "failure_reason": None,
            "idempotency_key": key,
            "requires_adjudication": False,
            "summary": "Gate-2-approved snapshots promoted; frozen scoring corpus materialized",
        }
    )
    return {
        "case_id": case_id,
        "state": registry.get_case(case_id)["current_state"],
        "collection": inputs,
        "workspace": receipt,
        "next_stage": "ANCHORS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--registry", type=Path, default=ROOT / "data" / "case_registry.sqlite3"
    )
    args = parser.parse_args()
    print(json.dumps(start(args.case_id, args.registry), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
