from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping


class AnchorReviewMaterializationError(ValueError):
    """Raised when a model review requires a substantive human decision."""


def materialize_model_reviewed_anchors(
    anchors: list[dict[str, Any]],
    review_records: list[dict[str, Any]],
    *,
    revised_basis_summaries: Mapping[str, str] | None = None,
    reviewed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Apply only non-substantive, auditable model-review revisions.

    APPROVE is materialized directly.  REVISE is accepted automatically only
    when the reviewer preserves the proposed epistemic status and confidence,
    cites a subset of the assessment's existing evidence, and reports no
    critical issue.  This permits temporal cleanup without letting the model
    silently rewrite the research finding.
    """

    by_anchor = {row["anchor_id"]: row for row in review_records}
    expected = {row["anchor_id"] for row in anchors}
    if set(by_anchor) != expected:
        raise AnchorReviewMaterializationError(
            "review records do not cover the exact anchor set"
        )
    timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
    basis_overrides = dict(revised_basis_summaries or {})
    result: list[dict[str, Any]] = []

    for source_anchor in anchors:
        anchor = copy.deepcopy(source_anchor)
        record = by_anchor[anchor["anchor_id"]]
        output = record["model_output"]
        assessment_reviews = {
            row["assessment_id"]: row for row in output["assessment_reviews"]
        }
        expected_assessments = {
            row["assessment_id"] for row in anchor["assessments"]
        }
        if set(assessment_reviews) != expected_assessments:
            raise AnchorReviewMaterializationError(
                f"assessment review coverage mismatch: {anchor['anchor_id']}"
            )
        for assessment in anchor["assessments"]:
            review = assessment_reviews[assessment["assessment_id"]]
            verdict = review["verdict"]
            if verdict not in {"APPROVE", "REVISE"}:
                raise AnchorReviewMaterializationError(
                    f"human review required for {assessment['assessment_id']}: {verdict}"
                )
            if review.get("critical_issue"):
                raise AnchorReviewMaterializationError(
                    f"critical issue for {assessment['assessment_id']}: "
                    f"{review['critical_issue']}"
                )
            if (
                review["recommended_epistemic_status"]
                != assessment["epistemic_status"]
                or review["recommended_confidence"] != assessment["confidence"]
            ):
                raise AnchorReviewMaterializationError(
                    f"substantive revision requires human review: "
                    f"{assessment['assessment_id']}"
                )
            if verdict == "REVISE":
                allowed = {
                    row["source_id"] for row in assessment["evidence_refs"]
                }
                selected = set(review["evidence_source_ids"])
                if not selected or not selected <= allowed:
                    raise AnchorReviewMaterializationError(
                        f"unsafe evidence revision: {assessment['assessment_id']}"
                    )
                assessment["evidence_refs"] = [
                    row
                    for row in assessment["evidence_refs"]
                    if row["source_id"] in selected
                ]
                if assessment["assessment_id"] not in basis_overrides:
                    raise AnchorReviewMaterializationError(
                        f"missing curated basis summary: {assessment['assessment_id']}"
                    )
                assessment["basis_summary"] = basis_overrides[
                    assessment["assessment_id"]
                ]
            assessment["review_status"] = "MODEL_APPROVED"
            assessment["reviewed_by"] = f"vertex:{record['model']}"
            assessment["reviewed_at"] = {
                "value": timestamp,
                "precision": "DATETIME",
            }
            assessment["review_notes"] = (
                f"{verdict} in {record['prompt_version']}; "
                f"review input {record['input_hash']}."
            )
        result.append(anchor)
    return result
