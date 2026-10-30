#!/usr/bin/env python3
"""Stage an approved topicality repair and reopen the exact basket at Gate 2."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.basket_builder import (  # noqa: E402
    SelectedArticle,
    build_basket_payload,
    load_publisher_roster,
    load_reviewed_leads,
    publisher_roster_path,
    stage_selected_articles,
)
from aletheia_lite.basket_progress import (  # noqa: E402
    restore_staged_from_previous_basket,
)
from aletheia_lite.case_registry import CaseRegistry, CaseRegistryError  # noqa: E402
from aletheia_lite.case_generator import CaseState  # noqa: E402
from aletheia_lite.collector import BoundedHTTPClient, canonicalize_url  # noqa: E402
from aletheia_lite.config import NetworkLimits  # noqa: E402


REGISTRY_PATH = ROOT / "data/case_registry.sqlite3"
ROSTER_PATH = ROOT / "config/multicase_publisher_roster_v1.json"
STAGING_ROOT = ROOT / "data/case_staging"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return float(value) / divisor


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def prepare_repair(
    *,
    case_id: str,
    plan_path: Path,
    reviewed_leads_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    registry = CaseRegistry(REGISTRY_PATH)
    case = registry.get_case(case_id)
    manifest = case.get("manifest")
    if not manifest:
        raise CaseRegistryError("topic repair requires a registered case manifest")
    prior_basket = registry.get_basket(case_id)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("case_id") != case_id:
        raise ValueError("repair plan targets a different case")
    if plan.get("base_basket_hash") != prior_basket.get("basket_hash"):
        raise ValueError("repair plan targets a stale basket hash")

    roster = load_publisher_roster(publisher_roster_path(ROOT, case_id))
    reviewed_leads = load_reviewed_leads(reviewed_leads_path, manifest, roster)
    leads_by_url = {
        canonicalize_url(lead.url): lead for lead in reviewed_leads
    }
    source_lookup = {
        outlet.source_id: (slot, outlet)
        for slot in roster.slots
        for outlet in slot.outlets
    }
    articles_by_id = {
        str(article["article_id"]): article
        for article in prior_basket.get("articles") or []
    }

    selected: list[SelectedArticle] = []
    repaired_cells: set[tuple[str, str]] = set()
    replacement_audit: list[dict[str, Any]] = []
    for replacement in plan.get("replacements") or []:
        old_article_id = str(replacement["old_article_id"])
        old_article = articles_by_id.get(old_article_id)
        if old_article is None:
            raise ValueError(f"unknown old article in repair plan: {old_article_id}")
        replacement_url = canonicalize_url(str(replacement["replacement_url"]))
        lead = leads_by_url.get(replacement_url)
        if lead is None:
            raise ValueError(
                f"replacement URL is absent from reviewed leads: {replacement_url}"
            )
        slot, outlet = source_lookup[lead.source_id]
        cell = (str(old_article["publisher_slot"]), str(old_article["sample_phase"]))
        if slot.slot_id != cell[0]:
            raise ValueError(
                f"replacement changes publisher slot for {old_article_id}"
            )
        if lead.sample_phase != cell[1]:
            raise ValueError(
                f"replacement changes sample phase for {old_article_id}"
            )
        if cell in repaired_cells:
            raise ValueError(f"repair plan replaces the same basket cell twice: {cell}")
        repaired_cells.add(cell)
        selected.append(SelectedArticle(slot=slot, outlet=outlet, lead=lead))
        replacement_audit.append(
            {
                "old_article_id": old_article_id,
                "publisher_slot": cell[0],
                "sample_phase": cell[1],
                "old_source_id": old_article["source_id"],
                "old_title": old_article["title"],
                "new_source_id": lead.source_id,
                "new_title": lead.title,
                "new_url": lead.url,
                "relevance_basis": replacement.get("relevance_basis"),
            }
        )

    generated_at = utc_now()
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": case_id,
        "repair_type": plan.get("repair_type"),
        "generated_at": generated_at,
        "base_basket_hash": prior_basket["basket_hash"],
        "base_basket_version": prior_basket["version"],
        "scope_rule": plan.get("scope_rule"),
        "replacement_count": len(selected),
        "replacements": replacement_audit,
        "cloud_writes": 0,
        "model_calls": 0,
    }
    if not selected or len(selected) >= 27:
        raise ValueError("topic repair must replace between one and 26 cells")

    client = BoundedHTTPClient(
        NetworkLimits(
            timeout_seconds=15,
            max_response_bytes=5_000_000,
            requests_per_second_per_host=0.5,
            retries=1,
            min_text_chars=250,
            max_text_chars=30_000,
        )
    )
    staging = stage_selected_articles(
        manifest,
        selected,
        STAGING_ROOT,
        client=client,
    )
    report["staged_replacements"] = len(staging.staged)
    report["staging_failures"] = [asdict(item) for item in staging.failures]
    report["peak_rss_mb"] = peak_rss_mb()
    if len(staging.staged) != len(selected):
        report["status"] = "STAGING_FAILED"
        write_json(report_path, report)
        raise RuntimeError(
            f"only {len(staging.staged)} of {len(selected)} replacements passed staging"
        )

    prior_staged = restore_staged_from_previous_basket(
        prior_basket,
        roster,
        STAGING_ROOT,
        [],
    )
    retained = [
        item
        for item in prior_staged
        if (item.selected.slot.slot_id, item.selected.lead.sample_phase)
        not in repaired_cells
    ]
    expected_retained = 27 - len(selected)
    if len(retained) != expected_retained:
        report["status"] = "PRIOR_SNAPSHOT_RECOVERY_FAILED"
        report["retained_articles_restored"] = len(retained)
        write_json(report_path, report)
        raise RuntimeError(
            f"expected {expected_retained} retained articles, restored {len(retained)}"
        )

    version = int(prior_basket["version"]) + 1
    basket = build_basket_payload(
        manifest,
        [*retained, *staging.staged],
        version=version,
        created_at=generated_at,
        change_reason=(
            f"תיקון רלוונטיות נושאית: {len(selected)} כתבות הוחלפו "
            f"לאחר ביקורת {case_id}; הסל המדויק מחייב אישור מחודש בשער 2."
        ),
    )
    current_state = registry.get_case(case_id)["current_state"]
    if current_state == CaseState.RUNNING.value:
        registry.advance(
            case_id,
            CaseState.AWAITING_ADJUDICATION,
            actor="system",
            reason="topic relevance repair requires a revised exact basket",
        )
    registry.save_basket(basket)
    report.update(
        {
            "status": "AWAITING_GATE_2",
            "basket_id": basket["basket_id"],
            "basket_version": basket["version"],
            "basket_hash": basket["basket_hash"],
            "article_count": len(basket["articles"]),
            "retained_articles": len(retained),
            "requires_human_approval": sum(
                1 for article in basket["articles"]
                if article["requires_human_approval"]
            ),
            "peak_rss_mb": peak_rss_mb(),
        }
    )
    write_json(report_path, report)
    registry.save_checkpoint(
        {
            "checkpoint_id": f"CHECKPOINT-{case_id}-TOPIC-REPAIR-V{version}",
            "case_id": case_id,
            "stage": "BASKET_DISCOVERY",
            "status": "COMPLETE",
            "run_id": f"TOPIC-REPAIR-{case_id}-V{version}",
            "completed_units": len(basket["articles"]),
            "total_units": 27,
            "cost_nis": 0,
            "peak_rss_mb": peak_rss_mb(),
            "artifact_refs": [str(report_path.relative_to(ROOT))],
            "recorded_at": generated_at,
            "failure_reason": None,
            "idempotency_key": f"{case_id}-topic-repair-v{version}",
        }
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reviewed-leads", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = args.plan if args.plan.is_absolute() else ROOT / args.plan
    reviewed_leads_path = (
        args.reviewed_leads
        if args.reviewed_leads.is_absolute()
        else ROOT / args.reviewed_leads
    )
    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    report = prepare_repair(
        case_id=args.case_id,
        plan_path=plan_path,
        reviewed_leads_path=reviewed_leads_path,
        report_path=report_path,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
