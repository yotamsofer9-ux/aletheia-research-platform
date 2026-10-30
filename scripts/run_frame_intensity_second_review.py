#!/usr/bin/env python3
"""Run the independent second review for frame-intensity calibration outputs."""

from __future__ import annotations

import argparse
import json
import os
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
    FrameMeasurement,
    PROMPT_VERSION,
    build_score_gap_review,
    file_hash,
    frame_score,
    load_schema,
    normalize_frame_output_limits,
    resolve_verbatim_cues,
    stable_hash,
    validate_frame_measurement,
)
from scripts.run_frame_intensity_analysis import (
    ADAPTER_DIR,
    BUDGET_PATH,
    CASE_ID,
    CONTRACT_PATH,
    CORPUS_PATH,
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    DEFAULT_PROJECT,
    ROSTER_PATH,
    RULEBOOK_PATH,
    SCHEMA_PATH,
    append_jsonl,
    current_rss_mb,
    generation_response_schema,
    load_corpus,
    load_jsonl,
    normalize_fixed_metadata,
    peak_rss_mb,
    response_usage,
)


SECOND_RULEBOOK_PATH = ROOT / "measurement" / "FRAME_SECOND_REVIEW_RULEBOOK.md"
FIRST_PASS_PATH = (
    ROOT
    / "data"
    / "cases"
    / "case-002"
    / "analysis"
    / "frame_intensity_calibration_first_pass_v1.jsonl"
)
DEFAULT_OUTPUT = FIRST_PASS_PATH.with_name(
    "frame_intensity_calibration_second_pass_v1.jsonl"
)
DEFAULT_SUMMARY = DEFAULT_OUTPUT.with_name(
    "frame_intensity_calibration_second_pass_v1_summary.json"
)
REVIEW_PROMPT_VERSION = "case002-frame-intensity-review-v0.1.0"


