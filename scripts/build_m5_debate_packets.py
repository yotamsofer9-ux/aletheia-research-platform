#!/usr/bin/env python3
"""Generate bounded Hebrew courtroom debate packets for one M5 batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from google import genai
from google.genai import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.judgment_room import SCORE_LADDERS  # noqa: E402

MEASUREMENTS = ROOT / "analysis/runs/case001_article_measurements_v1.jsonl"
CORPUS = ROOT / "exports/case001_research_pilot_27.csv"
PROMPT_VERSION = "case001-article-analysis-v1.0.12"
MODEL = "gemini-2.5-flash"
PROJECT = os.getenv("GCP_PROJECT_ID")
LOCATION = "us-central1"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _latest_measurements(
    measurements_path: Path = MEASUREMENTS,
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(measurements_path):
        if row.get("status") == "SUCCESS" and row.get("prompt_version") == prompt_version:
            latest[row["document_id"]] = row
    return latest


def _corpus_rows(corpus_path: Path = CORPUS) -> dict[str, dict[str, str]]:
    import csv

    with corpus_path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["document_id"]: row for row in csv.DictReader(handle)}


def _dossiers(
    queue: dict[str, Any],
    *,
    measurements_path: Path = MEASUREMENTS,
    corpus_path: Path = CORPUS,
    prompt_version: str = PROMPT_VERSION,
    case_id: str = "CASE-001",
) -> list[dict[str, Any]]:
    measurements = _latest_measurements(measurements_path, prompt_version)
    corpus = _corpus_rows(corpus_path)
    dossiers: list[dict[str, Any]] = []
    for document in queue["documents"]:
        record = measurements[document["document_id"]]
        article = corpus[document["document_id"]]
        claims = record["measurement"].get("relevant_claims", [])
        for component in document["components"]:
            if component["decision"] != "HUMAN_REVIEW":
                continue
            decisive = component.get("evidence_span")
            dossiers.append(
                {
                    "dilemma_id": f"{case_id}:{document['document_id']}:{component['component']}",
                    "document_id": document["document_id"],
                    "component": component["component"],
                    "source_name": article["source_name"],
                    "language": document["language"],
                    "title": article["title"],
                    "published_at": article["published_at"],
                    "routing_reasons": component["reasons"],
                    "score_ladder": [
                        {"score": score, "meaning_he": meaning}
                        for score, meaning in SCORE_LADDERS[component["component"]]
                    ],
                    "model_position": {
                        "score": component.get("score"),
                        "confidence": component.get("confidence"),
                        "rationale": component.get("rationale"),
                        "evidence_span": decisive,
                        "anchor_refs": component.get("anchor_refs", []),
                    },
                    "eligible_exact_exhibits": [
                        {
                            "quote_original": claim["quote_original"],
                            "location": claim["location"],
                        }
                        for claim in claims
                    ],
                    "claim_context": [
                        {
                            key: claim.get(key)
                            for key in (
                                "quote_original",
                                "location",
                                "voice",
                                "speaker",
                                "centrality",
                                "claim_kind",
                                "outlet_stance",
                                "anchor_ids",
                                "stance_toward_anchors",
                                "certainty",
                                "evidence_access",
                            )
                        }
                        for claim in claims
                    ],
                }
            )
    return dossiers


def _generate(
    dossiers: list[dict[str, Any]],
    model: str,
    *,
    case_anchors: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    system = """You prepare neutral academic adjudication packets for a Hebrew-speaking principal researcher.
