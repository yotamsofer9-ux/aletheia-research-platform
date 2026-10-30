from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from .judgment_room import (
    COMPONENT_LABELS,
    build_precedent_records,
    load_case_material,
    load_policy_registry,
)


CANONICAL_CASE_ID = "CASE-001"
MIGRATION_VERSION = "case001-closeout-v1"
FIXED_COUNTS = {
    "research_cases": 1,
    "corpus_documents": 27,
    "factual_anchors": 16,
    "case_score_results": 40,
    "case_closeouts": 1,
}
BASELINE_MINIMUM_COUNTS = {
    "analysis_runs": 6,
    "article_measurements": 39,
    "adjudication_decisions": 11,
    "component_score_ledger": 72,
    "trajectory_decisions": 9,
    "policy_records": 6,
}
SUMMARY_FILES = (
    "case001_article_measurements_v1_0_6_smoke_summary.json",
    "case001_article_measurements_v1_0_7_hebrew_summary.json",
    "case001_article_measurements_v1_0_8_hebrew_summary.json",
    "case001_article_measurements_v1_0_9_arabic_summary.json",
    "case001_article_measurements_v1_0_10_batch_a_summary.json",
    "case001_article_measurements_v1_0_11_batch_b_summary.json",
)
M5_REPORT_GLOB = "case001_m5_b*_report_v1.json"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_hash(row: dict[str, Any]) -> str:
    return _sha256_bytes(_json(row).encode("utf-8"))


def _finalize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["source_row_hash"] = _source_hash(result)
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda item: item["row_id"]):
            handle.write(_json(row) + "\n")


def _sqlite_rows(path: Path, query: str) -> list[dict[str, Any]]:
    database = sqlite3.connect(path)
    try:
        database.row_factory = sqlite3.Row
        return [dict(row) for row in database.execute(query)]
    finally:
        database.close()


def _live_run_value(summary: dict[str, Any], *names: str) -> Any:
    live = summary.get("live_run", {})
    for name in names:
        if name in live:
            return live[name]
    return None


def _complete_count(summary: dict[str, Any]) -> int | None:
    local = summary.get("local_recovery", {})
    for name in ("latest_valid_outputs", "completed"):
        if name in local:
            return int(local[name])
    value = _live_run_value(
        summary, "completed", "complete_candidates", "successful_model_responses"
    )
    return int(value) if value is not None else None


def _prepare_research_case(root: Path) -> list[dict[str, Any]]:
    rows = _sqlite_rows(root / "data/case001_research_pilot_27.db", "SELECT * FROM cases")
    if len(rows) != 1:
        raise ValueError(f"Expected one corpus case row, found {len(rows)}")
    source = rows[0]
    return [_finalize({
        "row_id": CANONICAL_CASE_ID,
        "schema_version": "1.0",
        "case_id": CANONICAL_CASE_ID,
        "corpus_id": source["case_id"],
        "title": source["title"],
        "window_start": source["window_start"],
        "window_end": source["window_end"],
        "keywords_json": source["keywords_json"],
        "config_json": source["config_json"],
        "updated_at": source["updated_at"],
        "migration_version": MIGRATION_VERSION,
    })]


