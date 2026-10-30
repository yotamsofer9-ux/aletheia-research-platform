from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .case_discovery import (
    ClusterAnnotation,
    EventCluster,
    EventLead,
    SampleComposition,
    cluster_ids_to_leads,
)


ANNOTATION_PROTOCOL_VERSION = "case-candidate-annotation-v1.1"

SYSTEM_INSTRUCTION = """You are a bounded academic event-cluster annotator for Aletheia.
The user payload contains only public discovery records already collected by the system. Summarize and classify that observed cluster; never introduce an event, source, URL, fact, responsibility finding, consequence, or date absent from the supplied records. The initial responsibility and clarity fields are sample-balance labels, not findings of truth. Use neutral wording and preserve uncertainty. Do not score media performance and never emit T, N, A, truth, narrative-pressure, or Aletheia scores. An external-indirect event requires a documented material effect on Israel beyond local media attention. Mark a case unresolved only when at least two plausible hypotheses remain; list them. The supplied bundle may contain more than one contemporaneous story. Select one coherent event and cite only records that describe that same event. If no coherent event is supported by two discovery providers and two source domains, by at least four source domains across two languages, or by at least six independent source domains, set is_coherent_event to false, explain briefly in exclusion_reason, and do not force an annotation.

This discovery pass is restricted to canonical turning points, not merely coherent news events. A turning point must visibly alter at least one of these dimensions in the supplied records: MILITARY_PHASE, ACTOR_LEADERSHIP, DIPLOMATIC_ORDER, LEGAL_ORDER, HUMANITARIAN_ORDER, HOSTAGE_OR_CEASEFIRE_REGIME, or REGIONAL_ESCALATION. A routine strike, casualty update, official quotation, threat, or duplicated wire story is not a turning point merely because it generated many headlines. Set is_canonical_turning_point to false unless the observed records themselves support a specific change in state; explain the exclusion. Do not infer long-term impact from general knowledge.

For an eligible event, the Hebrew title must name the concrete dated event itself. Do not title the candidate after commentary, expectations, warnings, or assessments about the event when the supplied records identify the underlying action.

Return JSON only and cite observed lead IDs exactly. Cite a coherent evidence set that satisfies the same independence rule above. Propose concise search terms in Hebrew, English and Arabic for later article-basket discovery."""

ALLOWED_RESPONSE_FIELDS = {
    "title",
    "neutral_summary",
    "scope_relation",
    "responsibility_class",
    "evidence_clarity",
    "war_nexus_rationale",
    "material_impact_on_israel",
    "observed_lead_ids",
    "proposed_keywords",
    "unresolved_hypotheses",
    "is_coherent_event",
    "is_canonical_turning_point",
    "turning_point_rationale",
    "transformative_dimensions",
    "exclusion_reason",
}
OPTIONAL_RESPONSE_FIELDS = {"unresolved_hypotheses"}


class CandidateAnnotationError(ValueError):
    pass


@dataclass(frozen=True)
class AnnotationResult:
    annotation: ClusterAnnotation
    model_name: str
    protocol_version: str
    usage: Mapping[str, int | None]