def latest_successes(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(path):
        latest[row["document_id"]] = row
    return {
        document_id: row
        for document_id, row in latest.items()
        if row.get("status") == "SUCCESS"
    }


def cached_successes(path: Path) -> set[tuple[str, str, str]]:
    return {
        (row["document_id"], row["input_hash"], row["model"])
        for row in load_jsonl(path)
        if row.get("status") == "SUCCESS"
    }


def system_instruction(adapter: dict[str, Any]) -> str:
    return (
        SECOND_RULEBOOK_PATH.read_text(encoding="utf-8")
        + "\n\nBASE CODER RULEBOOK:\n"
        + RULEBOOK_PATH.read_text(encoding="utf-8")
        + "\n\nFROZEN CONTRACT:\n"
        + CONTRACT_PATH.read_text(encoding="utf-8")
        + "\n\nLANGUAGE ADAPTER:\n"
        + json.dumps(adapter, ensure_ascii=False)
    )


def build_prompt(
    *,
    document: dict[str, str],
    first_measurement: dict[str, Any],
    schema: dict[str, Any],
) -> str:
    bundle = {
        "case_id": CASE_ID,
        "review_task": "INDEPENDENT_ADVERSARIAL_FRAME_REVIEW",
        "document": {
            key: document.get(key, "")
            for key in (
                "document_id",
                "source_id",
                "source_name",
                "language",
                "publisher_slot",
                "title",
                "published_at",
                "sample_phase",
                "content_hash",
                "text",
            )
        },
        "first_pass_measurement": first_measurement,
    }
    return (
        "Re-code the article independently after testing every methodological "
        "counterargument. Return one fresh complete measurement and no prose.\n\n"
        "FROZEN REVIEW BUNDLE:\n"
        + json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        + "\n\nSTRICT OUTPUT SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def write_summary(path: Path, output_path: Path, run_stats: dict[str, Any]) -> None:
    latest: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(output_path):
        latest[row["document_id"]] = row
    successes = [row for row in latest.values() if row.get("status") == "SUCCESS"]
    gaps = [
        component["gap"]
        for row in successes
        for component in row["comparison"]["components"].values()
    ]
    summary = {
        "schema_version": "1.0",
        "case_id": CASE_ID,
        "review_prompt_version": REVIEW_PROMPT_VERSION,
        "latest_status_counts": {
            status: sum(row.get("status") == status for row in latest.values())
            for status in ("SUCCESS", "FAILED")
        },
        "documents_with_latest_record": len(latest),
        "agreement": {
            "component_comparisons": len(gaps),
            "exact_rate": round(sum(gap == 0 for gap in gaps) / len(gaps), 4)
            if gaps
            else None,
            "within_one_rate": round(sum(gap <= 1 for gap in gaps) / len(gaps), 4)
            if gaps
            else None,
            "mean_absolute_gap": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "documents_escalated": sum(
                row["comparison"]["requires_human_review"] for row in successes
            ),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--recover-saved-candidate", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even when the same validated input hash is already cached.",
    )
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--first-pass", type=Path, default=FIRST_PASS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID", DEFAULT_PROJECT))
    parser.add_argument("--location", default=os.getenv("GCP_REGION", DEFAULT_LOCATION))
    parser.add_argument("--model", default=os.getenv("VERTEX_AI_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    schema = load_schema(SCHEMA_PATH)
    budgets = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    corpus = load_corpus(CORPUS_PATH)
    first_pass = latest_successes(args.first_pass)
    requested = args.document_id or roster["document_ids"]
    missing = set(requested) - first_pass.keys()
    if missing:
        raise ValueError(f"FIRST_PASS_SUCCESS_REQUIRED:{sorted(missing)}")

    prepared = []
    for document_id in requested:
        document = corpus[document_id]
        first = first_pass[document_id]
        adapter_path = ADAPTER_DIR / f"{document['language']}.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        prompt = build_prompt(
            document=document,
            first_measurement=first["measurement"],
            schema=schema,
        )
        system = system_instruction(adapter)
        input_chars = len(prompt) + len(system)
        if input_chars > budgets["max_prompt_chars"]:
            raise ValueError(f"PROMPT_CHAR_BUDGET_EXCEEDED:{document_id}")
        provenance = {
            "first_pass_input_hash": first["input_hash"],
            "first_pass_recorded_at": first["recorded_at"],
            "schema_hash": file_hash(SCHEMA_PATH),
            "contract_hash": file_hash(CONTRACT_PATH),
            "base_rulebook_hash": file_hash(RULEBOOK_PATH),
            "review_rulebook_hash": file_hash(SECOND_RULEBOOK_PATH),
            "adapter_hash": stable_hash(adapter),
            "budget_hash": file_hash(BUDGET_PATH),
        }
        input_hash = stable_hash(
            {
                "model": args.model,
                "project": args.project,
                "location": args.location,
                "review_prompt_version": REVIEW_PROMPT_VERSION,
                "provenance": provenance,
                "prompt": prompt,
            }
        )
        prepared.append(
            {
                "document": document,
                "first": first,
                "prompt": prompt,
                "system": system,
                "input_chars": input_chars,
                "input_hash": input_hash,
                "provenance": provenance,
            }
        )

    cache = cached_successes(args.output)
    if args.recover_saved_candidate:
        records = load_jsonl(args.output)
        completed = failed = 0
        for item in prepared:
            document = item["document"]
            source = next(
                (
                    row
                    for row in reversed(records)
                    if row.get("document_id") == document["document_id"]
                    and row.get("model") == args.model
                    and isinstance(row.get("candidate_measurement"), dict)
                ),
                None,
            )
            if source is None:
                failed += 1
                continue
            raw, metadata_log = normalize_fixed_metadata(
                source["candidate_measurement"], document
            )
            raw, output_normalization_log = normalize_frame_output_limits(raw)
            raw, span_log = resolve_verbatim_cues(
                raw, document={**document, "case_id": CASE_ID}
            )
            result, validation_errors = validate_frame_measurement(
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
                            "errors": validation_errors,
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            comparison = build_score_gap_review(
                FrameMeasurement.model_validate(item["first"]["measurement"]),
                result,
            )
            append_jsonl(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "SUCCESS",
                    "document_id": document["document_id"],
                    "source_name": document["source_name"],
                    "language": document["language"],
                    "sample_phase": document["sample_phase"],
                    "model": args.model,
                    "review_prompt_version": REVIEW_PROMPT_VERSION,
                    "input_hash": item["input_hash"],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": 0,
                    "usage": {
                        "prompt_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                    "resource_usage": {
                        "rss_after_mb": current_rss_mb(),
                        "peak_rss_mb": peak_rss_mb(),
                        "elapsed_seconds": 0.0,
                    },
                    "provenance": item["provenance"],
                    "recovered_from_saved_candidate": True,
                    "metadata_corrections": metadata_log,
                    "output_normalizations": output_normalization_log,
                    "span_resolutions": span_log,
                    "attempt_diagnostics": [],
                    "first_frame_score": item["first"]["frame_score"],
                    "second_frame_score": frame_score(result),
                    "comparison": comparison,
                    "measurement": result.model_dump(mode="json"),
                },
            )
            completed += 1
            print(
                json.dumps(
                    {
                        "document_id": document["document_id"],
                        "recovered": True,
                        "second_F": frame_score(result),
                        "human_review": comparison["requires_human_review"],
                    },
                    ensure_ascii=False,
                )
            )
        write_summary(
            args.summary,
            args.output,
            {
                "requested": len(prepared),
                "completed": completed,
                "cached": 0,
                "failed": failed,
                "model_calls": 0,
                "total_tokens": 0,
                "recovery_only": True,
            },
        )
        return 1 if failed else 0
    if not args.live:
        print(
            json.dumps(
                {
                    "valid": True,
                    "live": False,
                    "documents": [
                        {
                            "document_id": item["document"]["document_id"],
                            "first_frame_score": item["first"]["frame_score"],
                            "total_input_chars": item["input_chars"],
                            "cache_hit": (
                                item["document"]["document_id"],
                                item["input_hash"],
                                args.model,
                            )
                            in cache,
                        }
                        for item in prepared
                    ],
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
    calls = tokens = completed = skipped = failed = 0
    for item in prepared:
        document = item["document"]
        cache_key = (document["document_id"], item["input_hash"], args.model)
        if cache_key in cache and not args.force:
            skipped += 1
            continue
        attempts = 0
        validation_errors: list[str] = []
        last_candidate = None
        last_usage: dict[str, int | None] = {}
        result = None
        diagnostics = []
        error_text = None
        failure_code = "MODEL_ERROR"
        while attempts <= budgets["max_repair_calls_per_document"]:
            attempts += 1
            calls += 1
            prompt = item["prompt"]
            if validation_errors:
                targeted_guidance = []
                if any("exact evidence span not found" in error for error in validation_errors):
                    targeted_guidance.append(
                        "Replace each missing span with a shorter verbatim source substring "
                        "or delete the cue and its references; never repeat rejected wording."
                    )
                if any(
                    "lacks prominent or repeated editorial evidence" in error
                    for error in validation_errors
                ):
                    targeted_guidance.append(
                        "For F3/F4 scores 3-4, cite qualifying prominent or repeated "
                        "structural evidence or lower the score to 0-2."
                    )
                prompt += (
                    "\n\nREPAIR THE PREVIOUS SECOND-REVIEW OUTPUT. Return a fresh "
                    "complete object.\n"
                    + ("TARGETED REPAIR RULES:\n- " + "\n- ".join(targeted_guidance) + "\n" if targeted_guidance else "")
                    + "PREVIOUS OUTPUT:\n"
                    + json.dumps(last_candidate, ensure_ascii=False, separators=(",", ":"))
                    + "\nVALIDATION ERRORS:\n- "
                    + "\n- ".join(validation_errors)
                )
            try:
                response = client.models.generate_content(
                    model=args.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=item["system"],
                        response_mime_type="application/json",
                        response_schema=generation_response_schema(),
                        temperature=0.0,
                        max_output_tokens=budgets["max_output_tokens_per_call"],
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=budgets["thinking_budget_tokens_per_call"],
                            include_thoughts=False,
                        ),
                    ),
                )
                last_usage = response_usage(response)
                tokens += last_usage.get("total_tokens") or 0
                raw = json.loads(response.text)
                raw, metadata_log = normalize_fixed_metadata(raw, document)
                raw, output_normalization_log = normalize_frame_output_limits(raw)
                raw, span_log = resolve_verbatim_cues(
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
                        "attempt": attempts,
                        "usage": last_usage,
                        "validation_errors": validation_errors,
                        "metadata_corrections": metadata_log,
                        "output_normalizations": output_normalization_log,
                        "span_resolutions": span_log,
                    }
                )
                if result is not None:
                    break
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {str(exc)[:2500]}"
                diagnostics.append({"attempt": attempts, "error": error_text})
                failure_code = (
                    "RATE_LIMITED"
                    if "429 RESOURCE_EXHAUSTED" in error_text
                    else "MODEL_ERROR"
                )
                if "400 INVALID_ARGUMENT" in error_text or failure_code == "RATE_LIMITED":
                    break

        if result is None:
            failed += 1
            append_jsonl(
                args.output,
                {
                    "schema_version": "1.0",
                    "status": "FAILED",
                    "failure_code": failure_code,
                    "document_id": document["document_id"],
                    "model": args.model,
                    "review_prompt_version": REVIEW_PROMPT_VERSION,
                    "input_hash": item["input_hash"],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": attempts,
                    "usage": last_usage,
                    "provenance": item["provenance"],
                    "error": error_text,
                    "validation_errors": validation_errors,
                    "candidate_measurement": last_candidate,
                    "attempt_diagnostics": diagnostics,
                },
            )
            continue
        first_measurement = item["first"]["measurement"]
        comparison = build_score_gap_review(
            FrameMeasurement.model_validate(first_measurement),
            result,
        )
        append_jsonl(
            args.output,
            {
                "schema_version": "1.0",
                "status": "SUCCESS",
                "document_id": document["document_id"],
                "source_name": document["source_name"],
                "language": document["language"],
                "sample_phase": document["sample_phase"],
                "model": args.model,
                "review_prompt_version": REVIEW_PROMPT_VERSION,
                "input_hash": item["input_hash"],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
                "usage": last_usage,
                "resource_usage": {
                    "rss_after_mb": current_rss_mb(),
                    "peak_rss_mb": peak_rss_mb(),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                },
                "provenance": item["provenance"],
                "attempt_diagnostics": diagnostics,
                "first_frame_score": item["first"]["frame_score"],
                "second_frame_score": frame_score(result),
                "comparison": comparison,
                "measurement": result.model_dump(mode="json"),
            },
        )
        completed += 1
        print(
            json.dumps(
                {
                    "document_id": document["document_id"],
                    "first_F": item["first"]["frame_score"],
                    "second_F": frame_score(result),
                    "human_review": comparison["requires_human_review"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    stats = {
        "requested": len(prepared),
        "completed": completed,
        "cached": skipped,
        "failed": failed,
        "model_calls": calls,
        "total_tokens": tokens,
        "peak_rss_mb": peak_rss_mb(),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    write_summary(args.summary, args.output, stats)
    print(json.dumps({"run_summary": stats}, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
