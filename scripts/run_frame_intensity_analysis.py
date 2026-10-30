#!/usr/bin/env python3
"""Run the bounded first-pass frame-intensity calibration for CASE-002."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google import genai
from google.genai import types

from aletheia_lite.frame_intensity import (
    METHODOLOGY_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    file_hash,
    frame_score,
    load_schema,
    normalize_frame_output_limits,
    resolve_verbatim_cues,
    stable_hash,
    validate_frame_measurement,
)


CASE_ID = "CASE-002"
CORPUS_PATH = ROOT / "data" / "cases" / "case-002" / "analysis" / "corpus.csv"
ROSTER_PATH = (
    ROOT
    / "data"
    / "cases"
    / "case-002"
    / "analysis"
    / "frame_calibration_roster_v1.json"
)
SCHEMA_PATH = ROOT / "measurement" / "frame_intensity_schema.json"
CONTRACT_PATH = ROOT / "measurement" / "FRAME_INTENSITY_CONTRACT.md"
RULEBOOK_PATH = ROOT / "measurement" / "FRAME_SYSTEM_RULEBOOK.md"
ADAPTER_DIR = ROOT / "measurement" / "frame_language_adapters"
BUDGET_PATH = ROOT / "analysis" / "frame_intensity_budgets_v1.json"
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "cases"
    / "case-002"
    / "analysis"
    / "frame_intensity_calibration_first_pass_v1.jsonl"
)
DEFAULT_SUMMARY = DEFAULT_OUTPUT.with_name(
    "frame_intensity_calibration_first_pass_v1_summary.json"
)
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL = "gemini-2.5-flash"


def generation_response_schema() -> dict[str, Any]:
    """Vertex-compatible structural envelope; strict semantics stay local."""
    review_reasons = {
        "type": "ARRAY",
        "items": {"type": "STRING"},
    }
    cue = {
        "type": "OBJECT",
        "required": [
            "cue_id",
            "quote_original",
            "location",
            "owner",
            "function",
            "device",
            "reviewer_translation_he",
        ],
        "properties": {
            "cue_id": {"type": "STRING"},
            "quote_original": {"type": "STRING"},
            "location": {"type": "STRING"},
            "owner": {"type": "STRING"},
            "function": {"type": "STRING"},
            "device": {"type": "STRING"},
            "reviewer_translation_he": {"type": "STRING", "nullable": True},
        },
    }
    component = {
        "type": "OBJECT",
        "required": [
            "score",
            "confidence",
            "rationale_he",
            "evidence_cue_ids",
            "needs_review_reasons",
        ],
        "properties": {
            "score": {"type": "INTEGER"},
            "confidence": {"type": "STRING"},
            "rationale_he": {"type": "STRING"},
            "evidence_cue_ids": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "needs_review_reasons": review_reasons,
        },
    }
    required = [
        "schema_version",
        "methodology_version",
        "prompt_version",
        "case_id",
        "document_id",
        "language",
        "analysis_status",
        "coverage_scope",
        "frame_statement_he",
        "frame_families",
        "frame_cues",
        "components",
        "needs_review",
    ]
    return {
        "type": "OBJECT",
        "required": required,
        "properties": {
            "schema_version": {"type": "STRING"},
            "methodology_version": {"type": "STRING"},
            "prompt_version": {"type": "STRING"},
            "case_id": {"type": "STRING"},
            "document_id": {"type": "STRING"},
            "language": {"type": "STRING"},
            "analysis_status": {"type": "STRING"},
            "coverage_scope": {"type": "STRING"},
            "frame_statement_he": {"type": "STRING"},
            "frame_families": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "frame_cues": {"type": "ARRAY", "items": cue},
            "components": {
                "type": "OBJECT",
                "required": ["F1", "F2", "F3", "F4", "F5"],
                "properties": {code: component for code in ("F1", "F2", "F3", "F4", "F5")},
            },
            "needs_review": {
                "type": "OBJECT",
                "required": ["required", "reasons", "note_he"],
                "properties": {
                    "required": {"type": "BOOLEAN"},
                    "reasons": review_reasons,
                    "note_he": {"type": "STRING", "nullable": True},
                },
            },
        },
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_corpus(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return {row["document_id"]: row for row in csv.DictReader(handle)}


def current_rss_mb() -> float | None:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            text=True,
            timeout=2,
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


def response_usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def cached_successes(path: Path) -> set[tuple[str, str, str]]:
    return {
        (row["document_id"], row["input_hash"], row["model"])
        for row in load_jsonl(path)
        if row.get("status") == "SUCCESS"
    }


def normalize_fixed_metadata(
    raw: dict[str, Any], document: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = dict(raw)
    expected = {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "prompt_version": PROMPT_VERSION,
        "case_id": CASE_ID,
        "document_id": document["document_id"],
        "language": document["language"],
    }
    corrections = []
    for field, value in expected.items():
        if normalized.get(field) != value:
            corrections.append(
                {
                    "field": field,
                    "model_value": normalized.get(field),
                    "runner_value": value,
                    "method": "INJECT_FROZEN_INPUT_METADATA",
                }
            )
            normalized[field] = value
    return normalized, corrections


def build_bundle(
    document: dict[str, str], *, adapter: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_fields = (
        "document_id",
        "source_id",
        "source_name",
        "group_name",
        "language",
        "publisher_slot",
        "editorial_profile",
        "substitute_for",
        "url",
        "title",
        "published_at",
        "sample_phase",
        "content_hash",
        "snapshot_hash",
        "text",
    )
    bundle = {
        "case_id": CASE_ID,
        "measurement_task": "DESCRIPTIVE_FRAME_INTENSITY_FIRST_PASS",
        "document": {key: document.get(key, "") for key in selected_fields},
        "language_adapter": adapter,
    }
    provenance = {
        "document_content_hash": document["content_hash"],
        "snapshot_hash": document["snapshot_hash"],
        "schema_hash": file_hash(SCHEMA_PATH),
        "contract_hash": file_hash(CONTRACT_PATH),
        "rulebook_hash": file_hash(RULEBOOK_PATH),
        "adapter_hash": stable_hash(adapter),
        "budget_hash": file_hash(BUDGET_PATH),
    }
    return bundle, provenance


def build_prompt(bundle: dict[str, Any], schema: dict[str, Any]) -> str:
    return (
        "Analyze the frozen article in its original language. First extract exact "
        "frame cues, assign ownership and function, and only then score F1-F5. "
        "Return one complete JSON object and no prose.\n\n"
        "FROZEN ANALYSIS BUNDLE:\n"
        + json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        + "\n\nOUTPUT JSON SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def system_instruction(adapter: dict[str, Any]) -> str:
    return (
        RULEBOOK_PATH.read_text(encoding="utf-8")
        + "\n\nFROZEN MEASUREMENT CONTRACT:\n"
        + CONTRACT_PATH.read_text(encoding="utf-8")
        + "\n\nLANGUAGE ADAPTER:\n"
        + json.dumps(adapter, ensure_ascii=False)
    )


def generate_response(
    client: Any,
    *,
    model: str,
    system: str,
    prompt: str,
    max_output_tokens: int,
    thinking_budget_tokens: int,
) -> Any:
    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=generation_response_schema(),
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_budget=thinking_budget_tokens,
                include_thoughts=False,
            ),
        ),
    )


def write_summary(
    path: Path, output_path: Path, run_stats: dict[str, Any]
) -> None:
    latest: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(output_path):
        latest[row["document_id"]] = row
    successes = [row for row in latest.values() if row.get("status") == "SUCCESS"]
    summary = {
        "schema_version": "1.0",
        "case_id": CASE_ID,
        "methodology_version": METHODOLOGY_VERSION,
        "prompt_version": PROMPT_VERSION,
        "latest_status_counts": {
            status: sum(row.get("status") == status for row in latest.values())
            for status in ("SUCCESS", "FAILED")
        },
        "documents_with_latest_record": len(latest),
        "successful_frame_scores": {
            row["document_id"]: row["frame_score"] for row in successes
        },
        "this_run": run_stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def recover_saved_candidates(
    *,
    prepared: list[dict[str, Any]],
    schema: dict[str, Any],
    output_path: Path,
    summary_path: Path,
    model: str,
    project: str,
    location: str,
) -> int:
    records = load_jsonl(output_path)
    completed = 0
    failed = 0
    for item in prepared:
        document = item["document"]
        source = next(
            (
                row
                for row in reversed(records)
                if row.get("document_id") == document["document_id"]
                and row.get("model") == model
                and row.get("prompt_version") == PROMPT_VERSION
                and isinstance(row.get("candidate_measurement"), dict)
            ),
            None,
        )
        if source is None:
            failed += 1
            print(
                json.dumps(
                    {
                        "document_id": document["document_id"],
                        "recovered": False,
                        "error": "no saved candidate",
                    }
                )
            )
            continue
        raw, corrections = normalize_fixed_metadata(
            source["candidate_measurement"], document
        )
        raw, output_normalizations = normalize_frame_output_limits(raw)
        raw, span_resolutions = resolve_verbatim_cues(
            raw, document={**document, "case_id": CASE_ID}
        )
        result, errors = validate_frame_measurement(
            raw,
            schema=schema,
            document={**document, "case_id": CASE_ID},
        )
        if result is None:
            failed += 1
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
            continue
        append_jsonl(
            output_path,
            {
                "schema_version": "1.0",
                "status": "SUCCESS",
                "document_id": document["document_id"],
                "source_id": document["source_id"],
                "source_name": document["source_name"],
                "language": document["language"],
                "publisher_slot": document["publisher_slot"],
                "sample_phase": document["sample_phase"],
                "model": model,
                "project": project,
                "location": location,
                "prompt_version": PROMPT_VERSION,
                "input_hash": item["input_hash"],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attempts": 0,
                "usage": {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "resource_usage": {
                    "rss_after_mb": current_rss_mb(),
                    "peak_rss_mb": peak_rss_mb(),
                },
                "provenance": item["provenance"],
                "recovered_from_saved_candidate": True,
                "source_candidate_input_hash": source.get("input_hash"),
                "source_candidate_recorded_at": source.get("recorded_at"),
                "source_candidate_provenance": source.get("provenance"),
                "recovery_note": (
                    "Candidate was revalidated under the current non-analytic "
                    "validation contract. Any source-exact span resolution is logged."
                ),
                "metadata_corrections": corrections,
                "output_normalizations": output_normalizations,
                "span_resolutions": span_resolutions,
                "attempt_diagnostics": [],
                "frame_score": frame_score(result),
                "measurement": result.model_dump(mode="json"),
            },
        )
        completed += 1
        print(
            json.dumps(
                {
                    "document_id": document["document_id"],
                    "recovered": True,
                    "frame_score": frame_score(result),
                },
                ensure_ascii=False,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--recover-saved-candidate", action="store_true")
    parser.add_argument("--reuse-latest-candidate", action="store_true")
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--roster", type=Path, default=ROSTER_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=os.getenv("GCP_REGION", DEFAULT_LOCATION))
    parser.add_argument("--model", default=os.getenv("VERTEX_AI_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    required = (
        args.corpus,
        args.roster,
        args.schema,
        CONTRACT_PATH,
        RULEBOOK_PATH,
        ADAPTER_DIR,
        BUDGET_PATH,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"required inputs are missing: {missing}")

    schema = load_schema(args.schema)
    budgets = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    roster = json.loads(args.roster.read_text(encoding="utf-8"))
    if roster["case_id"] != CASE_ID:
        raise ValueError("ROSTER_CASE_ID_MISMATCH")
    if roster["methodology_version"] != METHODOLOGY_VERSION:
        raise ValueError("ROSTER_METHODOLOGY_VERSION_MISMATCH")
    if roster["prompt_version"] != PROMPT_VERSION:
        raise ValueError("ROSTER_PROMPT_VERSION_MISMATCH")

    corpus = load_corpus(args.corpus)
    requested = args.document_id or roster["document_ids"]
    if len(requested) > budgets["max_documents_per_run"]:
        raise ValueError("MAX_DOCUMENTS_PER_RUN_EXCEEDED")
    if len(requested) != len(set(requested)):
        raise ValueError("DUPLICATE_DOCUMENT_ID")
    unknown = set(requested) - corpus.keys()
    outside = set(requested) - set(roster["document_ids"])
    if unknown or outside:
        raise ValueError(
            f"invalid documents: unknown={sorted(unknown)}, outside_roster={sorted(outside)}"
        )

    prepared = []
    for document_id in requested:
        document = corpus[document_id]
        if len(document["text"]) > budgets["max_document_chars"]:
            raise ValueError(f"DOCUMENT_CHAR_BUDGET_EXCEEDED:{document_id}")
        adapter_path = ADAPTER_DIR / f"{document['language']}.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        if adapter["shared_prompt_version"] != PROMPT_VERSION:
            raise ValueError(f"ADAPTER_PROMPT_VERSION_MISMATCH:{document_id}")
        bundle, provenance = build_bundle(document, adapter=adapter)
        prompt = build_prompt(bundle, schema)
        system = system_instruction(adapter)
        input_chars = len(prompt) + len(system)
        if input_chars > budgets["max_prompt_chars"]:
            raise ValueError(f"PROMPT_CHAR_BUDGET_EXCEEDED:{document_id}")
        digest = stable_hash(
            {
                "model": args.model,
                "project": args.project,
                "location": args.location,
                "bundle": bundle,
                "provenance": provenance,
                "prompt_version": PROMPT_VERSION,
            }
        )
        prepared.append(
            {
                "document": document,
                "bundle": bundle,
                "provenance": provenance,
                "prompt": prompt,
                "system": system,
                "input_chars": input_chars,
                "input_hash": digest,
            }
        )

    cache = cached_successes(args.output)
    if args.recover_saved_candidate:
        return recover_saved_candidates(
            prepared=prepared,
            schema=schema,
            output_path=args.output,
            summary_path=args.summary,
            model=args.model,
            project=args.project,
            location=args.location,
        )
    if not args.live:
        print(
            json.dumps(
                {
                    "valid": True,
                    "live": False,
                    "documents": [
                        {
                            "document_id": item["document"]["document_id"],
                            "language": item["document"]["language"],
                            "sample_phase": item["document"]["sample_phase"],
                            "publisher_slot": item["document"]["publisher_slot"],
                            "document_chars": len(item["document"]["text"]),
                            "total_input_chars": item["input_chars"],
                            "input_hash": item["input_hash"],
                            "cache_hit": (
                                item["document"]["document_id"],
                                item["input_hash"],
                                args.model,
                            )
                            in cache,
                        }
                        for item in prepared
                    ],
                    "budgets": budgets,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    client = genai.Client(
        vertexai=True,
        project=args.project,
        location=args.location,
        http_options=types.HttpOptions(
            timeout=budgets["request_timeout_millis"],
            retry_options=types.HttpRetryOptions(
                attempts=budgets["http_retry_attempts"]
            ),
        ),
    )
    started = time.monotonic()
    start_rss = current_rss_mb()
    calls = 0
    tokens = 0
    completed = 0
    skipped = 0
    failed = 0
    prior_records = load_jsonl(args.output)

    for item in prepared:
        document = item["document"]
        cache_key = (document["document_id"], item["input_hash"], args.model)
        if cache_key in cache:
            skipped += 1
            continue
        elapsed = time.monotonic() - started
        rss_before = current_rss_mb()
        if (
            calls >= budgets["max_model_calls_per_run"]
            or tokens >= budgets["max_total_tokens_per_run"]
            or elapsed >= budgets["max_elapsed_seconds_per_run"]
            or (
                rss_before is not None
                and rss_before > budgets["stop_before_next_document_if_rss_mb_exceeds"]
            )
        ):
            append_jsonl(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "FAILED",
                    "failure_code": "BUDGET_EXHAUSTED",
                    "document_id": document["document_id"],
                    "model": args.model,
                    "input_hash": item["input_hash"],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "provenance": item["provenance"],
                },
            )
            failed += 1
            continue

        attempts = 0
        validation_errors: list[str] = []
        last_candidate: dict[str, Any] | None = None
        last_usage: dict[str, int | None] = {}
        diagnostics = []
        result = None
        failure_code = "MODEL_ERROR"
        error_text = None
        metadata_corrections = []
        output_normalizations = []
        span_resolutions = []

        saved_source = next(
            (
                row
                for row in reversed(prior_records)
                if row.get("status") == "FAILED"
                and row.get("document_id") == document["document_id"]
                and (
                    row.get("input_hash") == item["input_hash"]
                    or (
                        args.reuse_latest_candidate
                        and row.get("prompt_version") == PROMPT_VERSION
                    )
                )
                and row.get("model") == args.model
                and isinstance(row.get("candidate_measurement"), dict)
            ),
            None,
        )
        if saved_source is not None:
            raw, metadata_corrections = normalize_fixed_metadata(
                saved_source["candidate_measurement"], document
            )
            raw, output_normalizations = normalize_frame_output_limits(raw)
            raw, span_resolutions = resolve_verbatim_cues(
                raw, document={**document, "case_id": CASE_ID}
            )
            last_candidate = raw
            result, validation_errors = validate_frame_measurement(
                raw,
                schema=schema,
                document={**document, "case_id": CASE_ID},
            )
            diagnostics.append(
                {
                    "attempt": 0,
                    "reused_saved_candidate": True,
                    "source_recorded_at": saved_source.get("recorded_at"),
                    "source_input_hash": saved_source.get("input_hash"),
                    "validation_errors": validation_errors,
                    "metadata_corrections": metadata_corrections,
                    "output_normalizations": output_normalizations,
                    "span_resolutions": span_resolutions,
                }
            )
            if result is None:
                attempts = 1

        while result is None and attempts <= budgets["max_repair_calls_per_document"]:
            if calls >= budgets["max_model_calls_per_run"]:
                error_text = "model-call budget exhausted before repair"
                failure_code = "BUDGET_EXHAUSTED"
                break
            attempts += 1
            calls += 1
            call_prompt = item["prompt"]
            if validation_errors:
                targeted_guidance = []
                if any("exact evidence span not found" in error for error in validation_errors):
                    targeted_guidance.append(
                        "For every missing exact span, copy a shorter verbatim substring "
                        "from the supplied article or delete that cue and its references. "
                        "Do not return the rejected wording again."
                    )
                if any(
                    "lacks prominent or repeated editorial evidence" in error
                    for error in validation_errors
                ):
                    targeted_guidance.append(
                        "For F3/F4 scores 3-4, either cite genuinely qualifying prominent "
                        "or repeated structural evidence, or lower the score to 0-2."
                    )
                call_prompt += (
                    "\n\nREPAIR THE PREVIOUS OUTPUT BELOW. Preserve valid analysis and "
                    "source-exact evidence, but correct every listed contract error. "
                    "Return a fresh complete object.\n"
                    + ("TARGETED REPAIR RULES:\n- " + "\n- ".join(targeted_guidance) + "\n" if targeted_guidance else "")
                    + "\nPREVIOUS OUTPUT:\n"
                    + json.dumps(last_candidate, ensure_ascii=False, separators=(",", ":"))
                    + "\n\nVALIDATION ERRORS:\n- "
                    + "\n- ".join(validation_errors)
                )
            try:
                response = generate_response(
                    client,
                    model=args.model,
                    system=item["system"],
                    prompt=call_prompt,
                    max_output_tokens=budgets["max_output_tokens_per_call"],
                    thinking_budget_tokens=budgets["thinking_budget_tokens_per_call"],
                )
                last_usage = response_usage(response)
                tokens += last_usage.get("total_tokens") or 0
                raw = json.loads(response.text)
                raw, metadata_corrections = normalize_fixed_metadata(raw, document)
                raw, output_normalizations = normalize_frame_output_limits(raw)
                raw, span_resolutions = resolve_verbatim_cues(
                    raw, document={**document, "case_id": CASE_ID}
                )
                last_candidate = raw
                validation_document = {
                    **document,
                    "case_id": CASE_ID,
                }
                result, validation_errors = validate_frame_measurement(
                    raw,
                    schema=schema,
                    document=validation_document,
                )
                diagnostics.append(
                    {
                        "attempt": attempts,
                        "usage": last_usage,
                        "validation_errors": validation_errors,
                        "metadata_corrections": metadata_corrections,
                        "output_normalizations": output_normalizations,
                        "span_resolutions": span_resolutions,
                    }
                )
                if result is not None:
                    break
                failure_code = "SCHEMA_OR_EVIDENCE_VALIDATION_FAILED"
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {str(exc)[:2500]}"
                diagnostics.append(
                    {"attempt": attempts, "error": error_text, "validation_errors": []}
                )
                failure_code = (
                    "RATE_LIMITED"
                    if "429 RESOURCE_EXHAUSTED" in error_text
                    else "MODEL_ERROR"
                )
                if (
                    "400 INVALID_ARGUMENT" in error_text
                    or failure_code == "RATE_LIMITED"
                    or attempts > budgets["max_repair_calls_per_document"]
                ):
                    break

        resource_usage = {
            "rss_before_mb": rss_before,
            "rss_after_mb": current_rss_mb(),
            "peak_rss_mb": peak_rss_mb(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        if result is None:
            append_jsonl(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "FAILED",
                    "failure_code": failure_code,
                    "document_id": document["document_id"],
                    "source_id": document["source_id"],
                    "language": document["language"],
                    "sample_phase": document["sample_phase"],
                    "model": args.model,
                    "project": args.project,
                    "location": args.location,
                    "prompt_version": PROMPT_VERSION,
                    "input_hash": item["input_hash"],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": attempts,
                    "usage": last_usage,
                    "resource_usage": resource_usage,
                    "provenance": item["provenance"],
                    "error": error_text,
                    "validation_errors": validation_errors,
                    "candidate_measurement": last_candidate,
                    "attempt_diagnostics": diagnostics,
                },
            )
            failed += 1
            print(
                json.dumps(
                    {
                        "document_id": document["document_id"],
                        "status": "FAILED",
                        "failure_code": failure_code,
                        "errors": validation_errors[:4],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        measurement = result.model_dump(mode="json")
        append_jsonl(
            args.output,
            {
                "schema_version": "1.0",
                "status": "SUCCESS",
                "document_id": document["document_id"],
                "source_id": document["source_id"],
                "source_name": document["source_name"],
                "language": document["language"],
                "publisher_slot": document["publisher_slot"],
                "sample_phase": document["sample_phase"],
                "model": args.model,
                "project": args.project,
                "location": args.location,
                "prompt_version": PROMPT_VERSION,
                "input_hash": item["input_hash"],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
                "usage": last_usage,
                "resource_usage": resource_usage,
                "provenance": item["provenance"],
                "metadata_corrections": metadata_corrections,
                "output_normalizations": output_normalizations,
                "span_resolutions": span_resolutions,
                "attempt_diagnostics": diagnostics,
                "frame_score": frame_score(result),
                "measurement": measurement,
            },
        )
        completed += 1
        cache.add(cache_key)
        print(
            json.dumps(
                {
                    "document_id": document["document_id"],
                    "status": "SUCCESS",
                    "attempts": attempts,
                    "frame_score": frame_score(result),
                    "analysis_status": result.analysis_status,
                },
                ensure_ascii=False,
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
        "rss_start_mb": start_rss,
        "peak_rss_mb": peak_rss_mb(),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    write_summary(args.summary, args.output, run_stats)
    print(json.dumps({"run_summary": run_stats}, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