def build_annotation_prompt(
    cluster: EventCluster,
    leads: Sequence[EventLead],
    composition: SampleComposition,
) -> dict[str, Any]:
    members = cluster_ids_to_leads(cluster, leads)
    observed = [
        {
            "lead_id": lead.lead_id,
            "provider_id": lead.provider_id,
            "source_name": lead.source_name,
            "source_domain": lead.source_domain,
            "title": lead.title[:300],
            "summary": lead.summary[:500],
            "published_at": lead.published_at,
            "language": lead.language,
            "canonical_url": lead.canonical_url,
        }
        for lead in members
    ]
    return {
        "protocol_version": ANNOTATION_PROTOCOL_VERSION,
        "task": "Summarize and classify only this observed event cluster.",
        "cluster": asdict(cluster),
        "observed_leads": observed,
        "current_sample_composition": asdict(composition),
        "output_contract": {
            "title": "neutral Hebrew title naming the concrete dated event",
            "neutral_summary": "concise Hebrew summary grounded only in observed leads",
            "scope_relation": "CORE | DIRECT_SPILLOVER | EXTERNAL_INDIRECT | PREWAR_FORMATIVE",
            "responsibility_class": "ISRAEL | NON_ISRAEL_ACTOR | SHARED_OR_CHAINED | UNRESOLVED | PROVISIONAL",
            "evidence_clarity": "HIGH | PARTIAL | UNRESOLVED",
            "war_nexus_rationale": "grounded Hebrew rationale",
            "material_impact_on_israel": "boolean",
            "observed_lead_ids": ["enough supplied lead IDs to satisfy the independence rule"],
            "proposed_keywords": {
                "he": ["Hebrew terms ordered from exact event names to broader context"],
                "en": ["English terms ordered from exact event names to broader context"],
                "ar": ["Arabic terms ordered from exact event names to broader context"],
            },
            "unresolved_hypotheses": [
                "at least two only when evidence_clarity is UNRESOLVED"
            ],
            "is_coherent_event": "boolean; false when no single event satisfies the independence rule",
            "is_canonical_turning_point": "boolean; true only for an observed change in state",
            "turning_point_rationale": "concise Hebrew rationale grounded only in supplied records",
            "transformative_dimensions": [
                "one or more of MILITARY_PHASE | ACTOR_LEADERSHIP | DIPLOMATIC_ORDER | LEGAL_ORDER | HUMANITARIAN_ORDER | HOSTAGE_OR_CEASEFIRE_REGIME | REGIONAL_ESCALATION"
            ],
            "exclusion_reason": "null only for a coherent canonical turning point; otherwise a concise reason",
        },
    }


