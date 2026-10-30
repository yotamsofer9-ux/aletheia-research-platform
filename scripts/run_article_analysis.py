#!/usr/bin/env python3
"""Run bounded, resumable article measurements for one approved case."""

from __future__ import annotations

import argparse
import csv
import copy
import difflib
import hashlib
import json
import os
import platform
import re
import resource
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import types
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "exports" / "case001_research_pilot_27.csv"
ROSTER_PATH = ROOT / "calibration" / "case001_calibration_roster_v1.json"
ANCHORS_PATH = ROOT / "anchors" / "case001_factual_anchors_v1.jsonl"
SCHEMA_PATH = ROOT / "measurement" / "article_measurement_schema.json"
CODEBOOK_PATH = ROOT / "CODEBOOK_DRAFT.md"
SYSTEM_RULEBOOK_PATH = ROOT / "measurement" / "SYSTEM_RULEBOOK.md"
ADAPTER_DIR = ROOT / "measurement" / "language_adapters"
BUDGET_PATH = ROOT / "analysis" / "budgets_v1.json"
DEFAULT_OUTPUT = ROOT / "analysis" / "runs" / "case001_article_measurements_v1.jsonl"
DEFAULT_SUMMARY = ROOT / "analysis" / "runs" / "case001_article_measurements_v1_summary.json"
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL = "gemini-2.5-flash"
PROMPT_VERSION = "case001-article-analysis-v1.0.12"
CASE_ID = "CASE-001"
CASE_SCOPE: dict[str, Any] | None = None

