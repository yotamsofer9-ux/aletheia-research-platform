from __future__ import annotations

import copy
import difflib
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "0.1.0"
METHODOLOGY_VERSION = "frame-intensity-v0.1.0"
PROMPT_VERSION = "case002-frame-intensity-v0.1.0"
COMPONENTS = ("F1", "F2", "F3", "F4", "F5")
PROMINENT_LOCATIONS = {"HEADLINE", "SUBHEADLINE", "LEAD"}
EDITORIALLY_RELEVANT_OWNERS = {"OUTLET", "HEADLINE_SELECTED_QUOTE"}
FUNCTION_BY_COMPONENT = {
    "F1": "PROBLEM_DEFINITION",
    "F2": "AGENCY_CAUSALITY",
    "F3": "MORAL_EVALUATION",
    "F4": "TREATMENT_DIRECTION",
    "F5": "RHETORICAL_REINFORCEMENT",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ReviewReason = Literal[
    "SCORE_GAP",
    "SOURCE_VOICE_AMBIGUITY",
    "TRANSLATION_UNCERTAINTY",
    "VISUAL_CONTENT_UNAVAILABLE",
    "EVIDENCE_SPAN_NOT_FOUND",
    "FRAME_FUNCTION_AMBIGUITY",
    "LOW_CONFIDENCE",
    "SOURCE_INCOMPLETE",
    "OTHER",
]


class FrameCue(StrictModel):
    cue_id: str = Field(pattern=r"^cue_[1-9][0-9]*$")
    quote_original: str = Field(min_length=1, max_length=500)
    location: Literal[
        "HEADLINE",
        "SUBHEADLINE",
        "LEAD",
        "BODY",
        "CONCLUSION",
        "VISUAL_CAPTION",
        "TRANSCRIPT",
    ]
    owner: Literal[
        "OUTLET",
        "ATTRIBUTED_ACTOR",
        "HEADLINE_SELECTED_QUOTE",
        "UNCLEAR",
    ]
    function: Literal[
        "PROBLEM_DEFINITION",
        "AGENCY_CAUSALITY",
        "MORAL_EVALUATION",
        "TREATMENT_DIRECTION",
        "RHETORICAL_REINFORCEMENT",
    ]
    device: Literal[
        "PLAIN_ASSERTION",
        "LABEL",
        "METAPHOR",
        "EXEMPLAR",
        "STATISTIC",
        "SOURCE_SELECTION",
        "QUOTE_SELECTION",
        "REPETITION",
        "CONTRAST",
        "VISUAL",
        "OTHER",
    ]
    reviewer_translation_he: str | None = Field(default=None, max_length=700)


class FrameComponent(StrictModel):
    score: int = Field(ge=0, le=4)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    rationale_he: str = Field(min_length=1, max_length=420)
    evidence_cue_ids: list[str] = Field(default_factory=list, max_length=6)
    needs_review_reasons: list[ReviewReason] = Field(default_factory=list, max_length=5)


class FrameComponents(StrictModel):
    F1: FrameComponent
    F2: FrameComponent
    F3: FrameComponent
    F4: FrameComponent
    F5: FrameComponent


class NeedsReview(StrictModel):
    required: bool
    reasons: list[ReviewReason] = Field(default_factory=list, max_length=8)
    note_he: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def reasons_match_required(self):
        if self.required != bool(self.reasons):
            raise ValueError("needs_review.required must match presence of reasons")
        if self.required and not self.note_he:
            raise ValueError("review routing requires a Hebrew note")
        if not self.required and self.note_he is not None:
            raise ValueError("review note must be null when no review is required")
        return self


class FrameMeasurement(StrictModel):
    schema_version: Literal["0.1.0"]
    methodology_version: Literal["frame-intensity-v0.1.0"]
    prompt_version: Literal["case002-frame-intensity-v0.1.0"]
    case_id: str = Field(pattern=r"^CASE-[0-9]{3}$")
    document_id: str = Field(min_length=1)
    language: Literal["he", "en", "ar"]
    analysis_status: Literal["COMPLETE", "NEEDS_REVIEW"]
    coverage_scope: Literal[
        "FULL_TEXT",
        "TEXTUAL_PAGE_ONLY",
        "TEXT_WITH_VISUAL_EVIDENCE",
        "INCOMPLETE_SOURCE",
    ]
    frame_statement_he: str = Field(min_length=1, max_length=320)
    frame_families: list[
        Literal[
            "HUMANITARIAN",
            "SECURITY",
            "RESPONSIBILITY",
            "CONFLICT",
            "HUMAN_INTEREST",
            "MORALITY",
            "CONSEQUENCES",
            "INVESTIGATIVE",
            "MEDIA_ACCOUNTABILITY",
            "STRATEGIC",
            "POLITICAL",
            "OTHER",
        ]
    ] = Field(min_length=1, max_length=3)
    frame_cues: list[FrameCue] = Field(min_length=1, max_length=20)
    components: FrameComponents
    needs_review: NeedsReview

    @model_validator(mode="after")
    def status_matches_review(self):
        expected = "NEEDS_REVIEW" if self.needs_review.required else "COMPLETE"
        if self.analysis_status != expected:
            raise ValueError("analysis_status must match needs_review.required")
        return self


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def frame_score(measurement: FrameMeasurement | dict[str, Any]) -> float:
    if isinstance(measurement, FrameMeasurement):
        scores = [getattr(measurement.components, code).score for code in COMPONENTS]
    else:
        scores = [measurement["components"][code]["score"] for code in COMPONENTS]
    return round(100.0 * sum(scores) / 20.0, 4)


def resolve_verbatim_cues(
    raw: dict[str, Any], *, document: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Conservatively replace near-exact cue text with an exact source substring.

    Only whitespace-only matches or long contiguous substrings are eligible. A
    match below 90% of the model-selected span is kept auditable and routed to
    review; no source wording is invented.
    """
    corrected = copy.deepcopy(raw)
    searchable = "\n".join(
        value for value in (document.get("title", ""), document.get("text", "")) if value
    )
    log: list[dict[str, Any]] = []
    review_cue_ids: set[str] = set()
    unresolved_cue_ids: set[str] = set()
    for cue in corrected.get("frame_cues", []):
        quote = cue.get("quote_original")
        cue_id = cue.get("cue_id")
        if not isinstance(quote, str) or not quote:
            continue
        title = document.get("title", "")
        wrong_headline_location = (
            cue.get("location") == "HEADLINE" and quote not in title
        )
        if quote in searchable and not wrong_headline_location:
            continue
        # Headline cues occasionally omit or substitute a single Hebrew
        # preposition/article (for example, ``הבור`` for the source's
        # ``בבור``).  For a short cue the generic long-span resolver
        # below intentionally refuses to guess.  A high-similarity,
        # same-word-count window from the title is nevertheless safe: the
        # replacement is verbatim source text, remains in the claimed
        # location, and is recorded in the audit log.
        if cue.get("location") == "HEADLINE" and title:
            quote_words = quote.split()
            title_words = title.split()
            window_size = len(quote_words)
            headline_candidates = [
                " ".join(title_words[index : index + window_size])
                for index in range(max(0, len(title_words) - window_size + 1))
            ]
            if headline_candidates:
                resolved = max(
                    headline_candidates,
                    key=lambda candidate: difflib.SequenceMatcher(
                        None, quote, candidate, autojunk=False
                    ).ratio(),
                )
                ratio = difflib.SequenceMatcher(
                    None, quote, resolved, autojunk=False
                ).ratio()
                if 12 <= len(quote) <= 60 and ratio >= 0.9:
                    cue["quote_original"] = resolved
                    log.append(
                        {
                            "cue_id": cue_id,
                            "original_hash": sha256(quote.encode()).hexdigest(),
                            "original_chars": len(quote),
                            "resolved_chars": len(resolved),
                            "matched_ratio": round(ratio, 4),
                            "method": "HIGH_SIMILARITY_VERBATIM_HEADLINE_WINDOW",
                            "requires_review": False,
                        }
                    )
                    continue
        whitespace_pattern = r"\s+".join(re.escape(part) for part in quote.split())
        whitespace_match = re.search(whitespace_pattern, searchable)
        if whitespace_match:
            resolved = whitespace_match.group(0)
            cue["quote_original"] = resolved
            log.append(
                {
                    "cue_id": cue_id,
                    "original_hash": sha256(quote.encode()).hexdigest(),
                    "original_chars": len(quote),
                    "resolved_chars": len(resolved),
                    "matched_ratio": 1.0,
                    "method": "WHITESPACE_ONLY_SOURCE_RESOLUTION",
                    "requires_review": False,
                }
            )
            continue
        match = difflib.SequenceMatcher(
            None, quote, searchable, autojunk=False
        ).find_longest_match(0, len(quote), 0, len(searchable))
        ratio = match.size / len(quote)
        if match.size < 80 or ratio < 0.8:
            if isinstance(cue_id, str):
                unresolved_cue_ids.add(cue_id)
            continue
        resolved = searchable[match.b : match.b + match.size].strip()
        if len(resolved) < 80 or resolved not in searchable:
            continue
        requires_review = ratio < 0.9
        cue["quote_original"] = resolved
        log.append(
            {
                "cue_id": cue_id,
                "original_hash": sha256(quote.encode()).hexdigest(),
                "original_chars": len(quote),
                "resolved_chars": len(resolved),
                "matched_ratio": round(ratio, 4),
                "method": "LONGEST_EXACT_SOURCE_SUBSTRING",
                "requires_review": requires_review,
            }
        )
        if requires_review and isinstance(cue_id, str):
            review_cue_ids.add(cue_id)

    # Drop a non-verbatim or low-ratio repaired cue only when it is strictly
    # redundant: every
    # component that cited it retains another cited, source-exact cue with the
    # component's required function. Scores, rationales, and decisive evidence
    # therefore remain unchanged. Otherwise the normal validator still fails.
    removable_candidates = unresolved_cue_ids | review_cue_ids
    if removable_candidates:
        cues_by_id = {
            cue.get("cue_id"): cue for cue in corrected.get("frame_cues", [])
        }
        components = corrected.get("components", {})
        safely_droppable = set()
        for cue_id in removable_candidates:
            referencing = [
                (code, component)
                for code, component in components.items()
                if cue_id in component.get("evidence_cue_ids", [])
            ]
            safe = True
            for code, component in referencing:
                remaining = [
                    other_id
                    for other_id in component.get("evidence_cue_ids", [])
                    if other_id != cue_id
                ]
                expected = FUNCTION_BY_COMPONENT.get(code)
                if not any(
                    other_id in cues_by_id
                    and cues_by_id[other_id].get("quote_original") in searchable
                    and (
                        cues_by_id[other_id].get("function") == expected
                        or code == "F5"
                    )
                    for other_id in remaining
                ):
                    safe = False
                    break
            if safe and referencing:
                safely_droppable.add(cue_id)
        if safely_droppable:
            corrected["frame_cues"] = [
                cue
                for cue in corrected.get("frame_cues", [])
                if cue.get("cue_id") not in safely_droppable
            ]
            for component in components.values():
                component["evidence_cue_ids"] = [
                    cue_id
                    for cue_id in component.get("evidence_cue_ids", [])
                    if cue_id not in safely_droppable
                ]
            remaining_cues = corrected.get("frame_cues", [])
            remaining_by_id = {
                cue.get("cue_id"): cue for cue in remaining_cues
            }
            f5 = components.get("F5", {})
            f5_refs = f5.get("evidence_cue_ids", [])
            if (
                isinstance(f5.get("score"), int)
                and f5["score"] > 0
                and not any(
                    remaining_by_id.get(cue_id, {}).get("function")
                    == "RHETORICAL_REINFORCEMENT"
                    for cue_id in f5_refs
                )
            ):
                source = next(
                    (
                        remaining_by_id[cue_id]
                        for cue_id in f5_refs
                        if cue_id in remaining_by_id
                        and remaining_by_id[cue_id].get("quote_original") in searchable
                    ),
                    None,
                )
                if source is not None:
                    numbers = [
                        int(match.group(1))
                        for cue_id in remaining_by_id
                        if (match := re.fullmatch(r"cue_([1-9][0-9]*)", cue_id))
                    ]
                    reinforcement_id = f"cue_{max(numbers, default=0) + 1}"
                    duplicate = copy.deepcopy(source)
                    duplicate["cue_id"] = reinforcement_id
                    duplicate["function"] = "RHETORICAL_REINFORCEMENT"
                    remaining_cues.append(duplicate)
                    f5["evidence_cue_ids"].append(reinforcement_id)
                    log.append(
                        {
                            "cue_id": reinforcement_id,
                            "method": "REPLACE_UNRESOLVED_F5_WITH_SELECTED_EXACT_CUE",
                            "source_cue_id": source["cue_id"],
                            "requires_review": False,
                        }
                    )
            for cue_id in sorted(safely_droppable):
                log.append(
                    {
                        "cue_id": cue_id,
                        "method": (
                            "DROP_LOW_RATIO_REDUNDANT_CUE"
                            if cue_id in review_cue_ids
                            else "DROP_UNRESOLVED_REDUNDANT_CUE"
                        ),
                        "requires_review": False,
                    }
                )
            review_cue_ids -= safely_droppable

    if review_cue_ids:
        for component in corrected.get("components", {}).values():
            if review_cue_ids.intersection(component.get("evidence_cue_ids", [])):
                reasons = component.setdefault("needs_review_reasons", [])
                if "EVIDENCE_SPAN_NOT_FOUND" not in reasons:
                    reasons.append("EVIDENCE_SPAN_NOT_FOUND")
        review = corrected.setdefault(
            "needs_review", {"required": False, "reasons": [], "note_he": None}
        )
        review["required"] = True
        if "EVIDENCE_SPAN_NOT_FOUND" not in review.setdefault("reasons", []):
            review["reasons"].append("EVIDENCE_SPAN_NOT_FOUND")
        review["note_he"] = (
            "קטע ראיה אחד לפחות קוצר לתת־מחרוזת ארוכה ומדויקת מן המקור; "
            "יש לאשר שהקיצור אינו משנה את משמעות הסימן."
        )
        corrected["analysis_status"] = "NEEDS_REVIEW"
    return corrected, log


def normalize_frame_output_limits(
    raw: dict[str, Any], *, rationale_limit: int = 420, evidence_limit: int = 6
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply lossless or conservative output-size normalization with an audit log."""
    corrected = copy.deepcopy(raw)
    log: list[dict[str, Any]] = []
    components = corrected.get("components", {})
    for code, component in components.items():
        rationale = component.get("rationale_he")
        if isinstance(rationale, str) and len(rationale) > rationale_limit:
            prefix = rationale[:rationale_limit]
            boundaries = [
                prefix.rfind(marker) + len(marker)
                for marker in (". ", "? ", "! ", ".\n", "?\n", "!\n")
                if prefix.rfind(marker) >= rationale_limit // 2
            ]
            if boundaries:
                shortened = prefix[: max(boundaries)].rstrip()
            else:
                word_end = prefix.rfind(" ")
                shortened = (
                    prefix[:word_end]
                    if word_end >= rationale_limit // 2
                    else prefix
                ).rstrip()
            component["rationale_he"] = shortened
            log.append(
                {
                    "field": f"components.{code}.rationale_he",
                    "original_chars": len(rationale),
                    "normalized_chars": len(shortened),
                    "original_hash": sha256(rationale.encode()).hexdigest(),
                    "method": "EXACT_PREFIX_AT_SENTENCE_OR_WORD_BOUNDARY",
                }
            )
        evidence = component.get("evidence_cue_ids")
        if isinstance(evidence, list) and len(evidence) > evidence_limit:
            dropped = evidence[evidence_limit:]
            component["evidence_cue_ids"] = evidence[:evidence_limit]
            log.append(
                {
                    "field": f"components.{code}.evidence_cue_ids",
                    "original_count": len(evidence),
                    "normalized_count": evidence_limit,
                    "dropped_ids": dropped,
                    "method": "PRESERVE_FIRST_SCHEMA_LIMITED_EVIDENCE_IDS",
                }
            )

    # F5 is explicitly structural: the same exact passage may legitimately be
    # represented once for its substantive function and once for rhetorical
    # reinforcement. Models often select the right passage for F5 but omit the
    # duplicate cue required by the frozen one-function-per-cue representation.
    # This normalization changes neither the quote nor the score; it only makes
    # the model's own F5 evidence selection structurally explicit and auditable.
    cues = corrected.get("frame_cues", [])
    # Preserve a valid model-selected cue when the model uses a semantically
    # intelligible device label that is outside the frozen taxonomy.  The
    # generic OTHER bucket exists for exactly this case; the wording, owner,
    # function and score remain untouched and the coercion is audited.
    device_aliases = {"RHETORICAL_QUESTION": "OTHER"}
    for cue in cues:
        quote = cue.get("quote_original")
        if isinstance(quote, str) and len(quote) > 500:
            prefix = quote[:500]
            sentence_boundaries = [
                prefix.rfind(marker) + len(marker)
                for marker in (". ", "? ", "! ", ".\n", "?\n", "!\n")
                if prefix.rfind(marker) >= 300
            ]
            if sentence_boundaries:
                shortened = prefix[: max(sentence_boundaries)].rstrip()
            else:
                word_end = prefix.rfind(" ")
                shortened = (
                    prefix[:word_end] if word_end >= 300 else prefix
                ).rstrip()
            cue["quote_original"] = shortened
            log.append(
                {
                    "field": f"frame_cues.{cue.get('cue_id')}.quote_original",
                    "original_chars": len(quote),
                    "normalized_chars": len(shortened),
                    "original_hash": sha256(quote.encode()).hexdigest(),
                    "method": "EXACT_PREFIX_TO_SCHEMA_QUOTE_LIMIT",
                }
            )
        device = cue.get("device")
        if device in device_aliases:
            cue["device"] = device_aliases[device]
            log.append(
                {
                    "field": f"frame_cues.{cue.get('cue_id')}.device",
                    "model_value": device,
                    "normalized_value": cue["device"],
                    "method": "MAP_SEMANTIC_DEVICE_ALIAS_TO_FROZEN_OTHER_BUCKET",
                }
            )
        # HEADLINE_SELECTED_QUOTE is a structural owner class reserved for a
        # quotation foregrounded in the headline or subheadline.  Models
        # occasionally apply it to an ordinary quoted passage in the lead.
        # Preserve the exact passage and its analytical function, but restore
        # the conservative attributed-speaker ownership used outside headings.
        if (
            cue.get("owner") == "HEADLINE_SELECTED_QUOTE"
            and cue.get("location") not in {"HEADLINE", "SUBHEADLINE"}
        ):
            original_location = cue.get("location")
            cue["owner"] = "ATTRIBUTED_ACTOR"
            log.append(
                {
                    "field": f"frame_cues.{cue.get('cue_id')}.owner",
                    "model_value": "HEADLINE_SELECTED_QUOTE",
                    "normalized_value": "ATTRIBUTED_ACTOR",
                    "location": original_location,
                    "method": "NORMALIZE_NON_HEADLINE_SELECTED_QUOTE_OWNER",
                }
            )
    cue_by_id = {
        cue.get("cue_id"): cue
        for cue in cues
        if isinstance(cue, dict) and isinstance(cue.get("cue_id"), str)
    }
    # A passage may legitimately perform more than one framing function. When
    # the model selects an exact cue as evidence for F1-F4 but leaves its
    # single-valued function tag on the passage's other role, duplicate the
    # same source-exact cue under the component's required function. The model
    # has already made the analytical selection; this is representational only.
    for code in ("F1", "F2", "F3", "F4"):
        component = components.get(code, {})
        refs = component.get("evidence_cue_ids", [])
        expected_function = FUNCTION_BY_COMPONENT[code]
        if not (
            isinstance(component.get("score"), int)
            and component["score"] > 0
            and isinstance(refs, list)
            and refs
        ):
            continue
        if any(
            cue_by_id.get(cue_id, {}).get("function") == expected_function
            for cue_id in refs
        ):
            continue
        source = next(
            (cue_by_id[cue_id] for cue_id in refs if cue_id in cue_by_id), None
        )
        if source is None:
            continue
        numbers = [
            int(match.group(1))
            for cue_id in cue_by_id
            if (match := re.fullmatch(r"cue_([1-9][0-9]*)", cue_id))
        ]
        duplicate_id = f"cue_{max(numbers, default=0) + 1}"
        duplicate = copy.deepcopy(source)
        duplicate["cue_id"] = duplicate_id
        duplicate["function"] = expected_function
        cues.append(duplicate)
        cue_by_id[duplicate_id] = duplicate
        if len(refs) >= evidence_limit:
            replaced_id = refs[-1]
            component["evidence_cue_ids"] = refs[:-1] + [duplicate_id]
        else:
            replaced_id = None
            component["evidence_cue_ids"] = refs + [duplicate_id]
        log.append(
            {
                "field": f"components.{code}.evidence_cue_ids",
                "method": "DUPLICATE_MODEL_SELECTED_COMPONENT_EVIDENCE",
                "source_cue_id": source["cue_id"],
                "component_cue_id": duplicate_id,
                "component_function": expected_function,
                "replaced_cue_id": replaced_id,
                "score_unchanged": component["score"],
            }
        )
    f5 = components.get("F5", {})
    f5_refs = f5.get("evidence_cue_ids", [])
    if isinstance(f5.get("score"), int) and f5["score"] > 0 and isinstance(
        f5_refs, list
    ):
        matching_refs = [
            cue_id
            for cue_id in f5_refs
            if cue_by_id.get(cue_id, {}).get("function")
            == "RHETORICAL_REINFORCEMENT"
        ]
        if not matching_refs:
            existing = next(
                (
                    cue
                    for cue in cues
                    if cue.get("function") == "RHETORICAL_REINFORCEMENT"
                ),
                None,
            )
            if existing is not None:
                reinforcement_id = existing["cue_id"]
                method = "ATTACH_EXISTING_REINFORCEMENT_CUE"
                source_id = reinforcement_id
            else:
                source = next(
                    (cue_by_id[cue_id] for cue_id in f5_refs if cue_id in cue_by_id),
                    None,
                )
                if source is not None:
                    numbers = [
                        int(match.group(1))
                        for cue_id in cue_by_id
                        if (match := re.fullmatch(r"cue_([1-9][0-9]*)", cue_id))
                    ]
                    reinforcement_id = f"cue_{max(numbers, default=0) + 1}"
                    duplicate = copy.deepcopy(source)
                    duplicate["cue_id"] = reinforcement_id
                    duplicate["function"] = "RHETORICAL_REINFORCEMENT"
                    cues.append(duplicate)
                    cue_by_id[reinforcement_id] = duplicate
                    method = "DUPLICATE_MODEL_SELECTED_F5_EVIDENCE"
                    source_id = source["cue_id"]
                else:
                    reinforcement_id = None
                    method = None
                    source_id = None
            if reinforcement_id is not None:
                if len(f5_refs) >= evidence_limit:
                    replaced_id = f5_refs[-1]
                    f5["evidence_cue_ids"] = f5_refs[:-1] + [reinforcement_id]
                else:
                    replaced_id = None
                    f5["evidence_cue_ids"] = f5_refs + [reinforcement_id]
                log.append(
                    {
                        "field": "components.F5.evidence_cue_ids",
                        "method": method,
                        "source_cue_id": source_id,
                        "reinforcement_cue_id": reinforcement_id,
                        "replaced_cue_id": replaced_id,
                        "score_unchanged": f5["score"],
                    }
                )

    # Keep the frozen 20-cue capacity without discarding any cue actually cited
    # by a component. Long outputs may include surplus, uncited examples.
    if len(cues) > 20:
        referenced = {
            cue_id
            for component in components.values()
            for cue_id in component.get("evidence_cue_ids", [])
            if isinstance(cue_id, str)
        }
        excess = len(cues) - 20
        removable = [
            cue.get("cue_id")
            for cue in reversed(cues)
            if cue.get("cue_id") not in referenced
        ][:excess]
        if len(removable) == excess:
            removed = set(removable)
            corrected["frame_cues"] = [
                cue for cue in cues if cue.get("cue_id") not in removed
            ]
            log.append(
                {
                    "field": "frame_cues",
                    "method": "DROP_UNCITED_CUES_TO_FROZEN_CAPACITY",
                    "original_count": len(cues),
                    "normalized_count": len(corrected["frame_cues"]),
                    "dropped_ids": list(reversed(removable)),
                }
            )
            cues = corrected["frame_cues"]

    # In unusually dense articles the model may cite every extracted cue and
    # the F5 representation can add one duplicate, leaving 21 cues.  Remove a
    # cited cue only when every component that cites it retains another cue of
    # its required function.  This changes neither source text nor scores and
    # is safer than paying for another generative rewrite of a fixed capacity
    # rule.
    while len(cues) > 20:
        cue_by_id = {
            cue.get("cue_id"): cue
            for cue in cues
            if isinstance(cue, dict) and isinstance(cue.get("cue_id"), str)
        }
        removable_id = None
        for cue in reversed(cues):
            cue_id = cue.get("cue_id")
            if not isinstance(cue_id, str):
                continue
            safe = True
            cited = False
            for code, component in components.items():
                refs = component.get("evidence_cue_ids", [])
                if cue_id not in refs:
                    continue
                cited = True
                remaining = [ref for ref in refs if ref != cue_id and ref in cue_by_id]
                expected = FUNCTION_BY_COMPONENT.get(code)
                if not remaining or not any(
                    cue_by_id[ref].get("function") == expected for ref in remaining
                ):
                    safe = False
                    break
            if safe and cited:
                removable_id = cue_id
                break
        if removable_id is None:
            break
        for component in components.values():
            component["evidence_cue_ids"] = [
                ref
                for ref in component.get("evidence_cue_ids", [])
                if ref != removable_id
            ]
        cues = [cue for cue in cues if cue.get("cue_id") != removable_id]
        corrected["frame_cues"] = cues
        log.append(
            {
                "field": "frame_cues",
                "method": "DROP_REDUNDANT_REFERENCED_CUE_TO_FROZEN_CAPACITY",
                "dropped_id": removable_id,
                "normalized_count": len(cues),
                "scores_unchanged": True,
            }
        )

    # Scores 3-4 on moral evaluation or treatment direction require prominent
    # or repeated structural evidence. If a model persists with a high score
    # after selecting only limited evidence, apply the contract's conservative
    # ceiling of 2. This is deterministic, auditable, and avoids spending an
    # additional model call on a rule whose outcome is already fixed.
    for code in ("F3", "F4"):
        component = components.get(code, {})
        if not isinstance(component.get("score"), int) or component["score"] < 3:
            continue
        evidence = [
            cue_by_id[cue_id]
            for cue_id in component.get("evidence_cue_ids", [])
            if cue_id in cue_by_id
        ]
        editorial = [
            cue for cue in evidence if cue.get("owner") in EDITORIALLY_RELEVANT_OWNERS
        ]
        prominent = any(cue.get("location") in PROMINENT_LOCATIONS for cue in editorial)
        repeated = len(editorial) >= 2
        repeated_structural_quote_selection = sum(
            cue.get("owner") == "ATTRIBUTED_ACTOR"
            and cue.get("device") == "QUOTE_SELECTION"
            for cue in evidence
        ) >= 2
        attributed = [
            cue for cue in evidence if cue.get("owner") == "ATTRIBUTED_ACTOR"
        ]
        prominent_attributed = any(
            cue.get("location") in PROMINENT_LOCATIONS for cue in attributed
        )
        repeated_attributed_structure = len(attributed) >= 3 or (
            prominent_attributed and len(attributed) >= 2
        )
        if any(
            (
                prominent,
                repeated,
                repeated_structural_quote_selection,
                repeated_attributed_structure,
            )
        ):
            continue
        original_score = component["score"]
        original_rationale = component.get("rationale_he", "")
        suffix = " הציון הוגבל ל־2: אין בראיות בולטות או חזרה מבנית הנדרשות לציון 3–4."
        component["score"] = 2
        component["rationale_he"] = (
            original_rationale[: max(1, rationale_limit - len(suffix))].rstrip()
            + suffix
        )
        log.append(
            {
                "field": f"components.{code}.score",
                "method": "CAP_HIGH_SCORE_WITHOUT_QUALIFYING_PROMINENCE",
                "original_score": original_score,
                "normalized_score": 2,
                "original_rationale_hash": sha256(
                    original_rationale.encode()
                ).hexdigest(),
            }
        )

    review = corrected.get("needs_review", {})
    if (
        review.get("required") is True
        and review.get("reasons")
        and not review.get("note_he")
    ):
        reasons = ", ".join(str(item) for item in review["reasons"])
        review["note_he"] = f"נדרש עיון נוסף בשל דגלי הבדיקה הבאים: {reasons}."
        log.append(
            {
                "field": "needs_review.note_he",
                "method": "INJECT_REVIEW_NOTE_FROM_MODEL_REASONS",
                "reasons": list(review["reasons"]),
            }
        )

    # Early live calls exposed a now-corrected schema contradiction: a zero
    # component was forced to cite positive evidence. Remove only the exact
    # machine-generated review flag that names that obsolete minItems defect;
    # substantive review reasons are never cleared here.
    note = review.get("note_he")
    component_review_reasons = [
        reason
        for component in components.values()
        for reason in component.get("needs_review_reasons", [])
    ]
    if (
        review.get("required") is True
        and review.get("reasons") == ["OTHER"]
        and isinstance(note, str)
        and "minItems" in note
        and not component_review_reasons
    ):
        review["required"] = False
        review["reasons"] = []
        review["note_he"] = None
        corrected["analysis_status"] = "COMPLETE"
        log.append(
            {
                "field": "needs_review",
                "method": "CLEAR_OBSOLETE_ZERO_EVIDENCE_SCHEMA_CONTRADICTION",
                "original_note_hash": sha256(note.encode()).hexdigest(),
            }
        )
    return corrected, log


def validate_frame_measurement(
    raw: dict[str, Any],
    *,
    schema: dict[str, Any],
    document: dict[str, Any],
) -> tuple[FrameMeasurement | None, list[str]]:
    errors = [
        f"schema:{error.json_path}:{error.message}"
        for error in Draft202012Validator(schema).iter_errors(raw)
    ]
    if errors:
        return None, sorted(errors)
    try:
        measurement = FrameMeasurement.model_validate(raw)
    except Exception as exc:
        return None, [f"pydantic:{type(exc).__name__}:{exc}"]

    if measurement.case_id != document.get("case_id", measurement.case_id):
        errors.append("metadata:case_id mismatch")
    if measurement.document_id != document.get("document_id"):
        errors.append("metadata:document_id mismatch")
    if measurement.language != document.get("language"):
        errors.append("metadata:language mismatch")

    cue_ids = [cue.cue_id for cue in measurement.frame_cues]
    if len(cue_ids) != len(set(cue_ids)):
        errors.append("cues:cue_id values must be unique")
    cues = {cue.cue_id: cue for cue in measurement.frame_cues}
    searchable = "\n".join(
        value for value in (document.get("title", ""), document.get("text", "")) if value
    )
    for cue in measurement.frame_cues:
        if cue.quote_original not in searchable:
            errors.append(f"cues:{cue.cue_id}:exact evidence span not found")
        if cue.location == "HEADLINE" and cue.quote_original not in document.get(
            "title", ""
        ):
            errors.append(f"cues:{cue.cue_id}:HEADLINE evidence not found in title")
        if (
            cue.owner == "HEADLINE_SELECTED_QUOTE"
            and cue.location not in {"HEADLINE", "SUBHEADLINE"}
        ):
            errors.append(
                f"cues:{cue.cue_id}:HEADLINE_SELECTED_QUOTE has incompatible location"
            )
        if measurement.language == "he" and cue.reviewer_translation_he is not None:
            errors.append(f"cues:{cue.cue_id}:Hebrew evidence translation must be null")
        if measurement.language != "he" and not (
            cue.reviewer_translation_he and cue.reviewer_translation_he.strip()
        ):
            errors.append(f"cues:{cue.cue_id}:non-Hebrew evidence requires translation")

    for code in COMPONENTS:
        component = getattr(measurement.components, code)
        unknown = set(component.evidence_cue_ids) - cues.keys()
        if unknown:
            errors.append(f"components:{code}:unknown cue IDs {sorted(unknown)}")
            continue
        evidence = [cues[cue_id] for cue_id in component.evidence_cue_ids]
        expected_function = FUNCTION_BY_COMPONENT[code]
        if component.score > 0 and not any(
            cue.function == expected_function for cue in evidence
        ):
            errors.append(
                f"components:{code}:positive score lacks {expected_function} cue"
            )
        if code in {"F3", "F4"} and component.score >= 3:
            editorial = [
                cue for cue in evidence if cue.owner in EDITORIALLY_RELEVANT_OWNERS
            ]
            prominent = any(cue.location in PROMINENT_LOCATIONS for cue in editorial)
            repeated = len(editorial) >= 2
            repeated_structural_quote_selection = sum(
                cue.owner == "ATTRIBUTED_ACTOR"
                and cue.device == "QUOTE_SELECTION"
                for cue in evidence
            ) >= 2
            attributed = [
                cue for cue in evidence if cue.owner == "ATTRIBUTED_ACTOR"
            ]
            prominent_attributed = any(
                cue.location in PROMINENT_LOCATIONS for cue in attributed
            )
            repeated_attributed_structure = len(attributed) >= 3 or (
                prominent_attributed and len(attributed) >= 2
            )
            if not (
                prominent
                or repeated
                or repeated_structural_quote_selection
                or repeated_attributed_structure
            ):
                errors.append(
                    f"components:{code}:score 3-4 lacks prominent or repeated editorial evidence"
                )
        unresolved = set(component.needs_review_reasons)
        if unresolved - set(measurement.needs_review.reasons):
            errors.append(
                f"components:{code}:component review reasons missing from document routing"
            )
        if component.confidence == "LOW" and (
            "LOW_CONFIDENCE" not in component.needs_review_reasons
            or "LOW_CONFIDENCE" not in measurement.needs_review.reasons
        ):
            errors.append(
                f"components:{code}:LOW confidence must route LOW_CONFIDENCE review"
            )

    if any(cue.owner == "UNCLEAR" for cue in measurement.frame_cues) and (
        "SOURCE_VOICE_AMBIGUITY" not in measurement.needs_review.reasons
    ):
        errors.append("cues:UNCLEAR ownership must route SOURCE_VOICE_AMBIGUITY")

    if measurement.coverage_scope == "INCOMPLETE_SOURCE" and (
        "SOURCE_INCOMPLETE" not in measurement.needs_review.reasons
    ):
        errors.append("coverage:INCOMPLETE_SOURCE must route SOURCE_INCOMPLETE")

    return (None if errors else measurement), errors


def build_score_gap_review(
    first: FrameMeasurement, second: FrameMeasurement
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    review_reasons: set[str] = set(second.needs_review.reasons)
    for code in COMPONENTS:
        first_component = getattr(first.components, code)
        second_component = getattr(second.components, code)
        gap = abs(first_component.score - second_component.score)
        escalated = gap >= 2
        if escalated:
            review_reasons.add("SCORE_GAP")
        components[code] = {
            "first_score": first_component.score,
            "second_score": second_component.score,
            "gap": gap,
            "requires_human_review": escalated,
            "provisional_score": None if escalated else second_component.score,
        }
    return {
        "document_id": first.document_id,
        "requires_human_review": bool(review_reasons),
        "review_reasons": sorted(review_reasons),
        "components": components,
    }