Return JSON only. Do not decide the case. For every supplied dilemma, present the strongest defensible alternative to the model position without inventing facts. All argument, question, label, bench-note, translation-note and glossary prose must be in Hebrew. Preserve the distinction between the outlet's own voice and attributed speech. Every dossier includes the authoritative score_ladder for its component; apply it literally. For T1-T3, 4 is the strongest performance and 0 is the gravest failure. For N1-N5, 0 is the absence of the measured narrative problem and 4 is the gravest problem. Never describe accurate alignment as T1=0, and never describe absence of narrative pressure as N=4. A challenge may propose 0-4 or N/A, but must differ from the model position and its label, argument, and score must all agree with the supplied ladder. Use only anchor IDs already present in the dossier. N1 or N3 scores 2-4 require at least one supplied anchor that identifies actual material factual counterevidence; a hypothetical statement that a claim could be challenged is not counterevidence. If no such anchor exists, do not manufacture a positive N1/N3 challenge. The decisive exhibit must equal either the model evidence or one eligible_exact_exhibits quote verbatim, and it must show the claim actually discussed by the challenge. Never paraphrase source exhibits. Translate every non-Hebrew decisive exhibit into Hebrew, preserving uncertainty, attribution, register and geographic meaning. The glossary should contain only terms whose linguistic or geographic nuance matters."""
    prompt = {
        "task": "Build one courtroom record for every dilemma below.",
        "output_contract": {
            "records": [
                {
                    "dilemma_id": "exact input id",
                    "question_he": "neutral question contrasting both positions",
                    "bench_note_he": "what the judge must verify",
                    "challenge_score": "integer 0-4 or null",
                    "challenge_na_reason": "string or null; required only when score is null",
                    "challenge_label_he": "short label",
                    "challenge_argument_he": "specific strongest counterargument",
                    "challenge_anchor_refs": ["only IDs in dossier"],
                    "decisive_exhibit_quote_original": "exact source quote",
                    "decisive_exhibit_location": "HEADLINE, LEAD, or BODY",
                    "curation_note_he": "why this passage is decisive",
                    "translation_he": "Hebrew translation, or null for Hebrew source",
                    "translation_note_he": "meaning/register note, or null for Hebrew source",
                    "glossary": [
                        {
                            "term_he": "term",
                            "source_term": "source term",
                            "explanation_he": "short explanation",
                        }
                    ],
                }
            ]
        },
        "dilemmas": dossiers,
        "case_anchors": case_anchors or [],
    }
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    response = client.models.generate_content(
        model=model,
        contents=json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=12000,
            thinking_config=types.ThinkingConfig(thinking_budget=768, include_thoughts=False),
        ),
    )
    payload = json.loads(response.text)
    usage = getattr(response, "usage_metadata", None)
    return payload, {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }


def _generated_records(generated: Any) -> list[dict[str, Any]]:
    records = generated if isinstance(generated, list) else generated.get("records")
    if not isinstance(records, list):
        raise ValueError("PACKET_RECORDS_MISSING")
    return records


def _normalize_nonverbatim_curated_exhibits(
    generated: Any,
    dossiers: list[dict[str, Any]],
) -> list[str]:
    """Replace a paraphrased curated exhibit with a frozen eligible exact quote."""
    dossier_index = {row["dilemma_id"]: row for row in dossiers}
    normalized: list[str] = []
    for row in _generated_records(generated):
        dilemma_id = row.get("dilemma_id")
        dossier = dossier_index.get(dilemma_id)
        if dossier is None:
            continue
        eligible_rows = dossier.get("eligible_exact_exhibits") or []
        eligible = {
            (item["quote_original"], item["location"]) for item in eligible_rows
        }
        model_exhibit = dossier["model_position"].get("evidence_span")
        if model_exhibit:
            eligible.add(
                (model_exhibit["quote_original"], model_exhibit["location"])
            )
        supplied = (
            row.get("decisive_exhibit_quote_original"),
            row.get("decisive_exhibit_location"),
        )
        if supplied in eligible:
            continue
        if model_exhibit:
            fallback = model_exhibit
        elif eligible_rows:
            fallback = eligible_rows[0]
        else:
            raise ValueError(f"EXHIBIT_NOT_VERBATIM:{dilemma_id}")
        row["decisive_exhibit_quote_original"] = fallback["quote_original"]
        row["decisive_exhibit_location"] = fallback["location"]
        note = row.get("curation_note_he") or ""
        row["curation_note_he"] = (
            f"{note} המערכת החליפה ניסוח לא־מדויק בציטוט המדויק הראשון "
            "מתוך הראיות הקפואות שסיפק מודל הניקוד."
        ).strip()
        if dossier["language"] != "he":
            row["translation_he"] = None
            row["translation_note_he"] = None
            row["glossary"] = []
        normalized.append(dilemma_id)
    return sorted(normalized)


