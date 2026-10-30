from __future__ import annotations

import json
from pathlib import Path
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from .case_generator import manifest_from_candidate
from .case_registry import CaseRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def replacement_slot_decision_copy(
    slot_label: str,
    slot_articles: list[Mapping[str, Any]],
) -> str:
    """Describe the exact source substitution the researcher is approving."""
    source_counts = Counter(str(article["source_name"]) for article in slot_articles)
    composition = " + ".join(
        f"{count} {source_name}" for source_name, count in source_counts.items()
    )
    if len(source_counts) == 1:
        return (
            f"{slot_label}: {composition}. זוהי החלפה מלאה של גוף המקור "
            "במשבצת; אישור הסל מאשר אותה במפורש."
        )
    return (
        f"{slot_label}: {composition}. הגופים החלופיים מוגדרים באותו פרופיל "
        "ערכי; אישור הסל מאשר במפורש את ההרכב המעורב הזה."
    )


def approve_topic_candidate(
    registry: CaseRegistry,
    candidate: Mapping[str, Any],
    *,
    actor: str,
    rationale: str,
) -> str:
    case_id = registry.next_case_id()
    token = uuid.uuid4().hex
    decision = {
        "decision_id": f"DEC-TOPIC-{token[:16].upper()}",
        "case_id": case_id,
        "candidate_id": candidate["candidate_id"],
        "gate": "TOPIC",
        "target_hash": candidate["candidate_hash"],
        "decision": "APPROVE",
        "actor": actor,
        "recorded_at": _utc_now(),
        "rationale": rationale.strip() or "נושא הקייס אושר בשער הראשון.",
        "idempotency_key": f"topic-approval-{token}",
    }
    registry.record_topic_decision(
        candidate["candidate_id"], int(candidate["version"]), decision
    )
    registry.register_manifest(manifest_from_candidate(candidate, case_id))
    return case_id


def reject_topic_candidate(
    registry: CaseRegistry,
    candidate: Mapping[str, Any],
    *,
    actor: str,
    rationale: str,
) -> None:
    if not rationale.strip():
        raise ValueError("rejection rationale is required")
    token = uuid.uuid4().hex
    decision = {
        "decision_id": f"DEC-TOPIC-{token[:16].upper()}",
        "case_id": None,
        "candidate_id": candidate["candidate_id"],
        "gate": "TOPIC",
        "target_hash": candidate["candidate_hash"],
        "decision": "REJECT",
        "actor": actor,
        "recorded_at": _utc_now(),
        "rationale": rationale.strip(),
        "idempotency_key": f"topic-rejection-{token}",
    }
    registry.record_topic_decision(
        candidate["candidate_id"], int(candidate["version"]), decision
    )


def decide_article_basket(
    registry: CaseRegistry,
    case_id: str,
    *,
    decision: str,
    actor: str,
    rationale: str,
) -> None:
    if decision not in {"APPROVE", "REQUEST_CHANGES", "REJECT"}:
        raise ValueError("unsupported basket decision")
    if decision != "APPROVE" and not rationale.strip():
        raise ValueError("basket change or rejection rationale is required")
    basket = registry.get_basket(case_id)
    case = registry.get_case(case_id)
    methodology = str((case.get("manifest") or {}).get("methodology_version") or "")
    if decision == "APPROVE" and methodology == "case-generator-contract-v1.2":
        project_root = Path(registry.path).resolve().parent.parent
        audit_path = (
            project_root
            / "analysis/case_generator"
            / case_id.lower()
            / "topic_relevance_audit_v2.json"
        )
        if not audit_path.exists():
            raise ValueError("topic relevance audit is required before Gate 2 approval")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if (
            audit.get("basket_hash") != basket.get("basket_hash")
            or audit.get("status") != "PASS"
        ):
            raise ValueError(
                "the exact basket must pass the autonomous topic relevance gate"
            )
    token = uuid.uuid4().hex
    payload = {
        "decision_id": f"DEC-BASKET-{token[:16].upper()}",
        "case_id": case_id,
        "gate": "BASKET",
        "target_hash": basket["basket_hash"],
        "decision": decision,
        "actor": actor,
        "recorded_at": _utc_now(),
        "rationale": rationale.strip() or "סל הכתבות אושר בשער השני.",
        "idempotency_key": f"basket-{decision.lower()}-{token}",
    }
    registry.record_basket_decision(case_id, payload)
