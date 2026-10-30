from __future__ import annotations

from copy import deepcopy
from typing import Any


def _hard_difference(left: int | None, right: int | None) -> bool:
    if (left is None) != (right is None):
        return True
    return (
        isinstance(left, int)
        and isinstance(right, int)
        and abs(left - right) >= 2
    )


def _ballots_are_strong(review: dict[str, Any]) -> bool:
    ballots = (review.get("ballot_1") or {}, review.get("ballot_2") or {})
    return all(
        ballot.get("confidence") == "HIGH"
        and ballot.get("evidence_sufficiency") == "SUFFICIENT"
        and not (ballot.get("residual_risks") or [])
        for ballot in ballots
    )


def _stable_numeric_na_reclassification(
    review: dict[str, Any], component: str
) -> bool:
    """Allow a unanimous, evidence-complete appeal to settle applicability.

    A numeric/N/A mismatch is not sent to the principal researcher merely
    because the first pass applied a conservative structural gate.  For T1,
    both order-reversed ballots must also cite an active anchor and an exact
    article exhibit.
    """

    if not review.get("order_stable") or not _ballots_are_strong(review):
        return False
    proposed_score = review.get("proposed_score")
    if component == "T1" and isinstance(proposed_score, int):
        return all(
            (review.get(key) or {}).get("anchor_refs")
            and (review.get(key) or {}).get("exact_quote_used")
            for key in ("ballot_1", "ballot_2")
        )
    return True


def _order_sensitive_zero_na_equivalence(
    review: dict[str, Any], component: str
) -> bool:
    """Treat N1/N3 zero versus N/A as a non-substantive applicability edge.

    Both outcomes mean that no counterevidence problem was established.  When
    two strong ballots differ only on whether the component is applicable, the
    original coding is retained instead of creating human work.
    """

    if component not in {"N1", "N3"} or not _ballots_are_strong(review):
        return False
    scores = {
        (review.get("ballot_1") or {}).get("proposed_score"),
        (review.get("ballot_2") or {}).get("proposed_score"),
    }
    return scores == {0, None}


def _ungrounded_stable_t1_numeric_challenge(
    review: dict[str, Any], component: str
) -> bool:
    if component != "T1" or not review.get("order_stable"):
        return False
    if not isinstance(review.get("proposed_score"), int):
        return False
    return not all(
        (review.get(key) or {}).get("anchor_refs")
        for key in ("ballot_1", "ballot_2")
    )


def _ungrounded_counterevidence_challenge(dilemma: dict[str, Any]) -> bool:
    challenge = dilemma.get("challenge") or {}
    score = challenge.get("score")
    return (
        dilemma.get("component") in {"N1", "N3"}
        and isinstance(score, int)
        and score >= 2
        and not (challenge.get("anchor_refs") or [])
    )