def _prepare_documents(
    root: Path, output_dir: Path, bucket: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query = """
        SELECT d.*, s.name AS source_name, s.group_name, s.language,
               s.publisher_slot, s.editorial_profile, s.substitute_for
        FROM documents d JOIN sources s
          ON d.case_id = s.case_id AND d.source_id = s.source_id
        WHERE d.status = 'READY'
        ORDER BY d.document_id
    """
    documents = _sqlite_rows(root / "data/case001_research_pilot_27.db", query)
    rows: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    object_root = output_dir / "objects"
    snapshot_prefix = "local-snapshot://"

    for source in documents:
        ref = str(source["snapshot_ref"] or "")
        if not ref.startswith(snapshot_prefix):
            raise ValueError(f"Unsafe or missing snapshot reference: {ref}")
        relative = Path(ref[len(snapshot_prefix):])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe snapshot path: {relative}")
        local_snapshot = root / "data/snapshots" / relative
        compressed = local_snapshot.read_bytes()
        uncompressed = gzip.decompress(compressed)
        if _sha256_bytes(uncompressed) != source["snapshot_hash"]:
            raise ValueError(f"Snapshot hash mismatch: {source['document_id']}")
        if len(uncompressed) != source["snapshot_size_bytes"]:
            raise ValueError(f"Snapshot size mismatch: {source['document_id']}")
        text = source["text"] or ""
        if _sha256_bytes(text.encode("utf-8")) != source["content_hash"]:
            raise ValueError(f"Normalized text hash mismatch: {source['document_id']}")

        raw_key = (
            f"raw/{CANONICAL_CASE_ID}/{source['source_id']}/"
            f"{source['snapshot_hash']}.gz"
        )
        raw_target = object_root / raw_key
        raw_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_snapshot, raw_target)

        normalized_key = (
            f"normalized/{CANONICAL_CASE_ID}/{source['source_id']}/"
            f"{source['document_id']}-{source['content_hash']}.json.gz"
        )
        normalized_target = object_root / normalized_key
        normalized_target.parent.mkdir(parents=True, exist_ok=True)
        normalized_payload = _json({
            "schema_version": "1.0",
            "case_id": CANONICAL_CASE_ID,
            "corpus_id": source["case_id"],
            "document_id": source["document_id"],
            "content_hash": source["content_hash"],
            "text": text,
        }).encode("utf-8")
        normalized_target.write_bytes(gzip.compress(normalized_payload, mtime=0))

        for kind, key, path, payload_hash in (
            ("RAW_SNAPSHOT", raw_key, raw_target, source["snapshot_hash"]),
            ("NORMALIZED_PAYLOAD", normalized_key, normalized_target, source["content_hash"]),
        ):
            object_bytes = path.read_bytes()
            objects.append({
                "object_type": kind,
                "document_id": source["document_id"],
                "object_key": key,
                "gcs_uri": f"gs://{bucket}/{key}",
                "staged_path": str(path.relative_to(output_dir)),
                "compressed_bytes": len(object_bytes),
                "compressed_sha256": _sha256_bytes(object_bytes),
                "payload_sha256": payload_hash,
                "content_type": "application/gzip",
            })

        rows.append(_finalize({
            "row_id": source["document_id"],
            "schema_version": "1.0",
            "case_id": CANONICAL_CASE_ID,
            "corpus_id": source["case_id"],
            "document_id": source["document_id"],
            "source_id": source["source_id"],
            "source_name": source["source_name"],
            "group_name": source["group_name"],
            "language": source["language"],
            "publisher_slot": source["publisher_slot"],
            "editorial_profile": source["editorial_profile"],
            "substitute_for": source["substitute_for"],
            "url": source["url"],
            "canonical_url": source["canonical_url"],
            "title": source["title"],
            "published_at": source["published_at"],
            "archived_at": source["archived_at"],
            "fetched_at": source["fetched_at"],
            "content_hash": source["content_hash"],
            "fetch_method": source["fetch_method"],
            "status": source["status"],
            "truncated": bool(source["truncated"]),
            "sample_phase": source["sample_phase"],
            "raw_snapshot_uri": f"gs://{bucket}/{raw_key}",
            "raw_snapshot_sha256": source["snapshot_hash"],
            "raw_snapshot_uncompressed_bytes": source["snapshot_size_bytes"],
            "raw_snapshot_media_type": source["snapshot_media_type"],
            "normalized_payload_uri": f"gs://{bucket}/{normalized_key}",
            "normalized_text_sha256": source["content_hash"],
            "normalized_text_chars": len(text),
            "run_id": source["run_id"],
        }))
    return rows, objects


def _prepare_frame_artifacts(
    root: Path, output_dir: Path, bucket: str
) -> list[dict[str, Any]]:
    paths = (
        root / "analysis/case001_frame_intensity_results_v1.json",
        root / "analysis/FRAME_INTENSITY_CASE001_FULL_REPORT.md",
        root / "analysis/runs/case001_frame_intensity_full_first_pass_v1.jsonl",
        root / "analysis/runs/case001_frame_intensity_full_second_pass_v1.jsonl",
        root / "analysis/case001_frame_full_roster_v1.json",
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"Missing CASE-001 frame artifacts: {missing}")
    artifacts = []
    for path in paths:
        payload = path.read_bytes()
        key = f"case-artifacts/{CANONICAL_CASE_ID}/frame-v1/{path.name}.gz"
        target = output_dir / "objects" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(gzip.compress(payload, mtime=0))
        object_bytes = target.read_bytes()
        artifacts.append({
            "object_type": "FRAME_ARTIFACT",
            "document_id": None,
            "object_key": key,
            "gcs_uri": f"gs://{bucket}/{key}",
            "staged_path": str(target.relative_to(output_dir)),
            "compressed_bytes": len(object_bytes),
            "compressed_sha256": _sha256_bytes(object_bytes),
            "payload_sha256": _sha256_bytes(payload),
            "content_type": "application/gzip",
        })
    return artifacts