def _repair_missing_translations(
    generated: Any,
    dossiers: list[dict[str, Any]],
    model: str,
) -> tuple[Any, dict[str, Any], list[str]]:
    """Repair translation-only omissions without regenerating debate arguments."""
    records = _generated_records(generated)
    dossier_index = {row["dilemma_id"]: row for row in dossiers}
    missing: list[dict[str, str]] = []
    for row in records:
        dossier = dossier_index.get(row.get("dilemma_id"))
        if dossier is None or dossier["language"] == "he":
            continue
        if isinstance(row.get("translation_he"), str) and row["translation_he"].strip():
            continue
        quote = row.get("decisive_exhibit_quote_original")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError(f"TRANSLATION_REPAIR_SOURCE_MISSING:{row.get('dilemma_id')}")
        missing.append(
            {
                "dilemma_id": row["dilemma_id"],
                "source_language": dossier["language"],
                "source_text": quote,
            }
        )
    if not missing:
        return generated, {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}, []

    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    response = client.models.generate_content(
        model=model,
        contents=json.dumps(
            {
                "task": (
                    "Translate each exact source exhibit into Hebrew. Preserve attribution, "
                    "uncertainty, geographic meaning, political register and dialect nuance."
                ),
                "output_contract": {
                    "records": [
                        {
                            "dilemma_id": "exact input id",
                            "translation_he": "faithful Hebrew translation",
                            "translation_note_he": "brief register or meaning note",
                            "glossary": [
                                {
                                    "term_he": "term",
                                    "source_term": "source term",
                                    "explanation_he": "short linguistic/geographic explanation",
                                }
                            ],
                        }
                    ]
                },
                "records": missing,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a research translation repairer. Return JSON only. Translate only "
                "the supplied exact exhibits; do not alter the debate, score or source text."
            ),
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=3000,
            thinking_config=types.ThinkingConfig(
                thinking_budget=256, include_thoughts=False
            ),
        ),
    )
    repaired_payload = json.loads(response.text)
    repaired_records = (
        repaired_payload
        if isinstance(repaired_payload, list)
        else repaired_payload.get("records")
    )
    if not isinstance(repaired_records, list):
        raise ValueError("TRANSLATION_REPAIR_RECORDS_MISSING")
    repaired_index = {row.get("dilemma_id"): row for row in repaired_records}
    expected = {row["dilemma_id"] for row in missing}
    if set(repaired_index) != expected:
        raise ValueError("TRANSLATION_REPAIR_KEYS_MISMATCH")
    for row in records:
        if row.get("dilemma_id") not in repaired_index:
            continue
        repair = repaired_index[row["dilemma_id"]]
        translation = repair.get("translation_he")
        if not isinstance(translation, str) or not translation.strip():
            raise ValueError(f"TRANSLATION_REPAIR_EMPTY:{row['dilemma_id']}")
        row["translation_he"] = translation
        row["translation_note_he"] = repair.get("translation_note_he") or ""
        row["glossary"] = repair.get("glossary") or []
    response_usage = getattr(response, "usage_metadata", None)
    return generated, {
        "prompt_tokens": getattr(response_usage, "prompt_token_count", None),
        "output_tokens": getattr(response_usage, "candidates_token_count", None),
        "total_tokens": getattr(response_usage, "total_token_count", None),
    }, sorted(expected)


