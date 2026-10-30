"""Pure helpers for Aletheia's shadow-mode autonomous appellate review.

The appellate layer never writes a human verdict.  It blinds the origin of the
two arguments, reverses their order in a second ballot, validates grounding,
and exposes only a conservative shadow candidate for later calibration.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aletheia_lite.judgment_room import SCORE_LADDERS


PROTOCOL_VERSION = "case001-autonomous-appellate-review-v1"
VALID_SELECTIONS = {"POSITION_A", "POSITION_B", "OTHER", "ABSTAIN"}
VALID_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}
VALID_SUFFICIENCY = {"INSUFFICIENT", "PARTIAL", "SUFFICIENT"}
VALID_RESIDUAL_RISKS = {
    "ANCHOR_SCOPE_UNRESOLVED",
    "SOURCE_VOICE_UNRESOLVED",
    "COUNTEREVIDENCE_SCOPE_UNRESOLVED",
    "TRANSLATION_UNCERTAINTY",
    "PRIVILEGED_ACCESS_UNRESOLVED",
    "VISUAL_EVIDENCE_UNAVAILABLE",
    "RULEBOOK_AMBIGUITY",
    "OTHER",
}


def _position_payload(
    score: int | None,
    na_reason: str | None,
    rationale: str,
    evidence_quote: str | None,
    anchor_refs: list[str] | None,
) -> dict[str, Any]:
    return {
        "score": score,
        "na_reason": na_reason if score is None else None,
        "rationale": rationale,
        "evidence_quote": evidence_quote,
        "anchor_refs": list(anchor_refs or []),
    }


def _model_position(dilemma: dict[str, Any]) -> dict[str, Any]:
    model = dilemma["model"]
    evidence = model.get("evidence_span") or dilemma.get("decisive_exhibit") or {}
    return _position_payload(
        model.get("score"),
        model.get("na_reason") or (
            "MODEL_NOT_APPLICABLE" if model.get("score") is None else None
        ),
        str(model.get("rationale") or ""),
        evidence.get("quote_original"),
        model.get("anchor_refs"),
    )


def _challenge_position(dilemma: dict[str, Any]) -> dict[str, Any]:
    challenge = dilemma["challenge"]
    evidence = dilemma.get("decisive_exhibit") or {}
    return _position_payload(
        challenge.get("score"),
        challenge.get("na_reason") or (
            "CHALLENGE_NOT_APPLICABLE"
            if challenge.get("score") is None
            else None
        ),
        str(challenge.get("argument_he") or ""),
        evidence.get("quote_original"),
        challenge.get("anchor_refs"),
    )


def first_ballot_model_label(dilemma_id: str) -> str:
    """Return a stable blinded label; never encode the source in the prompt."""
    digest = hashlib.sha256(
        f"{PROTOCOL_VERSION}:{dilemma_id}".encode("utf-8")
    ).digest()
    return "POSITION_A" if digest[0] % 2 == 0 else "POSITION_B"


def build_blind_dilemma(
    dilemma: dict[str, Any], ballot_number: int
) -> tuple[dict[str, Any], dict[str, str]]:
    if ballot_number not in {1, 2}:
        raise ValueError("ballot_number must be 1 or 2")
    model_label = first_ballot_model_label(dilemma["dilemma_id"])
    if ballot_number == 2:
        model_label = (
            "POSITION_B" if model_label == "POSITION_A" else "POSITION_A"
        )
    challenge_label = (
        "POSITION_B" if model_label == "POSITION_A" else "POSITION_A"
    )
    positions = {
        model_label: _model_position(dilemma),
        challenge_label: _challenge_position(dilemma),
    }
    public = {
        "dilemma_id": dilemma["dilemma_id"],
        "component": dilemma["component"],
        "language": dilemma["language"],
        "issue_tags": list(dilemma.get("issue_tags", [])),
        "question_he": dilemma["question_he"],
        "bench_note_he": dilemma["bench_note_he"],
        "score_ladder": [
            {"score": score, "meaning_he": meaning}
            for score, meaning in SCORE_LADDERS[dilemma["component"]]
        ],
        "positions": positions,
    }
    sealed_mapping = {
        model_label: "MODEL_PROPOSAL",
        challenge_label: "METHODOLOGICAL_CHALLENGE",
    }
    return public, sealed_mapping


def _anchor_projection(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        key: anchor.get(key)
        for key in (
            "anchor_id",
            "claim_family",
            "claim",
            "canonical_claim",
            "assessments",
        )
        if key in anchor
    }


def build_document_ballot(
    dilemmas: list[dict[str, Any]], ballot_number: int
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    if not dilemmas:
        raise ValueError("A document ballot requires at least one dilemma")
    document_ids = {item["document_id"] for item in dilemmas}
    if len(document_ids) != 1:
        raise ValueError("A ballot may contain dilemmas from one article only")
    article = dilemmas[0]["article"]
    public_dilemmas = []
    sealed = {}
    anchors: dict[str, dict[str, Any]] = {}
    for dilemma in dilemmas:
        public, mapping = build_blind_dilemma(dilemma, ballot_number)
        public_dilemmas.append(public)
        sealed[dilemma["dilemma_id"]] = mapping
        for anchor in dilemma.get("anchors", []):
            anchors[anchor["anchor_id"]] = _anchor_projection(anchor)
    bundle = {
        "protocol_version": PROTOCOL_VERSION,
        "ballot_number": ballot_number,
        "case_id": dilemmas[0]["case_id"],
        "document_id": dilemmas[0]["document_id"],
        "article": {
            key: article.get(key)
            for key in (
                "source_name",
                "language",
                "title",
                "published_at",
                "sample_phase",
                "text",
                "content_hash",
            )
        },
        "anchors": list(anchors.values()),
        "dilemmas": public_dilemmas,
    }
    return bundle, sealed


def validate_document_ballot(
    bundle: dict[str, Any], raw: dict[str, Any]
) -> dict[str, Any]:
    if raw.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("SECOND_REVIEW_PROTOCOL_VERSION_MISMATCH")
    if raw.get("document_id") != bundle["document_id"]:
        raise ValueError("SECOND_REVIEW_DOCUMENT_MISMATCH")
    expected = {item["dilemma_id"]: item for item in bundle["dilemmas"]}
    decisions = raw.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("SECOND_REVIEW_DECISIONS_MISSING")
    indexed = {row.get("dilemma_id"): row for row in decisions}
    if set(indexed) != set(expected) or len(indexed) != len(decisions):
        raise ValueError("SECOND_REVIEW_DILEMMA_KEYS_MISMATCH")
    article_text = "\n".join(
        str(bundle["article"].get(field) or "") for field in ("title", "text")
    )
    known_anchors = {
        anchor["anchor_id"] for anchor in bundle.get("anchors", [])
    }
    normalized = []
    for dilemma_id, specification in expected.items():
        row = dict(indexed[dilemma_id])
        selection = row.get("selection")
        score = row.get("proposed_score")
        na_reason = row.get("proposed_na_reason")
        confidence = row.get("confidence")
        sufficiency = row.get("evidence_sufficiency")
        risks = row.get("residual_risks")
        addressed = row.get("addressed_issue_tags")
        quote = row.get("exact_quote_used")
        refs = row.get("anchor_refs")
        if selection not in VALID_SELECTIONS:
            raise ValueError(f"INVALID_SECOND_REVIEW_SELECTION:{dilemma_id}")
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"INVALID_SECOND_REVIEW_CONFIDENCE:{dilemma_id}")
        if sufficiency not in VALID_SUFFICIENCY:
            raise ValueError(f"INVALID_SECOND_REVIEW_SUFFICIENCY:{dilemma_id}")
        if not isinstance(risks, list) or not set(risks) <= VALID_RESIDUAL_RISKS:
            raise ValueError(f"INVALID_SECOND_REVIEW_RISKS:{dilemma_id}")
        if not isinstance(addressed, list) or not set(addressed) <= set(
            specification["issue_tags"]
        ):
            raise ValueError(f"INVALID_ADDRESSED_ISSUES:{dilemma_id}")
        if not isinstance(refs, list) or not set(refs) <= known_anchors:
            raise ValueError(f"UNKNOWN_SECOND_REVIEW_ANCHOR:{dilemma_id}")
        if quote is not None and (
            not isinstance(quote, str) or not quote.strip() or quote not in article_text
        ):
            raise ValueError(f"SECOND_REVIEW_QUOTE_NOT_VERBATIM:{dilemma_id}")
        if selection == "ABSTAIN":
            if score is not None or na_reason is not None or confidence != "LOW":
                raise ValueError(f"INVALID_SECOND_REVIEW_ABSTENTION:{dilemma_id}")
        else:
            if score is None:
                if not isinstance(na_reason, str) or not na_reason.strip():
                    raise ValueError(f"SECOND_REVIEW_NA_REASON_MISSING:{dilemma_id}")
            elif not isinstance(score, int) or not 0 <= score <= 4 or na_reason is not None:
                raise ValueError(f"INVALID_SECOND_REVIEW_SCORE:{dilemma_id}")
            if selection in {"POSITION_A", "POSITION_B"}:
                chosen = specification["positions"][selection]
                if score != chosen["score"]:
                    raise ValueError(f"SECOND_REVIEW_POSITION_SCORE_MISMATCH:{dilemma_id}")
        if score is not None and quote is None:
            raise ValueError(f"SECOND_REVIEW_NUMERIC_SCORE_NEEDS_QUOTE:{dilemma_id}")
        rationale = row.get("rationale_he")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"SECOND_REVIEW_RATIONALE_MISSING:{dilemma_id}")
        row["all_issue_tags"] = list(specification["issue_tags"])
        normalized.append(row)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "document_id": bundle["document_id"],
        "ballot_number": bundle["ballot_number"],
        "decisions": normalized,
    }


def _decision_value(row: dict[str, Any]) -> tuple[int | None, str | None]:
    return row.get("proposed_score"), row.get("proposed_na_reason")


def reconcile_ballots(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    if first["document_id"] != second["document_id"]:
        raise ValueError("SECOND_REVIEW_BALLOT_DOCUMENT_MISMATCH")
    first_index = {row["dilemma_id"]: row for row in first["decisions"]}
    second_index = {row["dilemma_id"]: row for row in second["decisions"]}
    if set(first_index) != set(second_index):
        raise ValueError("SECOND_REVIEW_BALLOT_KEYS_MISMATCH")
    reconciled = []
    for dilemma_id in first_index:
        left = first_index[dilemma_id]
        right = second_index[dilemma_id]
        value_agrees = _decision_value(left) == _decision_value(right)
        both_choose_position = all(
            row.get("selection") in {"POSITION_A", "POSITION_B"}
            for row in (left, right)
        )
        all_issues_addressed = set(left.get("addressed_issue_tags", [])) == set(
            left.get("all_issue_tags", left.get("addressed_issue_tags", []))
        ) and set(right.get("addressed_issue_tags", [])) == set(
            right.get("all_issue_tags", right.get("addressed_issue_tags", []))
        )
        shadow_auto_candidate = (
            value_agrees
            and both_choose_position
            and all(row.get("confidence") == "HIGH" for row in (left, right))
            and all(
                row.get("evidence_sufficiency") == "SUFFICIENT"
                for row in (left, right)
            )
            and all(not row.get("residual_risks") for row in (left, right))
            and all_issues_addressed
            and left.get("proposed_score") is not None
        )
        reconciled.append(
            {
                "dilemma_id": dilemma_id,
                "order_stable": value_agrees,
                "proposed_score": left.get("proposed_score") if value_agrees else None,
                "proposed_na_reason": (
                    left.get("proposed_na_reason") if value_agrees else None
                ),
                "shadow_auto_candidate": shadow_auto_candidate,
                "requires_human": not shadow_auto_candidate,
                "ballot_1": left,
                "ballot_2": right,
            }
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "document_id": first["document_id"],
        "decisions": reconciled,
    }


def promotion_gate(metrics: dict[str, Any]) -> dict[str, Any]:
    """Conservative empirical gate; passing it never mutates current verdicts."""
    candidate_count = int(metrics.get("candidate_count", 0))
    exact = float(metrics.get("exact_agreement", 0.0))
    adjacent = float(metrics.get("within_one_agreement", 0.0))
    severe = int(metrics.get("severe_errors", 0))
    numeric_na = int(metrics.get("numeric_na_errors", 0))
    language_minimum = float(metrics.get("minimum_language_exact_agreement", 0.0))
    passed = (
        candidate_count >= 10
        and exact >= 0.90
        and adjacent >= 0.98
        and severe == 0
        and numeric_na == 0
        and language_minimum >= 0.80
    )
    return {
        "status": "ELIGIBLE_FOR_HUMAN_PROMOTION_DECISION" if passed else "SHADOW_ONLY",
        "passed": passed,
        "thresholds": {
            "minimum_candidates": 10,
            "minimum_exact_agreement": 0.90,
            "minimum_within_one_agreement": 0.98,
            "maximum_severe_errors": 0,
            "maximum_numeric_na_errors": 0,
            "minimum_language_exact_agreement": 0.80,
        },
    }
