#!/usr/bin/env python3
"""Discover and stage one approved case basket without cloud writes or model calls."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.basket_builder import (  # noqa: E402
    BasketFailure,
    GDELTBasketLeadProvider,
    GoogleNewsBasketLeadProvider,
    PHASES,
    PHASE_POLICY_VERSION,
    build_basket_payload,
    discover_basket_leads,
    load_publisher_roster,
    load_reviewed_leads,
    missing_roster_source_ids,
    publisher_roster_path,
    select_roster_articles,
    stage_selected_articles,
)
from aletheia_lite.basket_progress import (  # noqa: E402
    case_run_lock,
    input_signature,
    load_attempt_cache,
    load_lead_cache,
    load_stage_cache,
    mark_snapshot_recovery_complete,
    previous_basket_leads,
    recover_staged_from_snapshot_cache,
    recover_compatible_stage_cache,
    restore_staged_from_previous_basket,
    save_attempt_cache,
    save_lead_cache,
    save_run_state,
    save_stage_cache,
    snapshot_recovery_is_complete,
    staged_article_to_dict,
)
from aletheia_lite.case_registry import CaseRegistry, CaseRegistryError  # noqa: E402
from aletheia_lite.case_generator_contracts import (  # noqa: E402
    basket_article_payload_changed,
)
from aletheia_lite.collector import BoundedHTTPClient, canonicalize_url  # noqa: E402
from aletheia_lite.config import NetworkLimits  # noqa: E402
from aletheia_lite.topic_relevance import audit_basket_with_vertex  # noqa: E402


REGISTRY_PATH = ROOT / "data/case_registry.sqlite3"
STAGING_ROOT = ROOT / "data/case_staging"
REVIEWED_LEADS_ROOT = ROOT / "case_generator" / "reviewed_leads"
MAX_CUMULATIVE_ATTEMPTS_PER_URL = 2


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


def missing_cells(roster, completed: set[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"publisher_slot": slot.slot_id, "sample_phase": phase}
        for slot in roster.slots
        for phase in PHASES
        if (slot.slot_id, phase) not in completed
    ]


def save_progress(
    case_id: str,
    run_id: str,
    *,
    stage: str,
    staged_count: int,
    attempted_count: int,
    missing_count: int,
    message: str,
) -> None:
    save_run_state(
        ROOT,
        case_id,
        run_id,
        "RUNNING",
        pid=os.getpid(),
        stage=stage,
        articles_staged=staged_count,
        attempted_urls=attempted_count,
        missing_cells=missing_count,
        total_units=27,
        message=message,
        peak_rss_mb=peak_rss_mb(),
        cloud_writes=0,
        model_calls=0,
    )


def discover_and_stage_locked(case_id: str, run_id: str) -> dict[str, Any]:
    registry = CaseRegistry(REGISTRY_PATH)
    case = registry.get_case(case_id)
    report_path = (
        ROOT / "analysis" / "case_generator" / case_id.lower() / "basket_discovery_v1.json"
    )
    if case["current_state"] == "AWAITING_BASKET_APPROVAL":
        basket = registry.get_basket(case_id)
        return {
            "status": "ALREADY_AWAITING_APPROVAL",
            "case_id": case_id,
            "basket_hash": basket["basket_hash"],
            "articles": len(basket["articles"]),
        }
    if case["current_state"] != "BASKET_DISCOVERY" or not case["manifest"]:
        raise CaseRegistryError("case is not ready for local basket discovery")

    manifest = case["manifest"]
    target_articles = int(manifest.get("target_articles") or 27)
    latest_basket_decision = registry.latest_decision(case_id, "BASKET")
    is_requested_change = bool(
        latest_basket_decision
        and latest_basket_decision.get("decision") == "REQUEST_CHANGES"
    )
    roster = load_publisher_roster(publisher_roster_path(ROOT, case_id))
    signature = input_signature(manifest, roster)
    client = BoundedHTTPClient(
        NetworkLimits(
            timeout_seconds=12,
            max_response_bytes=5_000_000,
            requests_per_second_per_host=1,
            retries=1,
            min_text_chars=250,
            max_text_chars=30_000,
        )
    )

    staged = load_stage_cache(ROOT, case_id, signature, roster)
    if not staged:
        staged = recover_compatible_stage_cache(
            ROOT, case_id, roster, manifest, STAGING_ROOT
        )
        if staged:
            save_stage_cache(ROOT, case_id, signature, staged)
    attempt_counts = load_attempt_cache(ROOT, case_id, signature)
    if not attempt_counts and report_path.exists():
        try:
            previous_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_report = {}
        for failure in previous_report.get("failures") or []:
            url = canonicalize_url(str(failure.get("url") or ""))
            if url:
                attempt_counts[url] = max(attempt_counts.get(url, 0), 1)
        if attempt_counts:
            save_attempt_cache(ROOT, case_id, signature, attempt_counts)
    completed_cells = {
        (item.selected.slot.slot_id, item.selected.lead.sample_phase)
        for item in staged
    }
    content_hashes = {item.content_hash for item in staged}
    attempted_urls: set[str] = set()
    failures: list[BasketFailure] = []
    save_progress(
        case_id,
        run_id,
        stage="RESUME",
        staged_count=len(staged),
        attempted_count=0,
        missing_count=27 - len(completed_cells),
        message=(
            f"נטענו {len(staged)} כתבות תקינות; ממשיכים רק במשבצות החסרות."
            if staged
            else "מכין גילוי ראשון ושמירה מתמשכת של הסל."
        ),
    )

    reviewed_leads = load_reviewed_leads(
        REVIEWED_LEADS_ROOT / f"{case_id.lower()}.json",
        manifest,
        roster,
    )
    try:
        prior_basket = registry.get_basket(case_id)
    except CaseRegistryError:
        prior_basket = None
    prior_leads = previous_basket_leads(prior_basket, roster)
    source_to_slot = {
        outlet.source_id: slot.slot_id
        for slot in roster.slots
        for outlet in slot.outlets
    }
    seeded_cells = {
        (source_to_slot[lead.source_id], lead.sample_phase)
        for lead in [*prior_leads, *reviewed_leads]
        if lead.source_id in source_to_slot
    }

    cached_leads = load_lead_cache(ROOT, case_id, signature)
    if cached_leads is None:
        if len(completed_cells | seeded_cells) == 27:
            leads = []
            provider_counts = {}
        else:
            save_progress(
                case_id,
                run_id,
                stage="DISCOVERY",
                staged_count=len(staged),
                attempted_count=0,
                missing_count=27 - len(completed_cells),
                message="מאתר קישורים ציבוריים בשלוש השפות.",
            )
            primary = GoogleNewsBasketLeadProvider(client, max_candidates_per_phase=3)
            leads, discovery_failures = discover_basket_leads(
                manifest, roster, [primary]
            )
            provider_counts = {primary.provider_id: len(leads)}
            fallback = GDELTBasketLeadProvider(client, max_records=75)
            missing_source_ids = missing_roster_source_ids(leads, roster)
            if missing_source_ids:
                extra, extra_failures = discover_basket_leads(
                    manifest,
                    roster,
                    [fallback],
                    source_ids=missing_source_ids,
                )
                discovery_failures.extend(extra_failures)
                provider_counts[fallback.provider_id] = len(extra)
                known = {
                    (lead.source_id, canonicalize_url(lead.url)) for lead in leads
                }
                for lead in extra:
                    key = (lead.source_id, canonicalize_url(lead.url))
                    if key not in known:
                        leads.append(lead)
                        known.add(key)
            failures.extend(discovery_failures)
        save_lead_cache(ROOT, case_id, signature, leads, provider_counts)
    else:
        leads, provider_counts = cached_leads

    # A researcher-requested revision is a new discovery pass, not a replay of
    # the old lead cache. Refresh only the still-missing publisher slots so the
    # retry remains bounded and inexpensive.
    if is_requested_change and cached_leads is not None:
        missing_slot_ids = {
            slot.slot_id
            for slot in roster.slots
            if any((slot.slot_id, phase) not in completed_cells for phase in PHASES)
        }
        refresh_source_ids = {
            outlet.source_id
            for slot in roster.slots
            if slot.slot_id in missing_slot_ids
            for outlet in slot.outlets
        }
        if refresh_source_ids:
            save_progress(
                case_id,
                run_id,
                stage="DISCOVERY_REFRESH",
                staged_count=len(staged),
                attempted_count=0,
                missing_count=27 - len(completed_cells),
                message="מרענן איתור ציבורי רק עבור המשבצות שהתבקשו להשלמה.",
            )
            primary = GoogleNewsBasketLeadProvider(client, max_candidates_per_phase=3)
            fresh, refresh_failures = discover_basket_leads(
                manifest, roster, [primary], source_ids=refresh_source_ids
            )
            failures.extend(refresh_failures)
            provider_counts[f"{primary.provider_id}-refresh"] = len(fresh)
            still_missing_sources = missing_roster_source_ids(fresh, roster)
            still_missing_sources.intersection_update(refresh_source_ids)
            if still_missing_sources:
                fallback = GDELTBasketLeadProvider(client, max_records=75)
                extra, extra_failures = discover_basket_leads(
                    manifest,
                    roster,
                    [fallback],
                    source_ids=still_missing_sources,
                )
                failures.extend(extra_failures)
                provider_counts[f"{fallback.provider_id}-refresh"] = len(extra)
                fresh.extend(extra)
            known = {
                (lead.source_id, canonicalize_url(lead.url)) for lead in leads
            }
            for lead in fresh:
                key = (lead.source_id, canonicalize_url(lead.url))
                if key not in known:
                    leads.append(lead)
                    known.add(key)
            save_lead_cache(ROOT, case_id, signature, leads, provider_counts)

    known_leads = {
        (lead.source_id, canonicalize_url(lead.url)): index
        for index, lead in enumerate(leads)
    }
    reviewed_added = 0
    for lead in reviewed_leads:
        key = (lead.source_id, canonicalize_url(lead.url))
        if key in known_leads:
            # A reviewed publisher URL is authoritative for date/phase/title
            # metadata and may correct an older cached discovery record.
            index = known_leads[key]
            if leads[index] != lead:
                leads[index] = lead
                reviewed_added += 1
        else:
            leads.append(lead)
            known_leads[key] = len(leads) - 1
            reviewed_added += 1
    if reviewed_added:
        provider_counts["reviewed-public-search"] = len(reviewed_leads)
        save_lead_cache(ROOT, case_id, signature, leads, provider_counts)

    prior_added = 0
    for lead in prior_leads:
        key = (lead.source_id, canonicalize_url(lead.url))
        if key not in known_leads:
            leads.append(lead)
            known_leads[key] = len(leads) - 1
            prior_added += 1
    if prior_leads:
        provider_counts["previous-basket"] = len(prior_leads)
    if prior_added:
        save_lead_cache(ROOT, case_id, signature, leads, provider_counts)

    if prior_basket:
        restored_prior = restore_staged_from_previous_basket(
            prior_basket, roster, STAGING_ROOT, staged
        )
        if len(restored_prior) > len(staged):
            staged = restored_prior
            completed_cells = {
                (item.selected.slot.slot_id, item.selected.lead.sample_phase)
                for item in staged
            }
            content_hashes = {item.content_hash for item in staged}
            save_stage_cache(ROOT, case_id, signature, staged)

    recovery_incomplete = not snapshot_recovery_is_complete(
        ROOT, case_id, signature
    )
    # A case-specific roster may replace an inaccessible publisher between
    # discovery passes.  Old basket leads are useful only when their source is
    # still part of the active roster; silently ignore retired source IDs.
    prior_cells = {
        (source_to_slot[lead.source_id], lead.sample_phase)
        for lead in prior_leads
        if lead.source_id in source_to_slot
    }
    staged_cells = {
        (item.selected.slot.slot_id, item.selected.lead.sample_phase)
        for item in staged
    }
    if recovery_incomplete or not prior_cells.issubset(staged_cells):
        recovered = recover_staged_from_snapshot_cache(
            manifest, leads, roster, STAGING_ROOT, staged
        )
        if len(recovered) > len(staged):
            staged = recovered
            completed_cells = {
                (item.selected.slot.slot_id, item.selected.lead.sample_phase)
                for item in staged
            }
            content_hashes = {item.content_hash for item in staged}
            save_stage_cache(ROOT, case_id, signature, staged)
            save_progress(
                case_id,
                run_id,
                stage="RESUME",
                staged_count=len(staged),
                attempted_count=0,
                missing_count=27 - len(completed_cells),
                message=f"שוחזרו {len(staged)} כתבות מן ה־snapshots המקומיים.",
            )
        mark_snapshot_recovery_complete(ROOT, case_id, signature)

    outlet_to_slot = {
        outlet.source_id: slot.slot_id
        for slot in roster.slots
        for outlet in slot.outlets
    }
    while len(staged) < target_articles:
        candidates = [
            lead
            for lead in leads
            if lead.source_id in outlet_to_slot
            and canonicalize_url(lead.url) not in attempted_urls
            and attempt_counts.get(canonicalize_url(lead.url), 0)
            < MAX_CUMULATIVE_ATTEMPTS_PER_URL
            and (outlet_to_slot[lead.source_id], lead.sample_phase) not in completed_cells
        ]
        lowest_attempt_count: dict[tuple[str, str], int] = {}
        for lead in candidates:
            cell = (outlet_to_slot[lead.source_id], lead.sample_phase)
            count = attempt_counts.get(canonicalize_url(lead.url), 0)
            lowest_attempt_count[cell] = min(
                count, lowest_attempt_count.get(cell, count)
            )
        eligible = [
            lead
            for lead in candidates
            if attempt_counts.get(canonicalize_url(lead.url), 0)
            == lowest_attempt_count[(outlet_to_slot[lead.source_id], lead.sample_phase)]
        ]
        selected = select_roster_articles(eligible, roster)
        if not selected:
            break
        pass_progress = 0
        for item in selected:
            canonical_attempt = canonicalize_url(item.lead.url)
            attempted_urls.add(canonical_attempt)
            attempt_counts[canonical_attempt] = attempt_counts.get(canonical_attempt, 0) + 1
            save_attempt_cache(ROOT, case_id, signature, attempt_counts)
            cell = (item.slot.slot_id, item.lead.sample_phase)
            save_progress(
                case_id,
                run_id,
                stage="STAGING",
                staged_count=len(staged),
                attempted_count=len(attempted_urls),
                missing_count=27 - len(completed_cells),
                message=f"בודק HTML: {item.outlet.name} · {item.lead.sample_phase}",
            )
            result = stage_selected_articles(
                manifest,
                [item],
                STAGING_ROOT,
                client=client,
            )
            failures.extend(result.failures)
            if not result.staged:
                continue
            candidate = result.staged[0]
            if candidate.content_hash in content_hashes:
                failures.append(
                    BasketFailure(
                        slot_id=item.slot.slot_id,
                        source_id=item.outlet.source_id,
                        sample_phase=item.lead.sample_phase,
                        url=item.lead.url,
                        message="duplicate article content across staging passes",
                    )
                )
                continue
            content_hashes.add(candidate.content_hash)
            completed_cells.add(cell)
            staged.append(candidate)
            pass_progress += 1
            save_stage_cache(ROOT, case_id, signature, staged)
            save_progress(
                case_id,
                run_id,
                stage="STAGING",
                staged_count=len(staged),
                attempted_count=len(attempted_urls),
                missing_count=27 - len(completed_cells),
                message=f"נשמרה כתבה {len(staged)} מתוך יעד 27.",
            )
        if pass_progress == 0 and not any(
            lead.source_id in outlet_to_slot
            and canonicalize_url(lead.url) not in attempted_urls
            and attempt_counts.get(canonicalize_url(lead.url), 0)
            < MAX_CUMULATIVE_ATTEMPTS_PER_URL
            and (outlet_to_slot[lead.source_id], lead.sample_phase) not in completed_cells
            for lead in leads
        ):
            break

    generated_at = utc_now()
    token = hashlib.sha256(generated_at.encode("utf-8")).hexdigest()[:12]
    provider_unavailable = not leads and any(
        item.sample_phase == "DISCOVERY" for item in failures
    )
    missing = missing_cells(roster, completed_cells)
    discovered_cells = {
        (outlet_to_slot[lead.source_id], lead.sample_phase)
        for lead in leads
        if lead.source_id in outlet_to_slot
    }
    uncovered_by_discovery = missing_cells(roster, discovered_cells)
    gap_details = []
    retryable_urls: set[str] = set()
    for cell in missing:
        cell_key = (cell["publisher_slot"], cell["sample_phase"])
        cell_urls = {
            canonicalize_url(lead.url)
            for lead in leads
            if lead.source_id in outlet_to_slot
            and (outlet_to_slot[lead.source_id], lead.sample_phase) == cell_key
        }
        cell_retryable = {
            url
            for url in cell_urls
            if attempt_counts.get(url, 0) < MAX_CUMULATIVE_ATTEMPTS_PER_URL
        }
        retryable_urls.update(cell_retryable)
        gap_details.append(
            {
                **cell,
                "candidate_urls": len(cell_urls),
                "retryable_urls": len(cell_retryable),
                "block_reason": (
                    "NO_CANDIDATE_DISCOVERED"
                    if not cell_urls
                    else "CANDIDATES_EXHAUSTED"
                    if not cell_retryable
                    else "RETRY_AVAILABLE"
                ),
            }
        )
    requires_human_decision = bool(
        len(staged) < target_articles
        and not provider_unavailable
        and not retryable_urls
    )
    status = (
        "READY_FOR_GATE"
        if len(staged) >= target_articles
        else "DISCOVERY_FAILED"
        if provider_unavailable
        else "HUMAN_DECISION_REQUIRED"
        if requires_human_decision
        else "INSUFFICIENT_BASKET"
    )
    report = {
        "schema_version": "1.1",
        "case_id": case_id,
        "run_id": run_id,
        "generated_at": generated_at,
        "status": status,
        "providers": provider_counts,
        "roster_version": roster.roster_version,
        "phase_policy_version": PHASE_POLICY_VERSION,
        "leads_discovered": len(leads),
        "attempted_urls": len(attempted_urls),
        "articles_staged": len(staged),
        "staged_articles": [staged_article_to_dict(item) for item in staged],
        "failures": [asdict(item) for item in failures],
        "missing_cells": missing,
        "uncovered_by_discovery": uncovered_by_discovery,
        "gap_details": gap_details,
        "retryable_urls": len(retryable_urls),
        "max_attempts_per_url": MAX_CUMULATIVE_ATTEMPTS_PER_URL,
        "requires_human_decision": requires_human_decision,
        "peak_rss_mb": peak_rss_mb(),
        "cloud_writes": 0,
        "model_calls": 0,
        "requested_change": (
            {
                "target_hash": latest_basket_decision.get("target_hash"),
                "rationale": latest_basket_decision.get("rationale"),
            }
            if is_requested_change
            else None
        ),
    }

    if len(staged) < target_articles:
        write_json(report_path, report)
        waiting_status = (
            "WAITING_HUMAN_DECISION"
            if requires_human_decision
            else "WAITING_RETRY"
        )
        registry.save_checkpoint({
            "checkpoint_id": f"CHECKPOINT-{case_id}-BASKET-{token.upper()}",
            "case_id": case_id,
            "stage": "BASKET_DISCOVERY",
            "status": waiting_status,
            "run_id": run_id,
            "completed_units": len(staged),
            "total_units": 27,
            "cost_nis": 0,
            "peak_rss_mb": peak_rss_mb(),
            "artifact_refs": [str(report_path.relative_to(ROOT))],
            "recorded_at": generated_at,
            "failure_reason": (
                "public discovery providers were unavailable"
                if provider_unavailable
                else "source substitution decision is required"
                if requires_human_decision
                else "fewer than 25 articles passed local staging"
            ),
            "idempotency_key": f"{case_id}-basket-{run_id}-waiting-retry",
        })
        save_run_state(
            ROOT,
            case_id,
            run_id,
            (
                "AWAITING_HUMAN_DECISION"
                if requires_human_decision
                else "WAITING_RETRY"
            ),
            pid=os.getpid(),
            stage="COMPLETE",
            articles_staged=len(staged),
            attempted_urls=len(attempted_urls),
            missing_cells=len(missing),
            total_units=27,
            message=(
                "האפשרויות האוטומטיות מוצו; נדרשת הכרעת חוקר על החלפת מקור."
                if requires_human_decision
                else "הניסיון הסתיים; הסל נשמר וממתין להשלמת המשבצות החסרות."
            ),
            report_path=str(report_path.relative_to(ROOT)),
            peak_rss_mb=peak_rss_mb(),
            cloud_writes=0,
            model_calls=0,
        )
        return report

    try:
        previous = registry.get_basket(case_id)
        version = int(previous["version"]) + 1
    except CaseRegistryError:
        version = 1
    basket = build_basket_payload(
        manifest,
        staged,
        version=version,
        created_at=generated_at,
        change_reason="סל שנבנה בגילוי ציבורי ובסקרייפ מקומי מוגבל.",
    )
    if prior_basket and not basket_article_payload_changed(prior_basket, basket):
        report["status"] = "NO_PROGRESS"
        report["previous_basket_hash"] = prior_basket["basket_hash"]
        report["failure_reason"] = (
            "ההרצה לא הוסיפה, החליפה או תיקנה אף רשומת כתבה בסל."
        )
        write_json(report_path, report)
        registry.save_checkpoint({
            "checkpoint_id": f"CHECKPOINT-{case_id}-BASKET-{token.upper()}",
            "case_id": case_id,
            "stage": "BASKET_DISCOVERY",
            "status": "NO_PROGRESS",
            "run_id": run_id,
            "completed_units": len(staged),
            "total_units": 27,
            "cost_nis": 0,
            "peak_rss_mb": peak_rss_mb(),
            "artifact_refs": [str(report_path.relative_to(ROOT))],
            "recorded_at": generated_at,
            "failure_reason": report["failure_reason"],
            "idempotency_key": f"{case_id}-basket-{run_id}-no-progress",
        })
        save_run_state(
            ROOT,
            case_id,
            run_id,
            "NO_PROGRESS",
            pid=os.getpid(),
            stage="COMPLETE",
            articles_staged=len(staged),
            attempted_urls=len(attempted_urls),
            missing_cells=len(missing),
            total_units=27,
            message=(
                "לא נמצאה כתבה חדשה או חלופה מאומתת; הקייס נשאר בבניית הסל."
            ),
            previous_basket_hash=prior_basket["basket_hash"],
            report_path=str(report_path.relative_to(ROOT)),
            peak_rss_mb=peak_rss_mb(),
            cloud_writes=0,
            model_calls=0,
        )
        return report
    audit_model_calls = 0
    audit_cost_nis = 0.0
    if manifest.get("methodology_version") == "case-generator-contract-v1.2":
        audit_path = (
            ROOT
            / "analysis/case_generator"
            / case_id.lower()
            / "topic_relevance_audit_v2.json"
        )
        cached_audit = None
        if audit_path.exists():
            try:
                candidate_audit = json.loads(audit_path.read_text(encoding="utf-8"))
                if candidate_audit.get("basket_hash") == basket["basket_hash"]:
                    cached_audit = candidate_audit
            except (OSError, json.JSONDecodeError):
                cached_audit = None
        audit = cached_audit or audit_basket_with_vertex(
            manifest,
            basket,
            staging_root=STAGING_ROOT,
            batch_size=3,
        )
        if cached_audit is None:
            write_json(audit_path, audit)
        audit_model_calls = int(audit["model_calls"])
        audit_cost_nis = float(audit["cost_estimate"]["upper_bound_nis"])
        report["topic_relevance"] = {
            "status": audit["status"],
            "summary": audit["summary"],
            "audit_ref": str(audit_path.relative_to(ROOT)),
            "model_calls": audit_model_calls,
            "cost_estimate": audit["cost_estimate"],
        }
        if audit["status"] != "PASS":
            report["requires_human_decision"] = True
    registry.save_basket(basket)
    report["basket_hash"] = basket["basket_hash"]
    report["basket_version"] = basket["version"]
    write_json(report_path, report)
    registry.save_checkpoint({
        "checkpoint_id": f"CHECKPOINT-{case_id}-BASKET-V{version}",
        "case_id": case_id,
        "stage": "BASKET_DISCOVERY",
        "status": "COMPLETE",
        "run_id": run_id,
        "completed_units": len(staged),
        "total_units": 27,
        "cost_nis": audit_cost_nis,
        "peak_rss_mb": peak_rss_mb(),
        "artifact_refs": [str(report_path.relative_to(ROOT))],
        "recorded_at": generated_at,
        "failure_reason": None,
        "idempotency_key": f"{case_id}-basket-v{version}-complete",
    })
    save_run_state(
        ROOT,
        case_id,
        run_id,
        "AWAITING_GATE_2",
        pid=os.getpid(),
        stage="COMPLETE",
        articles_staged=len(staged),
        attempted_urls=len(attempted_urls),
        missing_cells=len(missing),
        total_units=27,
        message=(
            "הסל עבר בדיקת התאמת נושא וממתין לאישור החוקר בשער 2."
            if not report.get("topic_relevance")
            or report["topic_relevance"]["status"] == "PASS"
            else "הסל ממתין לתיקון או להכרעת חוקר בבדיקת התאמת הנושא."
        ),
        basket_hash=basket["basket_hash"],
        report_path=str(report_path.relative_to(ROOT)),
        peak_rss_mb=peak_rss_mb(),
        cloud_writes=0,
        model_calls=audit_model_calls,
    )
    return report


def discover_and_stage(case_id: str, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or f"BASKET-{case_id}-{uuid.uuid4().hex[:12].upper()}"
    with case_run_lock(ROOT, case_id, run_id):
        save_run_state(
            ROOT,
            case_id,
            run_id,
            "RUNNING",
            pid=os.getpid(),
            stage="STARTING",
            articles_staged=0,
            attempted_urls=0,
            missing_cells=27,
            total_units=27,
            message="הרצת גילוי הסל התחילה.",
            started_at=utc_now(),
            peak_rss_mb=peak_rss_mb(),
            cloud_writes=0,
            model_calls=0,
        )
        try:
            return discover_and_stage_locked(case_id, run_id)
        except Exception as error:
            save_run_state(
                ROOT,
                case_id,
                run_id,
                "FAILED",
                pid=os.getpid(),
                stage="FAILED",
                message=f"הריצה נעצרה: {error}",
                finished_at=utc_now(),
                peak_rss_mb=peak_rss_mb(),
                cloud_writes=0,
                model_calls=0,
            )
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--full-json",
        action="store_true",
        help="Print the full report instead of the compact run summary.",
    )
    args = parser.parse_args()
    payload = discover_and_stage(args.case_id, args.run_id)
    output = payload if args.full_json else {
        "case_id": payload["case_id"],
        "run_id": payload["run_id"],
        "status": payload["status"],
        "articles_staged": payload["articles_staged"],
        "missing_cells": len(payload.get("missing_cells", [])),
        "requires_human_decision": payload.get("requires_human_decision", False),
        "peak_rss_mb": payload.get("peak_rss_mb", 0),
    }
    print(json.dumps(output, ensure_ascii=False))
    return {
        "INSUFFICIENT_BASKET": 2,
        "DISCOVERY_FAILED": 3,
    }.get(payload["status"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