def _validate_and_materialize(
    queue: dict[str, Any],
    dossiers: list[dict[str, Any]],
    generated: Any,
    *,
    case_id: str = "CASE-001",
    allowed_case_anchor_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    dossier_index = {row["dilemma_id"]: row for row in dossiers}
    records = _generated_records(generated)
    generated_index = {row.get("dilemma_id"): row for row in records}
    if set(generated_index) != set(dossier_index) or len(records) != len(generated_index):
        raise ValueError("PACKET_DILEMMA_KEYS_MISMATCH")

    dilemmas = []
    resolved_without_dispute = []
    translations = []
    translation_keys: set[tuple[str, str]] = set()
    for dilemma_id, dossier in dossier_index.items():
        row = generated_index[dilemma_id]
        model = dossier["model_position"]
        challenge_score = row.get("challenge_score")
        challenge_na_reason = row.get("challenge_na_reason")
        if isinstance(challenge_score, str) and challenge_score.strip().upper() in {
            "N/A",
            "NA",
        }:
            # The contract represents N/A as JSON null plus a reason. Preserve
            # the raw candidate for audit, but normalize this harmless model
            # serialization variant before deterministic validation.
            challenge_score = None
        elif isinstance(challenge_score, str) and challenge_score.strip() in {
            "0",
            "1",
            "2",
            "3",
            "4",
        }:
            # Vertex occasionally serializes an otherwise schema-valid integer
            # as a JSON string. Keep the saved candidate untouched for audit and
            # normalize only the materialized value.
            challenge_score = int(challenge_score.strip())
        if challenge_score is not None and (
            not isinstance(challenge_score, int) or not 0 <= challenge_score <= 4
        ):
            raise ValueError(f"INVALID_CHALLENGE_SCORE:{dilemma_id}")
        if challenge_score is None and not challenge_na_reason:
            raise ValueError(f"MISSING_CHALLENGE_NA_REASON:{dilemma_id}")
        if challenge_score == model.get("score") and not (
            challenge_score is None and challenge_na_reason != model.get("na_reason")
        ):
            resolved_without_dispute.append({
                "dilemma_id": dilemma_id,
                "document_id": dossier["document_id"],
                "component": dossier["component"],
                "score": challenge_score,
                "resolution": "METHODOLOGICAL_REVIEW_CONFIRMED_MODEL_SCORE",
                "review_rationale_he": row["challenge_argument_he"],
            })
            continue
        allowed_anchors = {
            anchor
            for claim in dossier["claim_context"]
            for anchor in claim.get("anchor_ids", [])
        } | set(model.get("anchor_refs", [])) | set(allowed_case_anchor_ids or set())
        challenge_anchors = row.get("challenge_anchor_refs") or []
        if not set(challenge_anchors) <= allowed_anchors:
            raise ValueError(f"UNKNOWN_CHALLENGE_ANCHOR:{dilemma_id}")
        if (
            dossier["component"] in {"N1", "N3"}
            and isinstance(challenge_score, int)
            and challenge_score >= 2
            and not challenge_anchors
        ):
            resolved_without_dispute.append({
                "dilemma_id": dilemma_id,
                "document_id": dossier["document_id"],
                "component": dossier["component"],
                "score": model.get("score"),
                "na_reason": model.get("na_reason"),
                "resolution": "UNGROUNDED_COUNTEREVIDENCE_CHALLENGE_REJECTED",
                "review_rationale_he": (
                    "ההשגה נדחתה לפי ספר החוקים: ציון 2–4 ב־N1 או N3 "
                    "מחייב עוגן המזהה ראיית־נגד עובדתית מהותית בפועל."
                ),
            })
            continue

        model_exhibit = model.get("evidence_span")
        exhibit_quote = row.get("decisive_exhibit_quote_original")
        exhibit_location = row.get("decisive_exhibit_location")
        eligible = {
            (item["quote_original"], item["location"])
            for item in dossier["eligible_exact_exhibits"]
        }
        if model_exhibit:
            eligible.add(
                (model_exhibit["quote_original"], model_exhibit["location"])
            )
        if (exhibit_quote, exhibit_location) not in eligible:
            raise ValueError(f"EXHIBIT_NOT_VERBATIM:{dilemma_id}")
        curated_exhibit = None
        if not model_exhibit or (
            exhibit_quote != model_exhibit["quote_original"]
            or exhibit_location != model_exhibit["location"]
        ):
            curated_exhibit = {
                "quote_original": exhibit_quote,
                "location": exhibit_location,
                "curation_note_he": row["curation_note_he"],
            }
        challenge = {
            "score": challenge_score,
            "na_reason": challenge_na_reason if challenge_score is None else None,
            "label_he": row["challenge_label_he"],
            "argument_he": row["challenge_argument_he"],
            "anchor_refs": challenge_anchors,
        }
        dilemma = {
            "dilemma_id": dilemma_id,
            "document_id": dossier["document_id"],
            "component": dossier["component"],
            "issue_tags": dossier["routing_reasons"],
            "question_he": row["question_he"],
            "bench_note_he": row["bench_note_he"],
            "challenge": challenge,
        }
        if curated_exhibit:
            dilemma["curated_exhibit"] = curated_exhibit
        dilemmas.append(dilemma)

        if dossier["language"] != "he":
            translation = row.get("translation_he")
            if not isinstance(translation, str) or not translation.strip():
                raise ValueError(f"MISSING_HEBREW_TRANSLATION:{dilemma_id}")
            translation_key = (dossier["document_id"], exhibit_quote)
            if translation_key not in translation_keys:
                translations.append(
                {
                    "translation_id": f"{dilemma_id}:HE",
                    "document_id": dossier["document_id"],
                    "source_language": dossier["language"],
                    "target_language": "he",
                    "source_text": exhibit_quote,
                    "translation_he": translation,
                    "review_status": "MODEL_DRAFT_FOR_HUMAN_ADJUDICATION",
                    "translation_note_he": row.get("translation_note_he") or "",
                    "glossary": row.get("glossary") or [],
                }
                )
                translation_keys.add(translation_key)

    packet = {
        "schema_version": "1.0",
        "case_id": case_id,
        "status": "PENDING_PRINCIPAL_RESEARCHER_DECISIONS",
        "purpose": (
            f"{queue.get('batch_id', case_id)} automatically generated "
            "hard-edge debate packets."
        ),
        "prompt_version": queue["prompt_version"],
        "acceptance_policy_version": queue["policy_version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dilemmas": dilemmas,
        "resolved_without_dispute": resolved_without_dispute,
    }
    translation_packet = {
        "schema_version": "1.0",
        "case_id": case_id,
        "target_language": "he",
        "translation_policy": "FUNCTIONAL_MEANING_WITH_EXPLICIT_REGISTER_AND_GEOGRAPHIC_NOTES",
        "records": translations,
    }
    return packet, translation_packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-number", type=int, choices=range(5, 10))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--recover-candidate", action="store_true")
    parser.add_argument("--case-id", default="CASE-001")
    parser.add_argument("--prompt-version", default=PROMPT_VERSION)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--measurements", type=Path, default=MEASUREMENTS)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--dilemmas-output", type=Path)
    parser.add_argument("--translations-output", type=Path)
    parser.add_argument("--anchors", type=Path)
    args = parser.parse_args()
    if args.batch_number is None and args.queue is None:
        parser.error("provide --batch-number for CASE-001 or --queue for a case workspace")
    number = args.batch_number
    queue_path = args.queue or (
        ROOT / f"analysis/m5/case001_m5_b{number:02d}_review_queue_v3.json"
    )
    dilemma_path = args.dilemmas_output or (
        ROOT / f"calibration/case001_judgment_dilemmas_v1_m5_b{number:02d}.json"
    )
    translation_path = args.translations_output or (
        ROOT / f"calibration/case001_evidence_translations_he_v1_m5_b{number:02d}.json"
    )
    queue = _read_json(queue_path)
    dossiers = _dossiers(
        queue,
        measurements_path=args.measurements,
        corpus_path=args.corpus,
        prompt_version=args.prompt_version,
        case_id=args.case_id,
    )
    case_anchors = _read_jsonl(args.anchors) if args.anchors else []
    if case_anchors and any(row.get("case_id") != args.case_id for row in case_anchors):
        raise ValueError("CASE_ANCHOR_CASE_ID_MISMATCH")
    compact_anchors = [
        {
            "anchor_id": row["anchor_id"],
            "canonical_proposition": row["canonical_proposition"],
            "scope_notes": row.get("scope_notes"),
            "assessments": [
                {
                    "as_of": assessment.get("as_of"),
                    "epistemic_status": assessment.get("epistemic_status"),
                    "basis_summary": assessment.get("basis_summary"),
                }
                for assessment in row.get("assessments", [])
            ],
        }
        for row in case_anchors
    ]
    candidate_path = args.candidate or (
        ROOT / f"analysis/m5/case001_m5_b{number:02d}_debate_candidate_v1.json"
    )
    if args.recover_candidate:
        candidate = _read_json(candidate_path)
        generated, usage = candidate["generated"], candidate["usage"]
    else:
        generated, usage = _generate(
            dossiers, args.model, case_anchors=compact_anchors
        )
        _write_json(candidate_path, {"generated": generated, "usage": usage})
    normalized_exhibits = _normalize_nonverbatim_curated_exhibits(
        generated, dossiers
    )
    generated, repair_usage, repaired_ids = _repair_missing_translations(
        generated, dossiers, args.model
    )
    if repaired_ids or normalized_exhibits:
        usage = {
            key: int(usage.get(key) or 0) + int(repair_usage.get(key) or 0)
            for key in ("prompt_tokens", "output_tokens", "total_tokens")
        }
        _write_json(
            candidate_path,
            {
                "generated": generated,
                "usage": usage,
                "translation_repairs": {
                    "dilemma_ids": repaired_ids,
                    "usage": repair_usage,
                },
                "exhibit_normalizations": {
                    "dilemma_ids": normalized_exhibits,
                    "method": "FIRST_FROZEN_MODEL_SELECTED_EXACT_QUOTE",
                },
            },
        )
    packet, translations = _validate_and_materialize(
        queue,
        dossiers,
        generated,
        case_id=args.case_id,
        allowed_case_anchor_ids={row["anchor_id"] for row in case_anchors},
    )
    _write_json(dilemma_path, packet)
    _write_json(translation_path, translations)
    print(
        json.dumps(
            {
                "batch_number": number,
                "case_id": args.case_id,
                "dilemmas": len(packet["dilemmas"]),
                "translations": len(translations["records"]),
                "usage": usage,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