PUBLIC_REPORT_ONLY_ANCHORS = {
    "CASE001-CASUALTY-001",
    "CASE001-CASUALTY-002",
    "CASE001-CASUALTY-004",
    "CASE001-EVIDENCE-002",
    "CASE001-EVIDENCE-005",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSpan(StrictModel):
    span_id: str = Field(min_length=1, max_length=40)
    quote_original: str = Field(min_length=1, max_length=500)
    location: Literal["HEADLINE", "LEAD", "BODY"]
    reviewer_translation_he: None


class RelevantClaim(StrictModel):
    claim_id: str = Field(min_length=1, max_length=40)
    quote_original: str = Field(min_length=1, max_length=500)
    location: Literal["HEADLINE", "LEAD", "BODY"]
    voice: Literal[
        "OUTLET_ASSERTION",
        "ATTRIBUTED_CLAIM",
        "DIRECT_QUOTE",
        "DENIAL",
        "HEDGED_INFERENCE",
    ]
    speaker: str | None = Field(max_length=200)
    centrality: Literal["CENTRAL", "CONTEXT"]
    claim_kind: Literal[
        "CAUSE_OR_RESPONSIBILITY",
        "CASUALTY_OR_IMPACT",
        "EVIDENCE_OR_PROVENANCE",
        "OTHER",
    ]
    outlet_stance: Literal["ADOPTS", "REPORTS_ONLY", "CONTESTS", "UNCLEAR"]
    anchor_ids: list[str] = Field(max_length=8)
    stance_toward_anchors: Literal[
        "ALIGNS", "CONFLICTS", "MIXED", "NOT_ASSESSABLE"
    ]
    certainty: Literal["ASSERTED", "ATTRIBUTED", "HEDGED", "DISPUTED", "UNKNOWN"]
    evidence_access: Literal[
        "PUBLIC_BASIS",
        "DOCUMENTED_PRIVILEGED_BASIS",
        "ACCESS_NOT_DOCUMENTED",
    ]
    reviewer_translation_he: None


ReviewReason = Literal[
    "ANCHOR_AMBIGUITY",
    "TRANSLATION_UNCERTAINTY",
    "SOURCE_VOICE_AMBIGUITY",
    "EVIDENCE_SPAN_NOT_FOUND",
    "LOW_CONFIDENCE",
    "CONFLICTING_PASSAGES",
    "PRIVILEGED_ACCESS_LIMITATION",
    "OTHER",
]


class ComponentMeasurement(StrictModel):
    score: int | None = Field(default=None, ge=0, le=4)
    na_reason: Literal[
        "NO_RELEVANT_CLAIM",
        "NO_ACTIVE_ANCHOR_FOR_CENTRAL_CLAIM",
        "NO_RELEVANT_COUNTEREVIDENCE",
        "HEADLINE_OR_LEAD_UNAVAILABLE",
        "NOT_APPLICABLE_TO_TEXT_TYPE",
        "OTHER",
    ] | None
    confidence: Literal["LOW", "MEDIUM", "HIGH"] | None
    rationale: str = Field(min_length=1, max_length=320)
    evidence_spans: list[EvidenceSpan] = Field(max_length=1)
    anchor_refs: list[str] = Field(max_length=8)
    needs_review_reasons: list[ReviewReason] = Field(max_length=5)

    @model_validator(mode="after")
    def validate_na_or_score(self):
        if self.score is None:
            if self.na_reason is None or self.confidence is not None or self.evidence_spans:
                raise ValueError("N/A requires a reason, null confidence, and no evidence spans")
        elif self.na_reason is not None or self.confidence is None or not self.evidence_spans:
            raise ValueError("a score requires confidence and evidence, with null na_reason")
        return self


class Components(StrictModel):
    T1: ComponentMeasurement
    T2: ComponentMeasurement
    T3: ComponentMeasurement
    N1: ComponentMeasurement
    N2: ComponentMeasurement
    N3: ComponentMeasurement
    N4: ComponentMeasurement
    N5: ComponentMeasurement


class TrajectoryComponent(StrictModel):
    code: Literal["T4"]
    state: Literal["PENDING_SEPARATE_TRAJECTORY_ANALYSIS"]
    reason: Literal[
        "T4 is scored across a three-phase publisher slot, never from one article."
    ]


class NeedsReview(StrictModel):
    required: bool
    reasons: list[ReviewReason] = Field(max_length=5)
    note: str | None


class ArticleMeasurement(StrictModel):
    schema_version: Literal["1.0.7"]
    prompt_version: str = Field(min_length=1)
    case_id: str = Field(pattern=r"^CASE-[0-9]{3}$")
    document_id: str = Field(min_length=1)
    language: Literal["he", "en", "ar"]
    analysis_status: Literal["COMPLETE", "NEEDS_REVIEW"]
    dominant_frame: Literal[
        "HUMANITARIAN",
        "SECURITY",
        "POLITICAL",
        "INVESTIGATIVE",
        "MEDIA_ACCOUNTABILITY",
        "MIXED",
        "OTHER",
    ]
    secondary_frames: list[
        Literal[
            "HUMANITARIAN",
            "SECURITY",
            "POLITICAL",
            "INVESTIGATIVE",
            "MEDIA_ACCOUNTABILITY",
            "MIXED",
            "OTHER",
        ]
    ] = Field(max_length=2)
    relevant_claims: list[RelevantClaim] = Field(max_length=4)
    components: Components
    trajectory_component: TrajectoryComponent
    needs_review: NeedsReview


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def case_scope_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Expose only the approved event definition needed to keep coding on-topic."""
    required = ("case_id", "title", "research_purpose", "keywords")
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise ValueError(f"case manifest lacks analysis scope fields: {missing}")
    keywords = manifest["keywords"]
    if not isinstance(keywords, dict) or any(
        not isinstance(keywords.get(language), list)
        for language in ("he", "en", "ar")
    ):
        raise ValueError("case manifest keywords must contain he, en and ar lists")
    return {
        "case_id": str(manifest["case_id"]),
        "title": str(manifest["title"]),
        "research_purpose": str(manifest["research_purpose"]),
        "event_expressions": {
            language: [str(value) for value in keywords[language]]
            for language in ("he", "en", "ar")
        },
    }


def active_measurement_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text())
    schema["properties"]["prompt_version"] = {"const": PROMPT_VERSION}
    schema["properties"]["case_id"] = {"const": CASE_ID}
    return schema


def active_measurement_schema_hash() -> str:
    stored = json.loads(SCHEMA_PATH.read_text())
    if (
        stored["properties"]["prompt_version"].get("const") == PROMPT_VERSION
        and stored["properties"]["case_id"].get("const") == CASE_ID
    ):
        return file_hash(SCHEMA_PATH)
    return stable_hash(active_measurement_schema())


def active_language_adapter(path: Path) -> dict[str, Any]:
    adapter = json.loads(path.read_text())
    adapter["shared_prompt_version"] = PROMPT_VERSION
    return adapter


def active_language_adapter_hash(path: Path) -> str:
    stored = json.loads(path.read_text())
    if stored.get("shared_prompt_version") == PROMPT_VERSION:
        return file_hash(path)
    return stable_hash(active_language_adapter(path))


def current_rss_mb() -> float | None:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True, timeout=2
        )
        return round(int(output.strip()) / 1024, 2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def peak_rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if platform.system() == "Darwin":
        value /= 1024 * 1024
    else:
        value /= 1024
    return round(value, 2)


def load_corpus() -> dict[str, dict[str, str]]:
    with CORPUS_PATH.open(encoding="utf-8-sig") as handle:
        return {row["document_id"]: row for row in csv.DictReader(handle)}


def parse_publication_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def temporal_value(value: dict[str, str] | None) -> date | None:
    return date.fromisoformat(value["value"][:10]) if value else None


def anchor_bundle_for(publication_date: date) -> list[dict[str, Any]]:
    result = []
    for anchor in load_jsonl(ANCHORS_PATH):
        active = None
        for assessment in anchor["assessments"]:
            starts = temporal_value(assessment["as_of"])
            ends = temporal_value(assessment.get("valid_until"))
            if starts <= publication_date and (ends is None or publication_date <= ends):
                if active is None or temporal_value(active["as_of"]) < starts:
                    active = assessment
        reduced = None
        if active is not None:
            reduced = {
                "assessment_id": active["assessment_id"],
                "as_of": active["as_of"],
                "valid_until": active.get("valid_until"),
                "epistemic_status": active["epistemic_status"],
                "confidence": active["confidence"],
                "basis_summary": active["basis_summary"],
            }
        if reduced is not None:
            result.append(
                {
                    "anchor_id": anchor["anchor_id"],
                    "family": anchor["family"],
                    "proposition_type": anchor["proposition_type"],
                    "evaluation_scope": (
                        "PUBLIC_REPORT_EXISTENCE_ONLY"
                        if (
                            anchor["anchor_id"] in PUBLIC_REPORT_ONLY_ANCHORS
                            or anchor.get("proposition_type")
                            in {"REPORTED_ESTIMATE", "EVIDENCE_EXISTENCE"}
                        )
                        else "UNDERLYING_PROPOSITION"
                    ),
                    "canonical_proposition": anchor["canonical_proposition"],
                    "scope_notes": anchor["scope_notes"],
                    "assessment_available_at_publication": reduced,
                }
            )
    return result


def build_bundle(document: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], str]:
    budgets = json.loads(BUDGET_PATH.read_text())
    if len(document["text"]) > budgets["max_document_chars"]:
        raise ValueError("DOCUMENT_CHAR_BUDGET_EXCEEDED")
    language = document["language"]
    adapter_path = ADAPTER_DIR / f"{language}.json"
    adapter = active_language_adapter(adapter_path)
    bundle = {
        "case_id": CASE_ID,
        "document": {
            key: document[key]
            for key in (
                "document_id",
                "source_id",
                "source_name",
                "group_name",
                "language",
                "publisher_slot",
                "title",
                "published_at",
                "sample_phase",
                "content_hash",
                "text",
            )
        },
        "time_indexed_anchors": anchor_bundle_for(
            parse_publication_date(document["published_at"])
        ),
        "language_adapter": {
            "adapter_version": adapter["adapter_version"],
            "language": adapter["language"],
        },
    }
    if CASE_SCOPE is not None:
        bundle["case_scope"] = CASE_SCOPE
    provenance = {
        "document_content_hash": document["content_hash"],
        "anchor_version_hash": file_hash(ANCHORS_PATH),
        "measurement_schema_hash": active_measurement_schema_hash(),
        "codebook_hash": file_hash(CODEBOOK_PATH),
        "system_rulebook_hash": file_hash(SYSTEM_RULEBOOK_PATH),
        "adapter_hash": active_language_adapter_hash(adapter_path),
        "budget_version": budgets["budget_version"],
    }
    if CASE_SCOPE is not None:
        provenance["case_scope_hash"] = stable_hash(CASE_SCOPE)
    return bundle, provenance, adapter_path.name


def build_prompt(bundle: dict[str, Any]) -> str:
    return (
        "FROZEN ANALYSIS BUNDLE:\n"
        + json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        + "\n\nOUTPUT JSON SCHEMA:\n"
        + json.dumps(active_measurement_schema(), ensure_ascii=False)
    )


def anchor_matches_claim_kind(anchor_family: str | None, claim_kind: str) -> bool:
    compatible_families = {
        "CAUSE_OR_RESPONSIBILITY": {"CAUSE_AND_MECHANISM", "ACTOR_RESPONSIBILITY"},
        "CASUALTY_OR_IMPACT": {"CASUALTY_ESTIMATE", "IMPACT_AND_DAMAGE"},
        "EVIDENCE_OR_PROVENANCE": {"EVIDENCE_AND_PROVENANCE"},
    }
    if claim_kind == "OTHER":
        return anchor_family is not None
    return anchor_family in compatible_families.get(claim_kind, set())


def validate_measurement(
    result: ArticleMeasurement,
    raw: dict[str, Any],
    document: dict[str, str],
    known_anchor_families: dict[str, str],
) -> list[str]:
    errors = [
        error.message
        for error in Draft202012Validator(active_measurement_schema()).iter_errors(raw)
    ]
    if result.document_id != document["document_id"]:
        errors.append("document_id does not match input")
    if result.language != document["language"]:
        errors.append("language does not match input")

    searchable = document["title"] + "\n" + document["text"]
    quotes = [claim.quote_original for claim in result.relevant_claims]
    anchor_refs: list[str] = [
        anchor_id for claim in result.relevant_claims for anchor_id in claim.anchor_ids
    ]
    components = result.components.model_dump().values()
    for component in components:
        quotes.extend(span["quote_original"] for span in component["evidence_spans"])
        anchor_refs.extend(component["anchor_refs"])
    if result.components.T1.score is not None and not result.components.T1.anchor_refs:
        errors.append("scored T1 requires at least one active anchor reference")
    t1_matches_central_eligible_claim = any(
        claim.centrality == "CENTRAL"
        and claim.outlet_stance in {"ADOPTS", "REPORTS_ONLY", "CONTESTS"}
        and any(
            anchor_id in result.components.T1.anchor_refs
            and anchor_matches_claim_kind(
                known_anchor_families.get(anchor_id), claim.claim_kind
            )
            for anchor_id in claim.anchor_ids
        )
        for claim in result.relevant_claims
    )
    if result.components.T1.score is not None and not t1_matches_central_eligible_claim:
        errors.append("scored T1 anchor must match a central eligible claim")
    for quote in quotes:
        if quote not in searchable:
            errors.append(f"evidence span not found verbatim: {quote[:120]!r}")
    unknown = sorted(set(anchor_refs) - set(known_anchor_families))
    if unknown:
        errors.append(f"unknown anchor references: {unknown}")

    low_confidence = any(
        component["confidence"] == "LOW" for component in components
    )
    if low_confidence and (
        not result.needs_review.required
        or "LOW_CONFIDENCE" not in result.needs_review.reasons
    ):
        errors.append("LOW confidence requires global LOW_CONFIDENCE review routing")
    local_review_reasons = {
        reason
        for component in components
        for reason in component["needs_review_reasons"]
    }
    missing_global_reasons = sorted(
        local_review_reasons - set(result.needs_review.reasons)
    )
    if missing_global_reasons:
        errors.append(
            "component review reasons missing from global routing: "
            f"{missing_global_reasons}"
        )
    if (
        local_review_reasons or result.needs_review.reasons
    ) and not result.needs_review.required:
        errors.append("review reasons require global human review")
    if result.needs_review.required != (result.analysis_status == "NEEDS_REVIEW"):
        errors.append("analysis_status and needs_review.required disagree")
    return errors


def resolve_verbatim_spans(
    raw: dict[str, Any], document: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Conservatively shorten near-exact model quotes to exact source spans."""
    corrected = copy.deepcopy(raw)
    searchable = document["title"] + "\n" + document["text"]
    log: list[dict[str, Any]] = []
    targets: list[tuple[str, dict[str, Any]]] = []
    for index, claim in enumerate(corrected.get("relevant_claims", [])):
        targets.append((f"relevant_claims[{index}]", claim))
    for code, component in corrected.get("components", {}).items():
        for index, span in enumerate(component.get("evidence_spans", [])):
            targets.append((f"components.{code}.evidence_spans[{index}]", span))

    for location, target in targets:
        quote = target.get("quote_original")
        if not isinstance(quote, str) or quote in searchable:
            continue
        whitespace_pattern = r"\s+".join(re.escape(part) for part in quote.split())
        whitespace_match = re.search(whitespace_pattern, searchable)
        if whitespace_match:
            resolved = whitespace_match.group(0)
            target["quote_original"] = resolved
            log.append(
                {
                    "field": location,
                    "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": 1.0,
                    "method": "WHITESPACE_ONLY_SOURCE_RESOLUTION",
                    "requires_review": False,
                }
            )
            continue

        ellipsis_parts = [
            part.strip() for part in re.split(r"\.{3}|…", quote) if part.strip()
        ]
        exact_parts = [part for part in ellipsis_parts if len(part) >= 40 and part in searchable]
        if exact_parts:
            resolved = max(exact_parts, key=len)
            target["quote_original"] = resolved
            log.append(
                {
                    "field": location,
                    "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": round(len(resolved) / len(quote), 4),
                    "method": "EXACT_SEGMENT_FROM_EXPLICIT_ELLIPSIS",
                    "requires_review": False,
                }
            )
            continue

        # A short terminology inventory (common in N5 explanations) may list
        # several exact phrases separated by ellipses.  When at least three
        # phrases are independently present, retain the longest exact phrase;
        # never treat the synthetic inventory as one source quotation.
        exact_inventory_parts = [
            part for part in ellipsis_parts if len(part) >= 12 and part in searchable
        ]
        if len(ellipsis_parts) >= 3 and len(exact_inventory_parts) >= 3:
            resolved = max(exact_inventory_parts, key=len)
            target["quote_original"] = resolved
            log.append(
                {
                    "field": location,
                    "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": round(len(resolved) / len(quote), 4),
                    "method": "EXACT_TERM_FROM_ELLIPSIS_INVENTORY",
                    "requires_review": False,
                }
            )
            continue

        # Live-blog and news-digest pages often separate independently valid
        # source snippets with a bullet.  A model may preserve two exact
        # snippets while omitting intervening bullets, producing a combined
        # string that is not itself verbatim.  Retain one complete, exact
        # bullet segment rather than accepting the synthetic concatenation.
        bullet_parts = [
            part.strip()
            for part in re.split(r"\s*[●•]\s*", quote)
            if part.strip()
        ]
        exact_bullet_parts = [
            part for part in bullet_parts if len(part) >= 40 and part in searchable
        ]
        if exact_bullet_parts:
            resolved = max(exact_bullet_parts, key=len)
            target["quote_original"] = resolved
            log.append(
                {
                    "field": location,
                    "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": round(len(resolved) / len(quote), 4),
                    "method": "EXACT_SEGMENT_FROM_BULLET_DIGEST",
                    "requires_review": False,
                }
            )
            continue

        # Models occasionally compress a provenance list into several exact
        # source fragments separated by ASCII commas.  Accepting the joined
        # list would fabricate contiguity, so keep only the longest complete
        # fragment when at least two list items independently occur verbatim.
        citation_parts = [part.strip() for part in quote.split(",") if part.strip()]
        exact_citation_parts = [part for part in citation_parts if part in searchable]
        if (
            len(citation_parts) >= 3
            and len(exact_citation_parts) >= 3
            and max(map(len, citation_parts), default=0) <= 60
            and max(map(len, exact_citation_parts), default=0) >= 40
        ):
            resolved = max(exact_citation_parts, key=len)
            target["quote_original"] = resolved
            log.append(
                {
                    "field": location,
                    "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": round(len(resolved) / len(quote), 4),
                    "method": "EXACT_SEGMENT_FROM_COMMA_SEPARATED_CITATION_LIST",
                    "requires_review": False,
                }
            )
            continue

        match = difflib.SequenceMatcher(
            None, quote, searchable, autojunk=False
        ).find_longest_match(0, len(quote), 0, len(searchable))
        high_confidence_required = max(40, int(len(quote) * 0.9))
        fallback_required = 80
        if match.size < min(high_confidence_required, fallback_required):
            continue
        resolved = searchable[match.b : match.b + match.size].strip()
        if len(resolved) < 40:
            continue
        # A fuzzy longest-substring repair proves only that a long exact span
        # exists somewhere in the source. It does not prove that the model's
        # original quotation was contiguous. Under the published method, that
        # ambiguity is substantive and must reach review even when similarity
        # exceeds 90 percent. Only the explicit, structure-aware repairs above
        # (whitespace, ellipsis, bullet, terminology and citation lists) are
        # safe to accept automatically.
        requires_review = True
        target["quote_original"] = resolved
        log.append(
            {
                "field": location,
                "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                "original_chars": len(quote),
                "resolved_chars": len(resolved),
                "matched_ratio": round(match.size / len(quote), 4),
                "method": "LONGEST_EXACT_SOURCE_SUBSTRING_REQUIRES_REVIEW",
                "requires_review": requires_review,
            }
        )

    # A model may return an exact multi-paragraph passage that exceeds the
    # schema's 500-character evidence limit. Shorten only at an exact source
    # boundary; this changes no wording and requires no additional model call.
    for location, target in targets:
        quote = target.get("quote_original")
        if not isinstance(quote, str) or len(quote) <= 500 or quote not in searchable:
            continue
        prefix = quote[:500]
        boundaries = [
            prefix.rfind(marker) + len(marker.rstrip())
            for marker in (". ", ".\n", "? ", "?\n", "! ", "!\n")
            if prefix.rfind(marker) >= 200
        ]
        shortened = prefix[:max(boundaries)].rstrip() if boundaries else prefix.rstrip()
        if len(shortened) < 200 or shortened not in searchable:
            shortened = prefix.rstrip()
        target["quote_original"] = shortened
        log.append(
            {
                "field": location,
                "original_quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
                "original_chars": len(quote),
                "resolved_chars": len(shortened),
                "matched_ratio": round(len(shortened) / len(quote), 4),
                "method": "EXACT_SOURCE_SCHEMA_BOUNDARY_TRIM",
                "requires_review": False,
            }
        )
    if any(item["requires_review"] for item in log):
        review = corrected.get("needs_review")
        if isinstance(review, dict):
            review["required"] = True
            reasons = review.setdefault("reasons", [])
            if "EVIDENCE_SPAN_NOT_FOUND" not in reasons:
                reasons.append("EVIDENCE_SPAN_NOT_FOUND")
            review["note"] = (
                "At least one model-selected quotation was reduced to a long exact "
                "contiguous source substring after the model omitted intervening text."
            )
            corrected["analysis_status"] = "NEEDS_REVIEW"
    return corrected, log


def normalize_fixed_metadata(
    raw: dict[str, Any], document: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Inject non-analytic identity fields from frozen runner inputs."""
    corrected = copy.deepcopy(raw)
    expected = {
        "schema_version": "1.0.7",
        "prompt_version": PROMPT_VERSION,
        "case_id": CASE_ID,
        "document_id": document["document_id"],
        "language": document["language"],
    }
    log = []
    for field, value in expected.items():
        if corrected.get(field) != value:
            log.append(
                {
                    "field": field,
                    "model_value": corrected.get(field),
                    "frozen_input_value": value,
                    "method": "INJECT_FROZEN_RUNNER_METADATA",
                }
            )
            corrected[field] = value
    return corrected, log


def normalize_component_rationale_lengths(
    raw: dict[str, Any], maximum_chars: int = 320
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Shorten overlong analytic rationales without altering their wording."""
    corrected = copy.deepcopy(raw)
    log: list[dict[str, Any]] = []
    for code, component in corrected.get("components", {}).items():
        rationale = component.get("rationale")
        if not isinstance(rationale, str) or len(rationale) <= maximum_chars:
            continue
        prefix = rationale[:maximum_chars]
        sentence_end = max(prefix.rfind(". "), prefix.rfind("? "), prefix.rfind("! "))
        if sentence_end >= maximum_chars // 2:
            shortened = prefix[: sentence_end + 1]
        else:
            word_end = prefix.rfind(" ")
            shortened = prefix[:word_end] if word_end >= maximum_chars // 2 else prefix
        shortened = shortened.rstrip()
        component["rationale"] = shortened
        log.append(
            {
                "field": f"components.{code}.rationale",
                "original_chars": len(rationale),
                "normalized_chars": len(shortened),
                "original_sha256": hashlib.sha256(rationale.encode()).hexdigest(),
                "method": "EXACT_PREFIX_AT_SENTENCE_OR_WORD_BOUNDARY",
            }
        )
    return corrected, log


def normalize_reviewer_aids(
    raw: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Drop optional model translations without changing source evidence."""
    corrected = copy.deepcopy(raw)
    log: list[dict[str, Any]] = []
    targets: list[tuple[str, dict[str, Any]]] = []
    for index, claim in enumerate(corrected.get("relevant_claims", [])):
        targets.append((f"relevant_claims[{index}].reviewer_translation_he", claim))
    for code, component in corrected.get("components", {}).items():
        for index, span in enumerate(component.get("evidence_spans", [])):
            targets.append(
                (
                    f"components.{code}.evidence_spans[{index}].reviewer_translation_he",
                    span,
                )
            )
    for field, target in targets:
        translation = target.get("reviewer_translation_he")
        if isinstance(translation, str):
            target["reviewer_translation_he"] = None
            log.append(
                {
                    "field": field,
                    "original_chars": len(translation),
                    "method": "DROP_OPTIONAL_MODEL_TRANSLATION",
                }
            )
    return corrected, log


def normalize_component_contract_conflicts(
    raw: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Recover a scored component with contradictory N/A metadata without a paid call.

    The recovery never invents source wording. When the model omitted the required
    component exhibit, it reuses an exact claim passage already selected by the
    same response and forces focused human review of that component.
    """
    corrected = copy.deepcopy(raw)
    log: list[dict[str, Any]] = []
    claims = corrected.get("relevant_claims", [])
    donor = next(
        (
            claim for claim in claims
            if claim.get("centrality") == "CENTRAL"
            and isinstance(claim.get("quote_original"), str)
            and claim["quote_original"].strip()
        ),
        None,
    )
    if donor is None:
        donor = next(
            (
                claim for claim in claims
                if isinstance(claim.get("quote_original"), str)
                and claim["quote_original"].strip()
            ),
            None,
        )
    recovered_components: list[str] = []
    for code, component in corrected.get("components", {}).items():
        if not isinstance(component, dict) or not isinstance(component.get("score"), int):
            continue
        evidence_spans = component.get("evidence_spans")
        if isinstance(evidence_spans, list) and len(evidence_spans) > 1:
            component["evidence_spans"] = evidence_spans[:1]
            log.append(
                {
                    "field": f"components.{code}.evidence_spans",
                    "model_value_count": len(evidence_spans),
                    "runner_value_count": 1,
                    "method": "KEEP_FIRST_MODEL_SELECTED_EXHIBIT_PER_SCHEMA",
                }
            )
        if component.get("na_reason") is not None:
            log.append(
                {
                    "field": f"components.{code}.na_reason",
                    "model_value": component["na_reason"],
                    "runner_value": None,
                    "method": "NUMERIC_SCORE_TAKES_PRECEDENCE_OVER_NA_METADATA",
                }
            )
            component["na_reason"] = None
        if component.get("evidence_spans") or donor is None:
            continue
        component["evidence_spans"] = [
            {
                "span_id": f"{code}_recovered_claim_1",
                "quote_original": donor["quote_original"],
                "location": donor["location"],
                "reviewer_translation_he": None,
            }
        ]
        reasons = component.setdefault("needs_review_reasons", [])
        if "EVIDENCE_SPAN_NOT_FOUND" not in reasons:
            reasons.append("EVIDENCE_SPAN_NOT_FOUND")
        recovered_components.append(code)
        log.append(
            {
                "field": f"components.{code}.evidence_spans",
                "donor_claim_id": donor.get("claim_id"),
                "method": "REUSE_MODEL_SELECTED_CLAIM_AND_FORCE_HUMAN_REVIEW",
            }
        )
    if recovered_components:
        review = corrected.setdefault(
            "needs_review", {"required": False, "reasons": [], "note": None}
        )
        review["required"] = True
        reasons = review.setdefault("reasons", [])
        if "EVIDENCE_SPAN_NOT_FOUND" not in reasons:
            reasons.append("EVIDENCE_SPAN_NOT_FOUND")
        note = (
            "Deterministic local recovery reused an exact model-selected claim "
            "because required component evidence was absent; the affected "
            f"components require human review: {', '.join(recovered_components)}."
        )
        review["note"] = f"{review.get('note') or ''} {note}".strip()
        corrected["analysis_status"] = "NEEDS_REVIEW"
    return corrected, log


def enforce_structural_review_gates(
    raw: dict[str, Any], known_anchor_families: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply non-interpretive eligibility and human-review gates."""
    corrected = copy.deepcopy(raw)
    log: list[dict[str, Any]] = []
    review = corrected.setdefault(
        "needs_review", {"required": False, "reasons": [], "note": None}
    )
    reasons = review.setdefault("reasons", [])
    notes: list[str] = []

    def route(reason: ReviewReason, note: str, field: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        review["required"] = True
        corrected["analysis_status"] = "NEEDS_REVIEW"
        notes.append(note)
        log.append({"field": field, "reason": reason, "method": "ROUTE_HUMAN_REVIEW"})

    claims = corrected.get("relevant_claims", [])
    central_causal = [
        claim
        for claim in claims
        if claim.get("centrality") == "CENTRAL"
        and claim.get("claim_kind") == "CAUSE_OR_RESPONSIBILITY"
    ]
    central_adopted_causal = [
        claim for claim in central_causal if claim.get("outlet_stance") == "ADOPTS"
    ]

    def has_matching_anchor(candidate_claims: list[dict[str, Any]]) -> bool:
        return any(
            any(
                anchor_id in known_anchor_families
                and anchor_matches_claim_kind(
                    known_anchor_families[anchor_id], claim["claim_kind"]
                )
                for anchor_id in claim.get("anchor_ids", [])
            )
            for claim in candidate_claims
        )

    no_eligible_central_causal_anchor = not has_matching_anchor(central_causal)
    adopted_causal_without_anchor = (
        bool(central_adopted_causal)
        and not has_matching_anchor(central_adopted_causal)
    )
    if central_causal and (
        no_eligible_central_causal_anchor or adopted_causal_without_anchor
    ):
        t1 = corrected.get("components", {}).get("T1")
        if isinstance(t1, dict):
            previous_score = t1.get("score")
            t1.update(
                {
                    "score": None,
                    "na_reason": "NO_ACTIVE_ANCHOR_FOR_CENTRAL_CLAIM",
                    "confidence": None,
                    "evidence_spans": [],
                    "anchor_refs": [],
                }
            )
            component_reasons = t1.setdefault("needs_review_reasons", [])
            if "ANCHOR_AMBIGUITY" not in component_reasons:
                component_reasons.append("ANCHOR_AMBIGUITY")
            log.append(
                {
                    "field": "components.T1.score",
                    "model_value": previous_score,
                    "runner_value": None,
                    "method": "ENFORCE_T1_CENTRAL_ANCHOR_ELIGIBILITY",
                }
            )
        route(
            "ANCHOR_AMBIGUITY",
            "No eligible central causal claim has a matching active anchor, or an adopted causal conclusion remains unanchored.",
            "relevant_claims",
        )

    # T1 may only be scored when its own anchor references are tied to a
    # central eligible claim of the matching family.  If the model scores T1
    # against a peripheral or incompatible claim, preserve the response but
    # deterministically mark T1 unavailable and route the ambiguity for review.
    t1 = corrected.get("components", {}).get("T1")
    if isinstance(t1, dict) and isinstance(t1.get("score"), int):
        t1_anchor_refs = set(t1.get("anchor_refs", []))
        t1_matches_central_eligible_claim = any(
            claim.get("centrality") == "CENTRAL"
            and claim.get("outlet_stance") in {"ADOPTS", "REPORTS_ONLY", "CONTESTS"}
            and any(
                anchor_id in t1_anchor_refs
                and anchor_matches_claim_kind(
                    known_anchor_families.get(anchor_id), claim.get("claim_kind")
                )
                for anchor_id in claim.get("anchor_ids", [])
            )
            for claim in claims
        )
        if not t1_matches_central_eligible_claim:
            previous_score = t1.get("score")
            t1.update(
                {
                    "score": None,
                    "na_reason": "NO_ACTIVE_ANCHOR_FOR_CENTRAL_CLAIM",
                    "confidence": None,
                    "evidence_spans": [],
                    "anchor_refs": [],
                }
            )
            component_reasons = t1.setdefault("needs_review_reasons", [])
            if "ANCHOR_AMBIGUITY" not in component_reasons:
                component_reasons.append("ANCHOR_AMBIGUITY")
            log.append(
                {
                    "field": "components.T1.score",
                    "model_value": previous_score,
                    "runner_value": None,
                    "method": "ENFORCE_T1_CENTRAL_ANCHOR_ELIGIBILITY",
                }
            )
            route(
                "ANCHOR_AMBIGUITY",
                "The scored T1 anchor is not tied to a matching central eligible claim.",
                "components.T1.anchor_refs",
            )

    for index, claim in enumerate(claims):
        voice = claim.get("voice")
        speaker = claim.get("speaker")
        conflict = (voice == "OUTLET_ASSERTION" and speaker is not None) or (
            voice in {"ATTRIBUTED_CLAIM", "DIRECT_QUOTE", "DENIAL"}
            and speaker is None
        )
        if conflict or claim.get("outlet_stance") == "UNCLEAR":
            route(
                "SOURCE_VOICE_AMBIGUITY",
                "At least one claim has conflicting or unclear voice and outlet stance.",
                f"relevant_claims[{index}]",
            )
        if claim.get("evidence_access") == "DOCUMENTED_PRIVILEGED_BASIS":
            route(
                "PRIVILEGED_ACCESS_LIMITATION",
                "A claim relies on documented privileged access and requires human inspection.",
                f"relevant_claims[{index}].evidence_access",
            )

        asserted_adopted_causal_conflict = (
            claim.get("centrality") == "CENTRAL"
            and claim.get("claim_kind") == "CAUSE_OR_RESPONSIBILITY"
            and claim.get("outlet_stance") == "ADOPTS"
            and claim.get("stance_toward_anchors") == "CONFLICTS"
            and claim.get("certainty") == "ASSERTED"
        )
        if asserted_adopted_causal_conflict:
            for code in ("T1", "T3", "N2"):
                component = corrected.get("components", {}).get(code)
                if not isinstance(component, dict):
                    continue
                component_reasons = component.setdefault("needs_review_reasons", [])
                if "CONFLICTING_PASSAGES" not in component_reasons:
                    component_reasons.append("CONFLICTING_PASSAGES")
            route(
                "CONFLICTING_PASSAGES",
                "A central asserted causal claim is adopted while marked as conflicting with anchors; T1, T3, and N2 require human consistency review.",
                f"relevant_claims[{index}].stance_toward_anchors",
            )

    for code in ("N1", "N3"):
        component = corrected.get("components", {}).get(code)
        if not isinstance(component, dict):
            continue
        score = component.get("score")
        if isinstance(score, int) and score >= 2 and not component.get("anchor_refs"):
            component_reasons = component.setdefault("needs_review_reasons", [])
            if "ANCHOR_AMBIGUITY" not in component_reasons:
                component_reasons.append("ANCHOR_AMBIGUITY")
            route(
                "ANCHOR_AMBIGUITY",
                f"{code} is 2–4 but cites no factual counterevidence anchor.",
                f"components.{code}.anchor_refs",
            )

    n5 = corrected.get("components", {}).get("N5")
    if isinstance(n5, dict) and isinstance(n5.get("score"), int) and n5["score"] > 0:
        evidence_spans = n5.get("evidence_spans", [])
        evidence_text = (
            evidence_spans[0].get("quote_original", "")
            if len(evidence_spans) == 1 and isinstance(evidence_spans[0], dict)
            else ""
        )
        attribution_patterns = {
            "he": r"(?:אמר(?:ה|ו)?|מסר(?:ה|ו)?|הגיב(?:ה)?|טען(?:ה)?|כתב(?:ה)?|הוסיף(?:ה)?|הודיע(?:ה)?|ציין(?:ה)?|לדברי)",
            "en": r"\b(?:said|told|wrote|added|stated|according to)\b",
            "ar": r"(?:قال(?:ت|وا)?|أضاف(?:ت|وا)?|أكد(?:ت|وا)?|صرح(?:ت|وا)?|كتب(?:ت|وا)?|بحسب|وفق(?:ا|اً)?)",
        }
        pattern = attribution_patterns.get(corrected.get("language"))
        if pattern and re.search(pattern, evidence_text, flags=re.IGNORECASE):
            component_reasons = n5.setdefault("needs_review_reasons", [])
            if "SOURCE_VOICE_AMBIGUITY" not in component_reasons:
                component_reasons.append("SOURCE_VOICE_AMBIGUITY")
            route(
                "SOURCE_VOICE_AMBIGUITY",
                "Positive N5 evidence contains an attribution cue; a human must confirm that the loading belongs to the newsroom rather than the quoted speaker.",
                "components.N5.evidence_spans[0]",
            )

    if notes:
        unique_notes = list(dict.fromkeys(notes))
        gate_note = "Deterministic gates: " + " ".join(unique_notes)
        existing_note = review.get("note")
        review["note"] = f"{existing_note} {gate_note}".strip() if existing_note else gate_note
    expected_status = "NEEDS_REVIEW" if review.get("required") else "COMPLETE"
    if corrected.get("analysis_status") != expected_status:
        log.append(
            {
                "field": "analysis_status",
                "model_value": corrected.get("analysis_status"),
                "runner_value": expected_status,
                "method": "ALIGN_STATUS_WITH_REVIEW_ROUTING",
            }
        )
        corrected["analysis_status"] = expected_status
    return corrected, log


def usage_dict(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def cached_successes(path: Path) -> set[tuple[str, str, str]]:
    return {
        (row["document_id"], row["input_hash"], row["model"])
        for row in load_jsonl(path)
        if row["status"] == "SUCCESS"
    }


def generate_response(
    client: Any,
    model: str,
    system_instruction: str,
    prompt: str,
    max_output_tokens: int,
    thinking_budget_tokens: int,
) -> Any:
    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_budget=thinking_budget_tokens,
                include_thoughts=False,
            ),
        ),
    )


def write_summary(path: Path, output_path: Path, run_stats: dict[str, Any]) -> None:
    records = load_jsonl(output_path)
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        latest[record["document_id"]] = record
    summary = {
        "schema_version": "1.0",
        "prompt_version": PROMPT_VERSION,
        "latest_status_counts": {
            status: sum(row["status"] == status for row in latest.values())
            for status in ("SUCCESS", "FAILED")
        },
        "documents_with_latest_record": len(latest),
        "this_run": run_stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def recover_saved_candidates(
    prepared: list[tuple[Any, ...]],
    output_path: Path,
    summary_path: Path,
    model: str,
    project: str,
    location: str,
) -> int:
    records = load_jsonl(output_path)
    completed = 0
    failed = 0
    for document, bundle, provenance, _, _, digest, _ in prepared:
        source = next(
            (
                row
                for row in reversed(records)
                if row.get("document_id") == document["document_id"]
                and row.get("input_hash") == digest
                and row.get("model") == model
                and row.get("candidate_measurement") is not None
            ),
            None,
        )
        if source is None:
            print(
                json.dumps(
                    {
                        "document_id": document["document_id"],
                        "recovered": False,
                        "error": "no saved candidate for the current exact input",
                    }
                )
            )
            failed += 1
            continue
        raw, metadata_log = normalize_fixed_metadata(
            source["candidate_measurement"], document
        )
        raw, rationale_log = normalize_component_rationale_lengths(raw)
        raw, reviewer_aid_log = normalize_reviewer_aids(raw)
        raw, contract_normalization_log = normalize_component_contract_conflicts(raw)
        raw, span_log = resolve_verbatim_spans(raw, document)
        known_anchor_families = {
            item["anchor_id"]: item["family"]
            for item in bundle["time_indexed_anchors"]
        }
        raw, structural_gate_log = enforce_structural_review_gates(
            raw, known_anchor_families
        )
        try:
            result = ArticleMeasurement.model_validate(raw)
            errors = validate_measurement(
                result,
                raw,
                document,
                known_anchor_families,
            )
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {str(exc)[:2000]}"]
        if errors:
            print(
                json.dumps(
                    {
                        "document_id": document["document_id"],
                        "recovered": False,
                        "errors": errors,
                    },
                    ensure_ascii=False,
                )
            )
            failed += 1
            continue
        usage = (source.get("last_response_metadata") or {}).get("usage") or {}
        append_record(
            output_path,
            {
                "schema_version": "1.0",
                "status": "SUCCESS",
                "document_id": document["document_id"],
                "source_id": document["source_id"],
                "language": document["language"],
                "sample_phase": document["sample_phase"],
                "model": model,
                "provider_model_version": source.get("provider_model_version"),
                "project": project,
                "location": location,
                "prompt_version": PROMPT_VERSION,
                "input_hash": digest,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attempts": source.get("attempts"),
                "usage": usage,
                "resource_usage": source.get("resource_usage"),
                "provenance": provenance,
                "recovered_from_saved_candidate": True,
                "source_candidate_recorded_at": source.get("recorded_at"),
                "span_resolution_log": span_log,
                "metadata_correction_log": metadata_log,
                "rationale_normalization_log": rationale_log,
                "reviewer_aid_normalization_log": reviewer_aid_log,
                "component_contract_normalization_log": contract_normalization_log,
                "structural_gate_log": structural_gate_log,
                "attempt_diagnostics": source.get("attempt_diagnostics", []),
                "measurement": result.model_dump(mode="json"),
            },
        )
        completed += 1
        print(
            json.dumps(
                {
                    "document_id": document["document_id"],
                    "recovered": True,
                    "span_resolutions": len(span_log),
                    "reviewer_aids_dropped": len(reviewer_aid_log),
                    "structural_gates": len(structural_gate_log),
                    "analysis_status": result.analysis_status,
                }
            )
        )
    run_stats = {
        "requested": len(prepared),
        "completed": completed,
        "cached": 0,
        "failed": failed,
        "model_calls": 0,
        "total_tokens": 0,
        "recovery_only": True,
    }
    write_summary(summary_path, output_path, run_stats)
    return 1 if failed else 0


def main() -> int:
    global CORPUS_PATH, ANCHORS_PATH, SCHEMA_PATH, ADAPTER_DIR, BUDGET_PATH, PROMPT_VERSION, CASE_ID, CASE_SCOPE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--recover-saved-candidate", action="store_true")
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROSTER_PATH,
        help="Frozen document allow-list; defaults to the calibration roster.",
    )
    parser.add_argument("--case-id", default=CASE_ID)
    parser.add_argument(
        "--case-manifest",
        type=Path,
        help="Approved case manifest supplying the event scope for claim selection.",
    )
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--anchors", type=Path, default=ANCHORS_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--adapter-dir", type=Path, default=ADAPTER_DIR)
    parser.add_argument("--budget", type=Path, default=BUDGET_PATH)
    parser.add_argument("--prompt-version", default=PROMPT_VERSION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=os.getenv("GCP_REGION", DEFAULT_LOCATION))
    parser.add_argument("--model", default=os.getenv("VERTEX_AI_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    if not re.fullmatch(r"CASE-[0-9]{3}", args.case_id):
        raise ValueError("case-id must match CASE-NNN")
    CASE_ID = args.case_id
    if args.case_manifest is not None:
        manifest_payload = json.loads(args.case_manifest.read_text(encoding="utf-8"))
        CASE_SCOPE = case_scope_from_manifest(manifest_payload)
        if CASE_SCOPE["case_id"] != CASE_ID:
            raise ValueError("case manifest case_id does not match --case-id")
    elif CASE_ID != "CASE-001":
        raise ValueError("--case-manifest is required for non-calibration cases")
    CORPUS_PATH = args.corpus
    ANCHORS_PATH = args.anchors
    SCHEMA_PATH = args.schema
    ADAPTER_DIR = args.adapter_dir
    BUDGET_PATH = args.budget
    PROMPT_VERSION = args.prompt_version
    for required_path in (
        CORPUS_PATH,
        ANCHORS_PATH,
        SCHEMA_PATH,
        ADAPTER_DIR,
        BUDGET_PATH,
        *([args.case_manifest] if args.case_manifest is not None else []),
    ):
        if not required_path.exists():
            raise ValueError(f"required analysis input does not exist: {required_path}")

    budgets = json.loads(BUDGET_PATH.read_text())
    roster = json.loads(args.manifest.read_text())
    if roster.get("prompt_version") not in (None, PROMPT_VERSION):
        raise ValueError("MANIFEST_PROMPT_VERSION_MISMATCH")
    if roster.get("budget_version") not in (None, budgets["budget_version"]):
        raise ValueError("MANIFEST_BUDGET_VERSION_MISMATCH")
    corpus = load_corpus()
    requested = args.document_id or roster["document_ids"]
    if len(requested) > budgets["max_documents_per_run"]:
        raise ValueError("MAX_DOCUMENTS_PER_RUN_EXCEEDED")
    if len(requested) != len(set(requested)):
        raise ValueError("duplicate document IDs requested")
    unknown = set(requested) - corpus.keys()
    outside_roster = set(requested) - set(roster["document_ids"])
    if unknown or outside_roster:
        raise ValueError(
            f"invalid documents: unknown={sorted(unknown)}, outside_roster={sorted(outside_roster)}"
        )

    prepared = []
    for document_id in requested:
        document = corpus[document_id]
        bundle, provenance, adapter_name = build_bundle(document)
        prompt = build_prompt(bundle)
        adapter_text = json.dumps(
            active_language_adapter(ADAPTER_DIR / adapter_name), ensure_ascii=False
        )
        total_input_chars = (
            len(prompt) + len(SYSTEM_RULEBOOK_PATH.read_text()) + len(adapter_text)
        )
        if total_input_chars > budgets["max_prompt_chars"]:
            raise ValueError(f"PROMPT_CHAR_BUDGET_EXCEEDED:{document_id}")
        digest = stable_hash(
            {
                "prompt_version": PROMPT_VERSION,
                "model": args.model,
                "provenance": provenance,
                "bundle": bundle,
            }
        )
        prepared.append(
            (
                document,
                bundle,
                provenance,
                adapter_name,
                prompt,
                digest,
                total_input_chars,
            )
        )

    if args.recover_saved_candidate:
        return recover_saved_candidates(
            prepared,
            args.output,
            args.summary,
            args.model,
            args.project,
            args.location,
        )

    if not args.live:
        cache = cached_successes(args.output)
        print(
            json.dumps(
                {
                    "valid": True,
                    "live": False,
                    "documents": [
                        {
                            "document_id": row[0]["document_id"],
                            "language": row[0]["language"],
                            "phase": row[0]["sample_phase"],
                            "document_chars": len(row[0]["text"]),
                            "prompt_chars": len(row[4]),
                            "active_anchor_assessments": sum(
                                item["assessment_available_at_publication"] is not None
                                for item in row[1]["time_indexed_anchors"]
                            ),
                            "total_input_chars": row[6],
                            "input_hash": row[5],
                            "cache_hit": (
                                row[0]["document_id"], row[5], args.model
                            ) in cache,
                        }
                        for row in prepared
                    ],
                    "budgets": budgets,
                },
                ensure_ascii=False,
            )
        )
        return 0

    start = time.monotonic()
    rss_start = current_rss_mb()
    cache = cached_successes(args.output)
    client = genai.Client(vertexai=True, project=args.project, location=args.location)
    system_rulebook = SYSTEM_RULEBOOK_PATH.read_text()
    calls = 0
    tokens = 0
    completed = 0
    skipped = 0
    failed = 0

    for document, bundle, provenance, adapter_name, prompt, digest, _ in prepared:
        if (document["document_id"], digest, args.model) in cache:
            skipped += 1
            continue
        elapsed = time.monotonic() - start
        rss_before = current_rss_mb()
        if (
            calls >= budgets["max_model_calls_per_run"]
            or tokens >= budgets["max_total_tokens_per_run"]
            or elapsed >= budgets["max_elapsed_seconds_per_run"]
            or (
                rss_before is not None
                and rss_before
                > budgets["stop_before_next_document_if_rss_mb_exceeds"]
            )
        ):
            failure_code = "BUDGET_EXHAUSTED"
            append_record(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "FAILED",
                    "failure_code": failure_code,
                    "document_id": document["document_id"],
                    "model": args.model,
                    "prompt_version": PROMPT_VERSION,
                    "input_hash": digest,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "error": "Run stopped before this document because a hard budget was reached.",
                    "provenance": provenance,
                },
            )
            failed += 1
            continue

        adapter = active_language_adapter(ADAPTER_DIR / adapter_name)
        system_instruction = system_rulebook + "\n\nLANGUAGE ADAPTER:\n" + json.dumps(
            adapter, ensure_ascii=False
        )
        attempts = 0
        validation_errors: list[str] = []
        final_result = None
        final_response = None
        error_text = None
        failure_code = "MODEL_ERROR"
        last_response_metadata: dict[str, Any] | None = None
        final_span_resolution_log: list[dict[str, Any]] = []
        final_metadata_correction_log: list[dict[str, Any]] = []
        final_rationale_normalization_log: list[dict[str, Any]] = []
        final_reviewer_aid_normalization_log: list[dict[str, Any]] = []
        final_component_contract_normalization_log: list[dict[str, Any]] = []
        final_structural_gate_log: list[dict[str, Any]] = []
        last_candidate_measurement: dict[str, Any] | None = None
        attempt_diagnostics: list[dict[str, Any]] = []
        while attempts <= budgets["max_repair_calls_per_document"]:
            if calls >= budgets["max_model_calls_per_run"]:
                error_text = "model-call budget exhausted before repair"
                break
            attempts += 1
            calls += 1
            call_prompt = prompt
            if validation_errors and failure_code != "MODEL_ERROR":
                call_prompt = (
                    prompt
                    + "\n\nREPAIR THE PREVIOUS OUTPUT. Validation errors:\n- "
                    + "\n- ".join(validation_errors)
                    + "\nReturn a fresh complete JSON object."
                )
            try:
                response = generate_response(
                    client,
                    args.model,
                    system_instruction,
                    call_prompt,
                    budgets["max_output_tokens_per_call"],
                    budgets["thinking_budget_tokens_per_call"],
                )
                usage = usage_dict(response)
                tokens += usage["total_tokens"] or 0
                finish_reason = None
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    finish_reason = str(getattr(candidates[0], "finish_reason", None))
                last_response_metadata = {
                    "usage": usage,
                    "response_chars": len(response.text or ""),
                    "finish_reason": finish_reason,
                }
                raw = json.loads(response.text)
                raw, metadata_correction_log = normalize_fixed_metadata(raw, document)
                raw, rationale_normalization_log = normalize_component_rationale_lengths(raw)
                raw, reviewer_aid_log = normalize_reviewer_aids(raw)
                raw, contract_normalization_log = normalize_component_contract_conflicts(raw)
                raw, span_resolution_log = resolve_verbatim_spans(raw, document)
                known_anchor_families = {
                    item["anchor_id"]: item["family"]
                    for item in bundle["time_indexed_anchors"]
                }
                raw, structural_gate_log = enforce_structural_review_gates(
                    raw, known_anchor_families
                )
                last_candidate_measurement = raw
                result = ArticleMeasurement.model_validate(raw)
                validation_errors = validate_measurement(
                    result,
                    raw,
                    document,
                    known_anchor_families,
                )
                if not validation_errors:
                    final_result = result
                    final_response = response
                    final_span_resolution_log = span_resolution_log
                    final_metadata_correction_log = metadata_correction_log
                    final_rationale_normalization_log = rationale_normalization_log
                    final_reviewer_aid_normalization_log = reviewer_aid_log
                    final_component_contract_normalization_log = contract_normalization_log
                    final_structural_gate_log = structural_gate_log
                    attempt_diagnostics.append(
                        {
                            "attempt": attempts,
                            "response": last_response_metadata,
                            "validation_errors": [],
                            "span_resolutions": span_resolution_log,
                            "metadata_corrections": metadata_correction_log,
                            "rationale_normalizations": rationale_normalization_log,
                            "reviewer_aid_normalizations": reviewer_aid_log,
                            "component_contract_normalizations": contract_normalization_log,
                            "structural_gates": structural_gate_log,
                        }
                    )
                    break
                attempt_diagnostics.append(
                    {
                        "attempt": attempts,
                        "response": last_response_metadata,
                        "validation_errors": validation_errors,
                        "span_resolutions": span_resolution_log,
                        "metadata_corrections": metadata_correction_log,
                        "rationale_normalizations": rationale_normalization_log,
                        "reviewer_aid_normalizations": reviewer_aid_log,
                        "component_contract_normalizations": contract_normalization_log,
                        "structural_gates": structural_gate_log,
                    }
                )
                if any("evidence span not found" in item for item in validation_errors):
                    failure_code = "EVIDENCE_SPAN_NOT_FOUND"
                elif any("unknown anchor" in item for item in validation_errors):
                    failure_code = "ANCHOR_REFERENCE_UNKNOWN"
                else:
                    failure_code = "SCHEMA_VALIDATION_FAILED"
            except json.JSONDecodeError as exc:
                failure_code = "INVALID_JSON"
                validation_errors = [f"JSONDecodeError: {str(exc)[:1000]}"]
                attempt_diagnostics.append(
                    {
                        "attempt": attempts,
                        "response": last_response_metadata,
                        "validation_errors": validation_errors,
                        "span_resolutions": [],
                    }
                )
            except PydanticValidationError as exc:
                failure_code = "SCHEMA_VALIDATION_FAILED"
                validation_errors = [f"ValidationError: {str(exc)[:1000]}"]
                attempt_diagnostics.append(
                    {
                        "attempt": attempts,
                        "response": last_response_metadata,
                        "validation_errors": validation_errors,
                        "span_resolutions": [],
                    }
                )
            except Exception as exc:  # provider errors become bounded failures
                failure_code = "MODEL_ERROR"
                validation_errors = [f"{type(exc).__name__}: {str(exc)[:1000]}"]
                attempt_diagnostics.append(
                    {
                        "attempt": attempts,
                        "response": last_response_metadata,
                        "validation_errors": validation_errors,
                        "span_resolutions": [],
                    }
                )
            error_text = "; ".join(validation_errors)[:4000]

        rss_after = current_rss_mb()
        if final_result is None:
            append_record(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "FAILED",
                    "failure_code": failure_code,
                    "document_id": document["document_id"],
                    "model": args.model,
                    "prompt_version": PROMPT_VERSION,
                    "input_hash": digest,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": attempts,
                    "error": error_text or "unknown bounded failure",
                    "provenance": provenance,
                    "resource_usage": {
                        "rss_before_mb": rss_before,
                        "rss_after_mb": rss_after,
                        "peak_rss_mb": peak_rss_mb(),
                    },
                    "last_response_metadata": last_response_metadata,
                    "attempt_diagnostics": attempt_diagnostics,
                    "candidate_measurement": last_candidate_measurement,
                },
            )
            failed += 1
            continue

        response_usage = usage_dict(final_response)
        append_record(
            args.output,
            {
                "schema_version": "1.0",
                "status": "SUCCESS",
                "document_id": document["document_id"],
                "source_id": document["source_id"],
                "language": document["language"],
                "sample_phase": document["sample_phase"],
                "model": args.model,
                "provider_model_version": getattr(final_response, "model_version", None),
                "project": args.project,
                "location": args.location,
                "prompt_version": PROMPT_VERSION,
                "input_hash": digest,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
                "usage": response_usage,
                "resource_usage": {
                    "rss_before_mb": rss_before,
                    "rss_after_mb": rss_after,
                    "peak_rss_mb": peak_rss_mb(),
                    "elapsed_seconds_total_run": round(time.monotonic() - start, 3),
                },
                "provenance": provenance,
                "span_resolution_log": final_span_resolution_log,
                "metadata_correction_log": final_metadata_correction_log,
                "rationale_normalization_log": final_rationale_normalization_log,
                "reviewer_aid_normalization_log": (
                    final_reviewer_aid_normalization_log
                ),
                "component_contract_normalization_log": (
                    final_component_contract_normalization_log
                ),
                "structural_gate_log": final_structural_gate_log,
                "attempt_diagnostics": attempt_diagnostics,
                "measurement": final_result.model_dump(mode="json"),
            },
        )
        completed += 1
        print(
            json.dumps(
                {
                    "document_id": document["document_id"],
                    "status": "SUCCESS",
                    "attempts": attempts,
                    "usage": response_usage,
                    "rss_after_mb": rss_after,
                }
            ),
            flush=True,
        )

    run_stats = {
        "requested": len(prepared),
        "completed": completed,
        "cached": skipped,
        "failed": failed,
        "model_calls": calls,
        "total_tokens": tokens,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "rss_start_mb": rss_start,
        "rss_end_mb": current_rss_mb(),
        "peak_rss_mb": peak_rss_mb(),
    }
    write_summary(args.summary, args.output, run_stats)
    print(json.dumps({"run": run_stats, "output": str(args.output)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