def _prepare_anchors(root: Path) -> list[dict[str, Any]]:
    rows = []
    for record in _read_jsonl(root / "anchors/case001_factual_anchors_v1.jsonl"):
        rows.append(_finalize({
            "row_id": record["anchor_id"],
            "schema_version": record["schema_version"],
            "case_id": record["case_id"],
            "anchor_id": record["anchor_id"],
            "family": record["family"],
            "proposition_type": record["proposition_type"],
            "canonical_proposition": record["canonical_proposition"],
            "scope_notes": record["scope_notes"],
            "assessment_count": len(record["assessments"]),
            "record_json": _json(record),
        }))
    return rows


def _prepare_analysis_runs(root: Path) -> list[dict[str, Any]]:
    rows = []
    for filename in SUMMARY_FILES:
        summary = json.loads((root / "analysis/runs" / filename).read_text(encoding="utf-8"))
        status = summary.get("status") or summary.get("gate") or "UNKNOWN"
        rows.append(_finalize({
            "row_id": summary["prompt_version"],
            "schema_version": summary["schema_version"],
            "case_id": summary["case_id"],
            "prompt_version": summary["prompt_version"],
            "measurement_schema_version": summary["measurement_schema_version"],
            "run_date": summary["run_date"],
            "status": status,
            "requested": _live_run_value(summary, "requested"),
            "completed": _complete_count(summary),
            "model_calls": _live_run_value(
                summary, "model_calls", "model_responses", "successful_model_responses"
            ),
            "paid_repair_calls": _live_run_value(summary, "paid_repair_calls"),
            "total_tokens": _live_run_value(summary, "total_tokens"),
            "elapsed_seconds": _live_run_value(summary, "elapsed_seconds"),
            "peak_rss_mb": _live_run_value(summary, "peak_rss_mb"),
            "summary_json": _json(summary),
        }))

    manifest = json.loads(
        (root / "analysis/m5/case001_m5_execution_manifest_v1.json").read_text(
            encoding="utf-8"
        )
    )
    for report_path in sorted((root / "analysis/m5").glob(M5_REPORT_GLOB)):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        batch_id = report.get("batch_id")
        if not batch_id:
            raise ValueError(f"M5 report has no batch_id: {report_path}")
        execution = report.get("execution") or {}
        usage = report.get("usage") or {}
        resources = report.get("resources") or {}
        completed = execution.get("successful_documents")
        if completed is None:
            completed = execution.get("final_successful_documents")
        rows.append(_finalize({
            "row_id": f"{report['prompt_version']}:{batch_id}",
            "schema_version": report["schema_version"],
            "case_id": report["case_id"],
            "prompt_version": report["prompt_version"],
            "measurement_schema_version": (
                report.get("measurement_schema_version")
                or manifest["measurement_schema_version"]
            ),
            "run_date": report.get("executed_at") or report.get("finalized_at") or "",
            "status": report["status"],
            "requested": execution.get("requested_documents"),
            "completed": completed,
            "model_calls": execution.get("paid_model_calls"),
            "paid_repair_calls": execution.get("paid_repair_calls"),
            "total_tokens": usage.get("total_tokens"),
            "elapsed_seconds": resources.get("elapsed_seconds"),
            "peak_rss_mb": resources.get("peak_rss_mb"),
            "summary_json": _json(report),
        }))
    for stage, summary_path in (
        (
            "FIRST_PASS",
            root / "analysis/runs/case001_frame_intensity_full_first_pass_v1_summary.json",
        ),
        (
            "SECOND_PASS",
            root / "analysis/runs/case001_frame_intensity_full_second_pass_v1_summary.json",
        ),
    ):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        success_count = int(summary["latest_status_counts"].get("SUCCESS", 0))
        failure_count = int(summary["latest_status_counts"].get("FAILED", 0))
        rows.append(_finalize({
            "row_id": f"CASE-001:FRAME_INTENSITY:{stage}:V1",
            "schema_version": summary["schema_version"],
            "case_id": CANONICAL_CASE_ID,
            "prompt_version": (
                summary.get("prompt_version")
                or summary.get("review_prompt_version")
                or "case002-frame-intensity-review-v0.1.0"
            ),
            "measurement_schema_version": "frame-intensity-v0.1.0",
            "run_date": summary["generated_at"],
            "status": "COMPLETE" if success_count == 27 and failure_count == 0 else "INCOMPLETE",
            "requested": 27,
            "completed": success_count,
            "model_calls": None,
            "paid_repair_calls": None,
            "total_tokens": None,
            "elapsed_seconds": None,
            "peak_rss_mb": None,
            "summary_json": _json(summary),
        }))
    return rows