def validate_annotation_response(
    raw: Mapping[str, Any],
    cluster: EventCluster,
    leads: Sequence[EventLead],
) -> ClusterAnnotation:
    extra = set(raw) - ALLOWED_RESPONSE_FIELDS
    missing = (ALLOWED_RESPONSE_FIELDS - OPTIONAL_RESPONSE_FIELDS) - set(raw)
    if extra:
        raise CandidateAnnotationError(f"unexpected annotation fields: {sorted(extra)}")
    if missing:
        raise CandidateAnnotationError(f"missing annotation fields: {sorted(missing)}")
    if not isinstance(raw["is_coherent_event"], bool):
        raise CandidateAnnotationError("is_coherent_event must be boolean")
    if not raw["is_coherent_event"]:
        reason = str(raw.get("exclusion_reason") or "").strip()
        raise CandidateAnnotationError(
            "incoherent observed bundle" + (f": {reason}" if reason else "")
        )
    if not isinstance(raw["is_canonical_turning_point"], bool):
        raise CandidateAnnotationError("is_canonical_turning_point must be boolean")
    if not raw["is_canonical_turning_point"]:
        reason = str(raw.get("exclusion_reason") or "").strip()
        raise CandidateAnnotationError(
            "event is not a canonical turning point"
            + (f": {reason}" if reason else "")
        )
    if raw.get("exclusion_reason") not in {None, ""}:
        raise CandidateAnnotationError(
            "eligible annotation cannot contain an exclusion_reason"
        )
    allowed_dimensions = {
        "MILITARY_PHASE",
        "ACTOR_LEADERSHIP",
        "DIPLOMATIC_ORDER",
        "LEGAL_ORDER",
        "HUMANITARIAN_ORDER",
        "HOSTAGE_OR_CEASEFIRE_REGIME",
        "REGIONAL_ESCALATION",
    }
    dimensions = tuple(
        str(value).strip() for value in raw["transformative_dimensions"]
        if str(value).strip()
    )
    if not dimensions or not set(dimensions) <= allowed_dimensions:
        raise CandidateAnnotationError("canonical event needs valid transformative dimensions")
    turning_point_rationale = str(raw["turning_point_rationale"] or "").strip()
    if not turning_point_rationale:
        raise CandidateAnnotationError("canonical event needs a turning-point rationale")
    if raw["scope_relation"] not in {
        "CORE",
        "DIRECT_SPILLOVER",
        "EXTERNAL_INDIRECT",
        "PREWAR_FORMATIVE",
    }:
        raise CandidateAnnotationError("invalid scope_relation")
    if raw["responsibility_class"] not in {
        "ISRAEL",
        "NON_ISRAEL_ACTOR",
        "SHARED_OR_CHAINED",
        "UNRESOLVED",
        "PROVISIONAL",
    }:
        raise CandidateAnnotationError("invalid responsibility_class")
    if raw["evidence_clarity"] not in {"HIGH", "PARTIAL", "UNRESOLVED"}:
        raise CandidateAnnotationError("invalid evidence_clarity")

    member_ids = set(cluster.lead_ids)
    lead_ids = tuple(str(value) for value in raw["observed_lead_ids"])
    if len(set(lead_ids)) < 2 or not set(lead_ids) <= member_ids:
        raise CandidateAnnotationError(
            "annotation must cite at least two supplied observed lead IDs"
        )
    available_ids = {lead.lead_id for lead in leads}
    if not set(lead_ids) <= available_ids:
        raise CandidateAnnotationError("annotation cites a lead missing from discovery")
    selected = [lead for lead in leads if lead.lead_id in set(lead_ids)]
    domains = {lead.source_domain for lead in selected}
    providers = {lead.provider_id for lead in selected}
    languages = {lead.language for lead in selected}
    if len(domains) < 2:
        raise CandidateAnnotationError("coherent event needs two source domains")
    if len(providers) < 2 and not (
        (len(domains) >= 4 and len(languages) >= 2) or len(domains) >= 6
    ):
        raise CandidateAnnotationError(
            "coherent event needs two providers, four domains across two languages, or six domains"
        )

    keywords = raw["proposed_keywords"]
    if not isinstance(keywords, Mapping) or set(keywords) != {"he", "en", "ar"}:
        raise CandidateAnnotationError("proposed_keywords must contain he, en and ar")
    normalized_keywords: dict[str, tuple[str, ...]] = {}
    for language in ("he", "en", "ar"):
        values = tuple(str(value).strip() for value in keywords[language] if str(value).strip())
        if not values:
            raise CandidateAnnotationError(f"missing {language} search terms")
        normalized_keywords[language] = values[:8]

    hypotheses = tuple(
        str(value).strip()
        for value in (raw.get("unresolved_hypotheses") or [])
        if str(value).strip()
    )
    if raw["evidence_clarity"] == "UNRESOLVED" and len(set(hypotheses)) < 2:
        raise CandidateAnnotationError("unresolved annotation needs two hypotheses")
    if raw["scope_relation"] == "EXTERNAL_INDIRECT" and not bool(
        raw["material_impact_on_israel"]
    ):
        raise CandidateAnnotationError(
            "external-indirect annotation lacks material impact on Israel"
        )

    return ClusterAnnotation(
        title=str(raw["title"]).strip(),
        neutral_summary=str(raw["neutral_summary"]).strip(),
        scope_relation=str(raw["scope_relation"]),
        responsibility_class=str(raw["responsibility_class"]),
        evidence_clarity=str(raw["evidence_clarity"]),
        war_nexus_rationale=str(raw["war_nexus_rationale"]).strip(),
        material_impact_on_israel=bool(raw["material_impact_on_israel"]),
        observed_lead_ids=lead_ids,
        proposed_keywords=normalized_keywords,
        unresolved_hypotheses=hypotheses,
        turning_point_rationale=turning_point_rationale,
        transformative_dimensions=dimensions,
    )


class VertexClusterAnnotator:
    """One bounded Vertex call that annotates supplied records but cannot create leads."""

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str = "gemini-2.5-flash",
        client: Any | None = None,
    ):
        self.project = project
        self.location = location
        self.model = model
        self._client = client

    def annotate(
        self,
        cluster: EventCluster,
        leads: Sequence[EventLead],
        composition: SampleComposition,
    ) -> AnnotationResult:
        from google import genai
        from google.genai import types

        client = self._client or genai.Client(
            vertexai=True, project=self.project, location=self.location
        )
        prompt = build_annotation_prompt(cluster, leads, composition)
        response = client.models.generate_content(
            model=self.model,
            contents=json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.0,
                max_output_tokens=2500,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=512, include_thoughts=False
                ),
            ),
        )
        try:
            raw = json.loads(response.text)
        except (TypeError, json.JSONDecodeError) as error:
            raise CandidateAnnotationError("Vertex returned invalid JSON") from error
        annotation = validate_annotation_response(raw, cluster, leads)
        usage = getattr(response, "usage_metadata", None)
        return AnnotationResult(
            annotation=annotation,
            model_name=self.model,
            protocol_version=ANNOTATION_PROTOCOL_VERSION,
            usage={
                "prompt_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
                "total_tokens": getattr(usage, "total_token_count", None),
            },
        )