def repair_open_human_docket(
    principal_review: dict[str, Any],
    *,
    decided_dilemma_ids: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Auto-close only still-open N1/N3 challenges that violate the rulebook.

    Existing principal-researcher decisions are deliberately preserved even if
    an older packet would now fail the stricter deterministic gate.
    """

    repaired = deepcopy(principal_review)
    kept: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    for dilemma in repaired.get("human_dilemmas", []):
        if (
            dilemma["dilemma_id"] in decided_dilemma_ids
            or not _ungrounded_counterevidence_challenge(dilemma)
        ):
            kept.append(dilemma)
            continue
        model = dilemma.get("model") or {}
        moved.append(deepcopy(dilemma))
        repaired.setdefault("auto_resolutions", []).append({
            "dilemma_id": dilemma["dilemma_id"],
            "document_id": dilemma["document_id"],
            "component": dilemma["component"],
            "model_score": model.get("score"),
            "model_na_reason": model.get("na_reason"),
            "second_review": dilemma.get("second_review"),
            "policy_version": "principal-escalation-v4-two-point",
            "reason": "COUNTEREVIDENCE_ANCHOR_REQUIRED",
            "final_score": model.get("score"),
            "final_na_reason": model.get("na_reason"),
            "resolution": "UNGROUNDED_COUNTEREVIDENCE_CHALLENGE_REJECTED",
        })
    repaired["human_dilemmas"] = kept
    summary = repaired.setdefault("summary", {})
    summary["reviewed_dilemmas"] = len(repaired.get("auto_resolutions", [])) + len(kept)
    summary["auto_resolved"] = len(repaired.get("auto_resolutions", []))
    summary["human_required"] = len(kept)
    return repaired, moved


def resolve_second_review(
    debate_packet: dict[str, Any],
    reconciled: dict[str, Any],
) -> dict[str, Any]:
    """Apply the approved two-point human-escalation policy.

    Stable appellate outcomes within one point are accepted automatically.
    Order-sensitive ballots are escalated only when at least one ballot has a
    hard difference from the original score. A unanimous, high-confidence and
    evidence-complete appellate result may settle numeric/N/A applicability;
    N1/N3 zero-versus-N/A order effects retain the original coding.
    """

    debate = {row["dilemma_id"]: row for row in debate_packet["dilemmas"]}
    appellate = {
        row["dilemma_id"]: row
        for document in reconciled["documents"]
        for row in document["decisions"]
    }
    if set(debate) != set(appellate):
        raise ValueError("second-review decisions do not cover the debate packet")
    auto: list[dict[str, Any]] = []
    human: list[dict[str, Any]] = []

    for dilemma_id, dilemma in debate.items():
        review = appellate[dilemma_id]
        model_score = dilemma["model_score"]
        full_dilemma = dilemma["full_dilemma"]
        if _ungrounded_counterevidence_challenge(full_dilemma):
            auto.append({
                "dilemma_id": dilemma_id,
                "document_id": dilemma["document_id"],
                "component": dilemma["component"],
                "model_score": model_score,
                "model_na_reason": dilemma.get("model_na_reason"),
                "second_review": review,
                "policy_version": "principal-escalation-v5-applicability",
                "reason": "COUNTEREVIDENCE_ANCHOR_REQUIRED",
                "final_score": model_score,
                "final_na_reason": dilemma.get("model_na_reason"),
                "resolution": "UNGROUNDED_COUNTEREVIDENCE_CHALLENGE_REJECTED",
            })
            continue
        if _ungrounded_stable_t1_numeric_challenge(review, dilemma["component"]):
            auto.append({
                "dilemma_id": dilemma_id,
                "document_id": dilemma["document_id"],
                "component": dilemma["component"],
                "model_score": model_score,
                "model_na_reason": dilemma.get("model_na_reason"),
                "second_review": review,
                "policy_version": "principal-escalation-v5-applicability",
                "reason": "T1_ACTIVE_ANCHOR_REQUIRED",
                "final_score": model_score,
                "final_na_reason": dilemma.get("model_na_reason"),
                "resolution": "UNGROUNDED_T1_NUMERIC_CHALLENGE_REJECTED",
            })
            continue
        if review["order_stable"]:
            proposed_score = review["proposed_score"]
            proposed_na_reason = review["proposed_na_reason"]
            numeric_na_mismatch = (model_score is None) != (
                proposed_score is None
            )
            stable_reclassification = (
                numeric_na_mismatch
                and _stable_numeric_na_reclassification(
                    review, dilemma["component"]
                )
            )
            hard = (
                False
                if stable_reclassification
                else _hard_difference(model_score, proposed_score)
            )
            reason = (
                "STABLE_HIGH_CONFIDENCE_NUMERIC_NA_RECLASSIFICATION"
                if stable_reclassification
                else "NUMERIC_NA_MISMATCH"
                if numeric_na_mismatch
                else (
                    "SCORE_GAP_AT_LEAST_TWO" if hard
                    else "BELOW_TWO_POINT_HUMAN_THRESHOLD"
                )
            )
        else:
            ballot_scores = [
                review["ballot_1"].get("proposed_score"),
                review["ballot_2"].get("proposed_score"),
            ]
            zero_na_equivalence = _order_sensitive_zero_na_equivalence(
                review, dilemma["component"]
            )
            hard = (
                False
                if zero_na_equivalence
                else any(
                    _hard_difference(model_score, score)
                    for score in ballot_scores
                )
            )
            proposed_score = None
            proposed_na_reason = None
            reason = (
                "ORDER_SENSITIVE_ZERO_NA_EQUIVALENCE"
                if zero_na_equivalence
                else "ORDER_SENSITIVE_WITH_HARD_DIFFERENCE"
                if hard else "ORDER_SENSITIVE_BELOW_HUMAN_THRESHOLD"
            )
        record = {
            "dilemma_id": dilemma_id,
            "document_id": dilemma["document_id"],
            "component": dilemma["component"],
            "model_score": model_score,
            "model_na_reason": dilemma.get("model_na_reason"),
            "second_review": review,
            "policy_version": "principal-escalation-v5-applicability",
            "reason": reason,
        }
        if hard:
            human.append({
                **dilemma["full_dilemma"],
                "principal_review_required": True,
                "principal_review": {
                    "required": True,
                    "policy_version": "principal-escalation-v5-applicability",
                    "reason": reason,
                },
                "second_review": review,
            })
        else:
            auto.append({
                **record,
                "final_score": (
                    proposed_score if review["order_stable"] else model_score
                ),
                "final_na_reason": (
                    proposed_na_reason if review["order_stable"]
                    else dilemma.get("model_na_reason")
                ),
                "resolution": (
                    "STABLE_SECOND_REVIEW_ACCEPTED"
                    if review["order_stable"]
                    else "ORIGINAL_RETAINED_AFTER_ORDER_SENSITIVITY"
                ),
            })
    return {
        "schema_version": "1.0",
        "case_id": debate_packet["case_id"],
        "policy_version": "principal-escalation-v5-applicability",
        "auto_resolutions": auto,
        "human_dilemmas": human,
        "summary": {
            "reviewed_dilemmas": len(debate),
            "auto_resolved": len(auto),
            "human_required": len(human),
        },
    }