def _prepare_measurements(root: Path) -> list[dict[str, Any]]:
    rows = []
    records = _read_jsonl(root / "analysis/runs/case001_article_measurements_v1.jsonl")
    document_metadata = {
        row["document_id"]: row
        for row in _sqlite_rows(
            root / "data/case001_research_pilot_27.db",
            """SELECT d.document_id, d.source_id, d.sample_phase, s.language
                 FROM documents d JOIN sources s
                   ON d.case_id = s.case_id AND d.source_id = s.source_id""",
        )
    }
    for index, record in enumerate(records, 1):
        document = document_metadata.get(record["document_id"])
        if document is None:
            raise ValueError(f"Unknown measurement document: {record['document_id']}")
        identity = "|".join(str(record.get(key) or "") for key in (
            "document_id", "prompt_version", "input_hash", "recorded_at", "status"
        )) + f"|{index}"
        usage = record.get("usage") or {}
        resources = record.get("resource_usage") or {}
        rows.append(_finalize({
            "row_id": _sha256_bytes(identity.encode("utf-8")),
            "schema_version": record["schema_version"],
            "case_id": (record.get("measurement") or {}).get("case_id", CANONICAL_CASE_ID),
            "document_id": record["document_id"],
            "source_id": record.get("source_id") or document["source_id"],
            "language": record.get("language") or document["language"],
            "sample_phase": record.get("sample_phase") or document["sample_phase"],
            "status": record["status"],
            "model": record["model"],
            "prompt_version": record["prompt_version"],
            "input_hash": record["input_hash"],
            "recorded_at": record["recorded_at"],
            "attempts": record["attempts"],
            "total_tokens": usage.get("total_tokens"),
            "peak_rss_mb": resources.get("peak_rss_mb"),
            "elapsed_seconds": resources.get("elapsed_seconds_total_run"),
            "record_json": _json(record),
        }))
    return rows


def _prepare_adjudications(root: Path) -> list[dict[str, Any]]:
    rows = _sqlite_rows(
        root / "data/judgment_room.sqlite3",
        "SELECT * FROM adjudication_decisions ORDER BY dilemma_id, decision_version",
    )
    prepared = []
    for source in rows:
        prepared.append(_finalize({
            "row_id": source["decision_id"],
            "case_id": source["case_id"],
            "decision_id": source["decision_id"],
            "dilemma_id": source["dilemma_id"],
            "document_id": source["document_id"],
            "component": source["component"],
            "outcome": source["outcome"],
            "final_score": source["final_score"],
            "final_na_reason": source["final_na_reason"],
            "rationale": source["rationale"],
            "decided_by": source["decided_by"],
            "decided_at": source["decided_at"],
            "decision_version": source["decision_version"],
            "supersedes_decision_id": source["supersedes_decision_id"],
            "is_current": bool(source["is_current"]),
            "dilemma_snapshot_json": source["dilemma_snapshot_json"],
            "precedent_mode": source["precedent_mode"],
            "policy_rule": source["policy_rule"],
            "policy_scope_json": source["policy_scope_json"],
            "policy_metadata_version": source["policy_metadata_version"],
        }))
    return prepared


