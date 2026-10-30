from __future__ import annotations

from enum import Enum
from datetime import date, timedelta
from typing import Any, Mapping


class CaseState(str, Enum):
    PROPOSED = "PROPOSED"
    AWAITING_TOPIC_APPROVAL = "AWAITING_TOPIC_APPROVAL"
    BASKET_DISCOVERY = "BASKET_DISCOVERY"
    AWAITING_BASKET_APPROVAL = "AWAITING_BASKET_APPROVAL"
    READY_TO_RUN = "READY_TO_RUN"
    RUNNING = "RUNNING"
    AWAITING_ADJUDICATION = "AWAITING_ADJUDICATION"
    FINALIZING = "FINALIZING"
    CLOSED = "CLOSED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class CaseTransitionError(ValueError):
    """Raised when a case attempts to bypass the frozen workflow."""


NORMAL_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.PROPOSED: frozenset({CaseState.AWAITING_TOPIC_APPROVAL}),
    CaseState.AWAITING_TOPIC_APPROVAL: frozenset(
        {CaseState.BASKET_DISCOVERY, CaseState.REJECTED}
    ),
    CaseState.BASKET_DISCOVERY: frozenset({CaseState.AWAITING_BASKET_APPROVAL}),
    CaseState.AWAITING_BASKET_APPROVAL: frozenset(
        {CaseState.BASKET_DISCOVERY, CaseState.READY_TO_RUN, CaseState.REJECTED}
    ),
    CaseState.READY_TO_RUN: frozenset(
        {CaseState.RUNNING, CaseState.AWAITING_BASKET_APPROVAL}
    ),
    CaseState.RUNNING: frozenset(
        {CaseState.AWAITING_ADJUDICATION, CaseState.FINALIZING}
    ),
    CaseState.AWAITING_ADJUDICATION: frozenset(
        {
            CaseState.RUNNING,
            CaseState.AWAITING_BASKET_APPROVAL,
            CaseState.FINALIZING,
        }
    ),
    CaseState.FINALIZING: frozenset({CaseState.CLOSED}),
    CaseState.CLOSED: frozenset(),
    CaseState.PAUSED: frozenset(),
    CaseState.FAILED: frozenset(),
    CaseState.REJECTED: frozenset(),
}

PAUSABLE_STATES = frozenset(
    state
    for state in CaseState
    if state not in {CaseState.CLOSED, CaseState.REJECTED, CaseState.PAUSED}
)

FAILABLE_STATES = frozenset(
    state
    for state in CaseState
    if state not in {
        CaseState.CLOSED,
        CaseState.REJECTED,
        CaseState.PAUSED,
        CaseState.FAILED,
    }
)


def require_transition(current: str | CaseState, target: str | CaseState) -> None:
    source = CaseState(current)
    destination = CaseState(target)
    if destination not in NORMAL_TRANSITIONS[source]:
        raise CaseTransitionError(
            f"transition {source.value} -> {destination.value} is not allowed"
        )


def manifest_from_candidate(
    candidate: Mapping[str, Any],
    case_id: str,
    *,
    max_project_cost_nis: float = 100,
) -> dict[str, Any]:
    """Create the post-topic-approval manifest without adding new model claims."""

    if not 0 < max_project_cost_nis <= 100:
        raise ValueError("max_project_cost_nis must be within the approved 100 NIS cap")
    window_start = date.fromisoformat(candidate["window_start"])
    candidate_end = date.fromisoformat(candidate["window_end"])
    research_end = max(candidate_end, window_start + timedelta(days=7))
    return {
        "case_id": case_id,
        "candidate_id": candidate["candidate_id"],
        "title": candidate["title"],
        "window_start": candidate["window_start"],
        "window_end": research_end.isoformat(),
        "scope_relation": candidate["scope_relation"],
        "responsibility_class": candidate["responsibility_class"],
        "evidence_clarity": candidate["evidence_clarity"],
        "research_purpose": candidate["neutral_summary"],
        "sample_slot": candidate["sample_balance_impact"]["fills_slot"],
        "keywords": {
            language: list(candidate["proposed_keywords"][language])
            for language in ("he", "en", "ar")
        },
        "target_articles": 27,
        "minimum_articles": 25,
        "topic_approval_hash": candidate["candidate_hash"],
        "methodology_version": candidate["methodology_version"],
        "max_project_cost_nis": max_project_cost_nis,
        "state": CaseState.BASKET_DISCOVERY.value,
    }