def _prepare_ledger(root: Path) -> list[dict[str, Any]]:
    source_rows = _sqlite_rows(
        root / "data/judgment_room.sqlite3",
        "SELECT * FROM component_score_ledger ORDER BY record_id",
    )
    rows = []
    for source in source_rows:
        row = dict(source)
        row["row_id"] = row["record_id"]
        row["is_current"] = bool(row["is_current"])
        row["is_effective"] = bool(row["is_effective"])
        rows.append(_finalize(row))
    frame_path = root / "analysis/case001_frame_intensity_results_v1.json"
    if frame_path.exists():
        frame_payload = json.loads(frame_path.read_text(encoding="utf-8"))
        if (
            frame_payload.get("status") != "COMPLETE"
            or frame_payload.get("documents") != 27
            or frame_payload.get("component_rows") != 135
            or frame_payload.get("unresolved_human_reviews") != 0
        ):
            raise ValueError("CASE-001 frame-intensity results are not complete")
        for record in frame_payload["records"]:
            cues = {cue["cue_id"]: cue for cue in record["frame_cues"]}
            for component_id in ("F1", "F2", "F3", "F4", "F5"):
                component = record["components"][component_id]
                evidence = [
                    cues[cue_id]
                    for cue_id in component["evidence_cue_ids"]
                    if cue_id in cues
                ]
                record_id = (
                    f"{CANONICAL_CASE_ID}:{record['document_id']}:"
                    f"{component_id}:FRAME_V1"
                )
                rows.append(_finalize({
                    "row_id": record_id,
                    "case_id": CANONICAL_CASE_ID,
                    "record_id": record_id,
                    "document_id": record["document_id"],
                    "source_id": record["source_id"],
                    "source_name": record["source_name"],
                    "published_at": record["published_at"],
                    "sample_phase": record["sample_phase"],
                    "component": component_id,
                    "component_label": COMPONENT_LABELS[component_id],
                    "authority": record["authority"],
                    "score": component["score"],
                    "na_reason": None,
                    "rationale": component["rationale_he"],
                    "status": "FINAL",
                    "outcome": "SECOND_PASS_ACCEPTED_WITHIN_THRESHOLD",
                    "prompt_version": record["prompt_version"],
                    "model_confidence": component["confidence"],
                    "evidence_span_json": _json(evidence),
                    "anchor_refs_json": _json([]),
                    "decision_id": None,
                    "decision_version": None,
                    "decided_by": None,
                    "recorded_at": record["recorded_at"],
                    "supersedes_record_id": None,
                    "is_current": True,
                    "is_effective": True,
                }))
    return rows


def _prepare_trajectory_decisions(root: Path) -> list[dict[str, Any]]:
    source_rows = _sqlite_rows(
        root / "data/judgment_room.sqlite3",
        "SELECT * FROM trajectory_decisions ORDER BY trajectory_id, decision_version",
    )
    rows = []
    for source in source_rows:
        rows.append(_finalize({
            "row_id": source["decision_id"],
            "decision_id": source["decision_id"],
            "trajectory_id": source["trajectory_id"],
            "case_id": source["case_id"],
            "publisher_slot": source["publisher_slot"],
            "language": source["language"],
            "score": source["score"],
            "na_reason": source["na_reason"],
            "rationale": source["rationale"],
            "decided_by": source["decided_by"],
            "decided_at": source["decided_at"],
            "decision_version": source["decision_version"],
            "supersedes_decision_id": source["supersedes_decision_id"],
            "is_current": bool(source["is_current"]),
            "trajectory_snapshot_json": source["trajectory_snapshot_json"],
        }))
    return rows


def _prepare_case_score_results(root: Path) -> list[dict[str, Any]]:
    payload = json.loads(
        (root / "analysis/case001_final_results_v1.json").read_text(
            encoding="utf-8"
        )
    )
    if payload.get("status") != "CLOSED":
        raise ValueError("CASE-001 score results are not closed")
    frame_path = root / "analysis/case001_frame_intensity_results_v1.json"
    frame_payload = (
        json.loads(frame_path.read_text(encoding="utf-8"))
        if frame_path.exists()
        else None
    )
    frame_documents = (
        {row["document_id"]: row for row in frame_payload["records"]}
        if frame_payload
        else {}
    )
    rows = []
    for source in payload["records"]:
        details = dict(source["details"])
        frame_details = None
        if source["level"] == "DOCUMENT" and source["entity_id"] in frame_documents:
            frame_record = frame_documents[source["entity_id"]]
            frame_details = {
                "frame_score": frame_record["frame_score"],
                "component_scores": frame_record["component_scores"],
                "frame_families": frame_record["frame_families"],
                "quality_effect": "DESCRIPTIVE_ONLY_NOT_INCLUDED_IN_A",
            }
        elif frame_payload and source["level"] == "PUBLISHER_SLOT":
            frame_details = frame_payload["breakdowns"]["publisher_slot"].get(
                source["entity_id"]
            )
        elif frame_payload and source["level"] == "STRATUM":
            frame_details = frame_payload["breakdowns"]["language"].get(
                source["entity_id"]
            )
        elif frame_payload and source["level"] == "CASE":
            frame_details = {
                **frame_payload["distribution"],
                "quality_effect": "DESCRIPTIVE_ONLY_NOT_INCLUDED_IN_A",
            }
        if frame_details is not None:
            details["frame_intensity"] = frame_details
        rows.append(_finalize({
            "row_id": source["row_id"],
            "schema_version": payload["schema_version"],
            "case_id": source["case_id"],
            "level": source["level"],
            "entity_id": source["entity_id"],
            "language": source["language"],
            "label": source["label"],
            "truth_score": source["truth_score"],
            "narrative_pressure": source["narrative_pressure"],
            "aletheia_65_35": source["aletheia_65_35"],
            "aletheia_50_50": source["aletheia_50_50"],
            "aletheia_75_25": source["aletheia_75_25"],
            "truth_coverage": source["truth_coverage"],
            "narrative_coverage": source["narrative_coverage"],
            "overall_coverage": source["overall_coverage"],
            "critical_flags_json": _json(source["critical_flags"]),
            "details_json": _json(details),
            "closed_at": payload["generated_at"],
        }))
    return rows


def _prepare_case_closeout(root: Path) -> list[dict[str, Any]]:
    source = json.loads(
        (root / "analysis/case001_closeout_v1.json").read_text(encoding="utf-8")
    )
    if source.get("status") != "CLOSED":
        raise ValueError("CASE-001 closeout is not closed")
    return [_finalize({
        "row_id": source["case_id"],
        "schema_version": source["schema_version"],
        "case_id": source["case_id"],
        "status": source["status"],
        "closed_at": source["closed_at"],
        "article_components": source["article_components"],
        "trajectory_decisions": source["trajectory_decisions"],
        "score_records": source["score_records"],
        "critical_flags_json": _json(source["critical_flags"]),
        "case_scores_json": _json(source["case_scores"]),
        "artifacts_json": _json(source["artifacts"]),
    })]


def _prepare_policy_records(root: Path) -> list[dict[str, Any]]:
    registry = load_policy_registry(root)
    material = load_case_material(root)
    current_decisions = {
        row["dilemma_id"]: row
        for row in _sqlite_rows(
            root / "data/judgment_room.sqlite3",
            "SELECT * FROM adjudication_decisions WHERE is_current = 1",
        )
    }
    precedents = build_precedent_records(material, current_decisions, registry)
    rows = []
    for decision in registry["decisions"]:
        rows.append(_finalize({
            "row_id": f"POLICY_DECISION:{decision['policy_id']}",
            "case_id": registry["case_id"],
            "record_type": "POLICY_DECISION",
            "record_id": decision["policy_id"],
            "status": decision["status"],
            "component": None,
            "source": None,
            "precedent_mode": "BINDING_POLICY",
            "record_json": _json(decision),
        }))
    for application in registry["resolved_applications"]:
        rows.append(_finalize({
            "row_id": f"POLICY_APPLICATION:{application['application_id']}",
            "case_id": application["case_id"],
            "record_type": "POLICY_APPLICATION",
            "record_id": application["application_id"],
            "status": application["status"],
            "component": application["component"],
            "source": application["source"],
            "precedent_mode": "CASE_ONLY",
            "record_json": _json(application),
        }))
    for precedent in precedents:
        rows.append(_finalize({
            "row_id": f"ACTIVE_PRECEDENT:{precedent['precedent_id']}",
            "case_id": precedent["case_id"],
            "record_type": "ACTIVE_PRECEDENT",
            "record_id": precedent["precedent_id"],
            "status": "ACTIVE",
            "component": precedent["component"],
            "source": precedent["source"],
            "precedent_mode": precedent["precedent_mode"],
            "record_json": _json(precedent),
        }))
    return rows


def _aggregate_hash(rows: list[dict[str, Any]]) -> str:
    joined = "".join(sorted(row["source_row_hash"] for row in rows))
    return _sha256_bytes(joined.encode("utf-8"))


def prepare_m3_export(root: Path, output_dir: Path, bucket: str) -> dict[str, Any]:
    contracts = json.loads((root / "cloud/m3_table_contracts.json").read_text(encoding="utf-8"))
    if contracts["migration_version"] != MIGRATION_VERSION:
        raise ValueError("Migration contract version mismatch")
    if output_dir.exists():
        raise FileExistsError(output_dir)
    (output_dir / "tables").mkdir(parents=True)
    (output_dir / "schemas").mkdir()

    documents, objects = _prepare_documents(root, output_dir, bucket)
    objects.extend(_prepare_frame_artifacts(root, output_dir, bucket))
    tables = {
        "research_cases": _prepare_research_case(root),
        "corpus_documents": documents,
        "factual_anchors": _prepare_anchors(root),
        "analysis_runs": _prepare_analysis_runs(root),
        "article_measurements": _prepare_measurements(root),
        "adjudication_decisions": _prepare_adjudications(root),
        "component_score_ledger": _prepare_ledger(root),
        "trajectory_decisions": _prepare_trajectory_decisions(root),
        "case_score_results": _prepare_case_score_results(root),
        "case_closeouts": _prepare_case_closeout(root),
        "policy_records": _prepare_policy_records(root),
    }
    if set(tables) != set(contracts["tables"]):
        raise ValueError("Prepared table set does not match the contract")

    table_manifest: dict[str, Any] = {}
    for table_name, rows in tables.items():
        if table_name in FIXED_COUNTS and len(rows) != FIXED_COUNTS[table_name]:
            raise ValueError(
                f"{table_name}: expected {FIXED_COUNTS[table_name]}, found {len(rows)}"
            )
        minimum = BASELINE_MINIMUM_COUNTS.get(table_name)
        if minimum is not None and len(rows) < minimum:
            raise ValueError(
                f"{table_name}: expected at least {minimum}, found {len(rows)}"
            )
        row_ids = [row["row_id"] for row in rows]
        if len(row_ids) != len(set(row_ids)):
            raise ValueError(f"{table_name}: duplicate row_id")
        schema_fields = {field["name"] for field in contracts["tables"][table_name]["schema"]}
        for row in rows:
            if set(row) != schema_fields:
                raise ValueError(
                    f"{table_name}: row fields do not match schema: "
                    f"missing={sorted(schema_fields - set(row))}, "
                    f"extra={sorted(set(row) - schema_fields)}"
                )
        table_path = output_dir / "tables" / f"{table_name}.jsonl"
        schema_path = output_dir / "schemas" / f"{table_name}.json"
        _write_jsonl(table_path, rows)
        schema_path.write_text(
            json.dumps(contracts["tables"][table_name]["schema"], indent=2) + "\n",
            encoding="utf-8",
        )
        table_manifest[table_name] = {
            "rows": len(rows),
            "distinct_row_ids": len(set(row_ids)),
            "aggregate_source_hash": _aggregate_hash(rows),
            "jsonl_sha256": _sha256_bytes(table_path.read_bytes()),
        }

    object_ids = [item["object_key"] for item in objects]
    if len(objects) != 59 or len(set(object_ids)) != 59:
        raise ValueError("Expected 59 unique GCS objects")
    object_manifest_path = output_dir / "m3_object_manifest.jsonl"
    with object_manifest_path.open("w", encoding="utf-8") as handle:
        for item in sorted(objects, key=lambda value: value["object_key"]):
            handle.write(_json(item) + "\n")
    object_types = Counter(item["object_type"] for item in objects)
    manifest = {
        "schema_version": "1.0",
        "migration_version": MIGRATION_VERSION,
        "case_id": CANONICAL_CASE_ID,
        "bucket": bucket,
        "tables": table_manifest,
        "objects": {
            "count": len(objects),
            "by_type": dict(sorted(object_types.items())),
            "manifest_sha256": _sha256_bytes(object_manifest_path.read_bytes()),
            "compressed_bytes": sum(item["compressed_bytes"] for item in objects),
        },
    }
    (output_dir / "m3_export_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
