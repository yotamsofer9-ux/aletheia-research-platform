from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping

from .judgment_room import COMPONENT_LABELS
from .case_workspace import CaseWorkspace


EXPORT_VERSION = "multicase-pre-adjudication-v1"
FINAL_EXPORT_VERSION = "multicase-final-v1"
TABLE_NAMES = (
    "research_cases",
    "corpus_documents",
    "factual_anchors",
    "analysis_runs",
    "article_measurements",
    "component_score_ledger",
)
FINAL_TABLE_NAMES = TABLE_NAMES[:-1] + (
    "adjudication_decisions",
    "component_score_ledger",
    "trajectory_decisions",
    "case_score_results",
    "case_closeouts",
    "policy_records",
)


class CaseCloudExportError(ValueError):
    """Raised when a case cannot be exported without losing provenance."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _finalize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["source_row_hash"] = _sha256_bytes(_json(result).encode("utf-8"))
    return result


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _latest_successful_measurements(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    """Return one current successful T/N measurement for every corpus document."""
    latest: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(workspace.analysis_dir / "article_measurements_v1.jsonl"):
        document_id = record.get("document_id")
        if document_id:
            latest[document_id] = record
    records = [latest[key] for key in sorted(latest)]
    if len(records) != 27:
        raise CaseCloudExportError(
            f"expected 27 current article measurements, found {len(records)}"
        )
    failures = [row["document_id"] for row in records if row.get("status") != "SUCCESS"]
    if failures:
        raise CaseCloudExportError(
            "current article measurements are not successful: " + ", ".join(failures)
        )
    return records


def _sqlite_rows(path: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda item: item["row_id"]):
            handle.write(_json(row) + "\n")


def _aggregate_hash(rows: list[dict[str, Any]]) -> str:
    joined = "".join(sorted(row["source_row_hash"] for row in rows))
    return _sha256_bytes(joined.encode("utf-8"))


def _prepare_research_case(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    rows = _sqlite_rows(workspace.research_db, "SELECT * FROM cases")
    if len(rows) != 1 or rows[0]["case_id"] != workspace.case_id:
        raise CaseCloudExportError("workspace must contain exactly its own research case")
    source = rows[0]
    return [
        _finalize(
            {
                "row_id": workspace.case_id,
                "schema_version": "1.0",
                "case_id": workspace.case_id,
                "corpus_id": source["case_id"],
                "title": source["title"],
                "window_start": source["window_start"],
                "window_end": source["window_end"],
                "keywords_json": source["keywords_json"],
                "config_json": source["config_json"],
                "updated_at": source["updated_at"],
                "migration_version": EXPORT_VERSION,
            }
        )
    ]


def _prepare_documents(
    workspace: CaseWorkspace, output_dir: Path, bucket: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query = """
        SELECT d.*, s.name AS source_name, s.group_name, s.language,
               s.publisher_slot, s.editorial_profile, s.substitute_for
        FROM documents d JOIN sources s
          ON d.case_id = s.case_id AND d.source_id = s.source_id
        WHERE d.case_id = ? AND d.status = 'READY'
        ORDER BY d.document_id
    """
    documents = _sqlite_rows(workspace.research_db, query, (workspace.case_id,))
    if len(documents) != 27:
        raise CaseCloudExportError(f"expected 27 READY documents, found {len(documents)}")
    rows: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    object_root = output_dir / "objects"
    snapshot_prefix = "local-snapshot://"

    for source in documents:
        ref = str(source["snapshot_ref"] or "")
        if not ref.startswith(snapshot_prefix):
            raise CaseCloudExportError(f"unsafe or missing snapshot reference: {ref}")
        relative = Path(ref[len(snapshot_prefix) :])
        if relative.is_absolute() or ".." in relative.parts:
            raise CaseCloudExportError(f"unsafe snapshot path: {relative}")
        local_snapshot = workspace.snapshot_root / relative
        compressed = local_snapshot.read_bytes()
        uncompressed = gzip.decompress(compressed)
        if _sha256_bytes(uncompressed) != source["snapshot_hash"]:
            raise CaseCloudExportError(f"snapshot hash mismatch: {source['document_id']}")
        text = source["text"] or ""
        if _sha256_bytes(text.encode("utf-8")) != source["content_hash"]:
            raise CaseCloudExportError(f"normalized text hash mismatch: {source['document_id']}")

        raw_key = (
            f"raw/{workspace.case_id}/{source['source_id']}/{source['snapshot_hash']}.gz"
        )
        raw_target = object_root / raw_key
        raw_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_snapshot, raw_target)

        normalized_key = (
            f"normalized/{workspace.case_id}/{source['source_id']}/"
            f"{source['document_id']}-{source['content_hash']}.json.gz"
        )
        normalized_target = object_root / normalized_key
        normalized_target.parent.mkdir(parents=True, exist_ok=True)
        normalized_payload = _json(
            {
                "schema_version": "1.0",
                "case_id": workspace.case_id,
                "corpus_id": workspace.case_id,
                "document_id": source["document_id"],
                "content_hash": source["content_hash"],
                "text": text,
            }
        ).encode("utf-8")
        normalized_target.write_bytes(gzip.compress(normalized_payload, mtime=0))

        for kind, key, path, payload_hash in (
            ("RAW_SNAPSHOT", raw_key, raw_target, source["snapshot_hash"]),
            ("NORMALIZED_PAYLOAD", normalized_key, normalized_target, source["content_hash"]),
        ):
            object_bytes = path.read_bytes()
            objects.append(
                {
                    "object_type": kind,
                    "document_id": source["document_id"],
                    "object_key": key,
                    "gcs_uri": f"gs://{bucket}/{key}",
                    "staged_path": str(path.relative_to(output_dir)),
                    "compressed_bytes": len(object_bytes),
                    "compressed_sha256": _sha256_bytes(object_bytes),
                    "payload_sha256": payload_hash,
                    "content_type": "application/gzip",
                }
            )

        rows.append(
            _finalize(
                {
                    "row_id": source["document_id"],
                    "schema_version": "1.0",
                    "case_id": workspace.case_id,
                    "corpus_id": workspace.case_id,
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
                }
            )
        )
    return rows, objects


def _prepare_anchors(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    rows = []
    for record in _read_jsonl(workspace.anchors_dir / "factual_anchors_v1.jsonl"):
        rows.append(
            _finalize(
                {
                    "row_id": record["anchor_id"],
                    "schema_version": record["schema_version"],
                    "case_id": workspace.case_id,
                    "anchor_id": record["anchor_id"],
                    "family": record["family"],
                    "proposition_type": record["proposition_type"],
                    "canonical_proposition": record["canonical_proposition"],
                    "scope_notes": record["scope_notes"],
                    "assessment_count": len(record["assessments"]),
                    "record_json": _json(record),
                }
            )
        )
    review_summary = _read_json(workspace.anchors_dir / "model_review_summary.json")
    expected_count = review_summary.get("anchors_reviewed")
    if not isinstance(expected_count, int) or expected_count < 1:
        raise CaseCloudExportError("anchor review summary has no valid reviewed count")
    if len(rows) != expected_count:
        raise CaseCloudExportError(
            f"expected {expected_count} reviewed anchors, found {len(rows)}"
        )
    if len({row["anchor_id"] for row in rows}) != len(rows):
        raise CaseCloudExportError("reviewed anchors contain duplicate ids")
    return rows


def _run_row(
    *,
    workspace: CaseWorkspace,
    row_id: str,
    prompt_version: str,
    schema_version: str,
    run_date: str,
    status: str,
    requested: int | None,
    completed: int | None,
    model_calls: int | None,
    total_tokens: int | None,
    elapsed_seconds: float | None,
    peak_rss_mb: float | None,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    return _finalize(
        {
            "row_id": f"{workspace.case_id}:{row_id}",
            "schema_version": "1.0",
            "case_id": workspace.case_id,
            "prompt_version": prompt_version,
            "measurement_schema_version": schema_version,
            "run_date": run_date,
            "status": status,
            "requested": requested,
            "completed": completed,
            "model_calls": model_calls,
            "paid_repair_calls": 0,
            "total_tokens": total_tokens,
            "elapsed_seconds": elapsed_seconds,
            "peak_rss_mb": peak_rss_mb,
            "summary_json": _json(summary),
        }
    )


def _prepare_analysis_runs(
    workspace: CaseWorkspace, *, final: bool = False
) -> list[dict[str, Any]]:
    anchor_summary = _read_json(workspace.anchors_dir / "model_review_summary.json")
    measurements = _latest_successful_measurements(workspace)
    article_summary = _read_json(workspace.analysis_dir / "article_measurements_v1_summary.json")
    debate = _read_json(workspace.analysis_dir / "debate_candidate_v1.json")
    dilemma_path = workspace.adjudication_dir / "dilemmas_v1.json"
    if not dilemma_path.exists():
        # A case whose entire appellate docket was resolved autonomously has
        # no human-facing packet to materialize.  The frozen pre-review packet
        # is still the canonical record of what the second review examined.
        dilemma_path = (
            workspace.adjudication_dir / "dilemmas_pre_second_review_v1.json"
        )
    dilemma_packet = _read_json(dilemma_path)
    review_queue = _read_json(workspace.analysis_dir / "article_review_queue_v1.json")
    second_manifest = _read_json(workspace.adjudication_dir / "second_review_manifest_v1.json")
    ballots = _read_jsonl(workspace.adjudication_dir / "second_review_ballots_v1.jsonl")

    article_tokens = sum(int((row.get("usage") or {}).get("total_tokens") or 0) for row in measurements)
    elapsed = sum(
        float((row.get("resource_usage") or {}).get("elapsed_seconds_total_run") or 0)
        for row in measurements
    )
    peak = max(
        float((row.get("resource_usage") or {}).get("peak_rss_mb") or 0)
        for row in measurements
    )
    debate_usage = debate.get("usage") or {}
    ballot_tokens = sum(int((row.get("usage") or {}).get("total_tokens") or 0) for row in ballots)
    successful_ballots = [row for row in ballots if row.get("status") == "SUCCESS"]
    current_ballots = {
        (row["document_id"], int(row["ballot_number"])): row
        for row in successful_ballots
    }
    dilemma_count = len(dilemma_packet.get("dilemmas") or [])
    resolved_without_dispute = len(dilemma_packet.get("resolved_without_dispute") or [])
    challenge_completed = dilemma_count + resolved_without_dispute
    challenge_requested = int(
        (review_queue.get("summary") or {}).get("routed_components")
        or challenge_completed
    )
    effective_second_manifest = {
        **second_manifest,
        "documents": len({key[0] for key in current_ballots}),
        "ballots": len(current_ballots),
        "dilemmas": dilemma_count,
    }
    second_summary = {
        "manifest": effective_second_manifest,
        "ballot_status_counts": dict(Counter(row["status"] for row in ballots)),
        "historical_ballot_attempts": len(ballots),
    }
    rows = [
        _run_row(
            workspace=workspace,
            row_id="anchor-review-v1",
            prompt_version=anchor_summary["prompt_version"],
            schema_version="anchor-review-0.1",
            run_date=anchor_summary["generated_at"],
            status="SUCCESS",
            requested=anchor_summary["anchors_reviewed"],
            completed=anchor_summary["anchors_reviewed"],
            model_calls=anchor_summary["anchors_reviewed"],
            total_tokens=anchor_summary["total_tokens"],
            elapsed_seconds=None,
            peak_rss_mb=None,
            summary=anchor_summary,
        ),
        _run_row(
            workspace=workspace,
            row_id="article-analysis-v1",
            prompt_version=measurements[0]["prompt_version"],
            schema_version=(measurements[0]["measurement"])["schema_version"],
            run_date=max(row["recorded_at"] for row in measurements),
            status="SUCCESS",
            requested=27,
            completed=27,
            model_calls=27,
            total_tokens=article_tokens,
            elapsed_seconds=round(elapsed, 3),
            peak_rss_mb=round(peak, 2),
            summary=article_summary,
        ),
        _run_row(
            workspace=workspace,
            row_id="methodological-challenge-v1",
            prompt_version=dilemma_packet.get("prompt_version") or "methodological-challenge-v1",
            schema_version="dilemma-1.0",
            run_date=max(row["recorded_at"] for row in measurements),
            status="SUCCESS",
            requested=challenge_requested,
            completed=challenge_completed,
            model_calls=len(debate.get("batch_candidates") or [1]),
            total_tokens=int(debate_usage.get("total_tokens") or 0),
            elapsed_seconds=None,
            peak_rss_mb=None,
            summary={
                "generated_dilemmas": dilemma_count,
                "resolved_without_dispute": resolved_without_dispute,
                "usage": debate_usage,
            },
        ),
        _run_row(
            workspace=workspace,
            row_id="autonomous-second-review-v1",
            prompt_version=second_manifest["protocol_version"],
            schema_version="appellate-review-1.0",
            run_date=max(row["recorded_at"] for row in ballots),
            status="SUCCESS" if final else "AWAITING_ADJUDICATION",
            requested=len(current_ballots),
            completed=len(current_ballots),
            model_calls=len(ballots),
            total_tokens=ballot_tokens,
            elapsed_seconds=None,
            peak_rss_mb=None,
            summary=second_summary,
        ),
    ]
    if final:
        t4 = _read_json(workspace.results_dir / "t4_autonomous_review_v1.json")
        work = _read_json(workspace.results_dir / "t4_autonomous_review_work_v1.json")
        usages = [t4.get("initial_usage") or {}, t4.get("independent_audit_usage") or {}]
        usages.extend(item.get("usage") or {} for item in t4.get("arbitration", []))
        usages.extend(item.get("usage") or {} for item in work.get("repair_usage", []))
        rows.append(
            _run_row(
                workspace=workspace,
                row_id="t4-autonomous-review-v1",
                prompt_version=t4["protocol_version"],
                schema_version="t4-review-1.0",
                run_date=t4["generated_at"],
                status="SUCCESS",
                requested=t4["summary"]["trajectories"],
                completed=t4["summary"]["auto_resolved"],
                model_calls=len(usages),
                total_tokens=sum(int(item.get("total_tokens") or 0) for item in usages),
                elapsed_seconds=None,
                peak_rss_mb=None,
                summary={
                    "summary": t4["summary"],
                    "known_usage": usages,
                    "failed_pre-schema_calls_excluded": True,
                },
            )
        )
    return rows


def _prepare_measurements(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    rows = []
    records = _latest_successful_measurements(workspace)
    for record in records:
        usage = record.get("usage") or {}
        resources = record.get("resource_usage") or {}
        identity = "|".join(
            str(record.get(key) or "")
            for key in ("document_id", "prompt_version", "input_hash", "recorded_at", "status")
        )
        rows.append(
            _finalize(
                {
                    "row_id": _sha256_bytes(identity.encode("utf-8")),
                    "schema_version": record["schema_version"],
                    "case_id": workspace.case_id,
                    "document_id": record["document_id"],
                    "source_id": record["source_id"],
                    "language": record["language"],
                    "sample_phase": record["sample_phase"],
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
                }
            )
        )
    if len(rows) != 27:
        raise CaseCloudExportError(f"expected 27 article measurements, found {len(rows)}")
    return rows


def _prepare_ledger(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    documents = {
        row["document_id"]: row
        for row in _sqlite_rows(
            workspace.research_db,
            """
            SELECT d.document_id, d.source_id, d.title, d.published_at, d.sample_phase,
                   s.name AS source_name
            FROM documents d JOIN sources s
              ON d.case_id = s.case_id AND d.source_id = s.source_id
            WHERE d.case_id = ?
            """,
            (workspace.case_id,),
        )
    }
    measurements = _latest_successful_measurements(workspace)
    principal = _read_json(workspace.adjudication_dir / "principal_review_v1.json")
    auto = {item["dilemma_id"]: item for item in principal["auto_resolutions"]}
    human = {item["dilemma_id"]: item for item in principal["human_dilemmas"]}
    receipt_path = workspace.results_dir / "run_receipt_v1.json"
    recorded_at = (
        _read_json(receipt_path)["recorded_at"]
        if receipt_path.exists()
        else max(row["recorded_at"] for row in measurements)
    )
    rows: list[dict[str, Any]] = []

    for measurement_record in measurements:
        document_id = measurement_record["document_id"]
        document = documents[document_id]
        measurement = measurement_record["measurement"]
        for component_id, component in measurement["components"].items():
            dilemma_id = f"{workspace.case_id}:{document_id}:{component_id}"
            score = component.get("score")
            na_reason = component.get("na_reason")
            rationale = component["rationale"]
            authority = "MODEL_ACCEPTED"
            status = "AUTO_ACCEPTED"
            outcome = "INITIAL_ANALYSIS_ACCEPTED"
            is_effective = True
            if dilemma_id in auto:
                resolution = auto[dilemma_id]
                score = resolution.get("final_score")
                na_reason = resolution.get("final_na_reason")
                rationale = resolution["second_review"]["ballot_1"]["rationale_he"]
                authority = "AUTONOMOUS_SECOND_REVIEW"
                status = "AUTO_RESOLVED"
                outcome = resolution["resolution"]
            elif dilemma_id in human:
                authority = "MODEL_PROVISIONAL"
                status = "AWAITING_ADJUDICATION"
                outcome = human[dilemma_id]["principal_review"]["reason"]
                is_effective = False
            record_id = f"{workspace.case_id}:{document_id}:{component_id}:PRE_ADJUDICATION_V1"
            rows.append(
                _finalize(
                    {
                        "row_id": record_id,
                        "case_id": workspace.case_id,
                        "record_id": record_id,
                        "document_id": document_id,
                        "source_id": document["source_id"],
                        "source_name": document["source_name"],
                        "published_at": document["published_at"] or "",
                        "sample_phase": document["sample_phase"],
                        "component": component_id,
                        "component_label": COMPONENT_LABELS[component_id],
                        "authority": authority,
                        "score": score,
                        "na_reason": na_reason,
                        "rationale": rationale,
                        "status": status,
                        "outcome": outcome,
                        "prompt_version": measurement_record["prompt_version"],
                        "model_confidence": component.get("confidence"),
                        "evidence_span_json": _json(component.get("evidence_spans") or []),
                        "anchor_refs_json": _json(component.get("anchor_refs") or []),
                        "decision_id": None,
                        "decision_version": None,
                        "decided_by": None,
                        "recorded_at": recorded_at,
                        "supersedes_record_id": None,
                        "is_current": True,
                        "is_effective": is_effective,
                    }
                )
            )
    if len(rows) != 216:
        raise CaseCloudExportError(f"expected 216 component rows, found {len(rows)}")
    counts = Counter(row["status"] for row in rows)
    expected_counts = Counter(
        {
            "AUTO_ACCEPTED": 216 - len(auto) - len(human),
            "AUTO_RESOLVED": len(auto),
            "AWAITING_ADJUDICATION": len(human),
        }
    )
    if counts != expected_counts:
        raise CaseCloudExportError(f"unexpected component resolution counts: {dict(counts)}")
    return rows


def _prepare_final_ledger(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    source_rows = _sqlite_rows(
        workspace.adjudication_dir / "judgment_room.sqlite3",
        "SELECT * FROM component_score_ledger ORDER BY record_id",
    )
    rows = []
    for source in source_rows:
        row = dict(source)
        row["row_id"] = row["record_id"]
        row["is_current"] = bool(row["is_current"])
        row["is_effective"] = bool(row["is_effective"])
        rows.append(_finalize(row))

    frame_path = workspace.results_dir / "frame_intensity_results_v1.json"
    frame_rows = 0
    if frame_path.exists():
        frame_payload = _read_json(frame_path)
        if (
            frame_payload.get("status") != "COMPLETE"
            or frame_payload.get("documents") != 27
            or frame_payload.get("component_rows") != 135
            or frame_payload.get("unresolved_human_reviews") != 0
        ):
            raise CaseCloudExportError("frame-intensity results are not complete")
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
                    f"{workspace.case_id}:{record['document_id']}:"
                    f"{component_id}:FRAME_V1"
                )
                rows.append(
                    _finalize(
                        {
                            "row_id": record_id,
                            "case_id": workspace.case_id,
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
                        }
                    )
                )
                frame_rows += 1
    closeout = _read_json(workspace.results_dir / "closeout_v1.json")
    expected_human = int(closeout.get("human_article_decisions") or 0)
    # Revised cases retain superseded model proposals as audit history.  The
    # final invariant is therefore 216 *current* model proposals (one for each
    # T/N component), not 216 lifetime proposal rows.
    current_model_rows = sum(
        row.get("authority") == "MODEL_PROPOSAL" and row.get("is_current")
        for row in rows
    )
    effective_human_rows = sum(
        row.get("authority") == "HUMAN_DECISION" and row.get("is_effective")
        for row in rows
    )
    expected_rows = len(source_rows) + frame_rows
    expected_effective = 216 + frame_rows
    if (
        len(rows) != expected_rows
        or current_model_rows != 216
        or effective_human_rows != expected_human
        or sum(row["is_effective"] for row in rows) != expected_effective
    ):
        raise CaseCloudExportError(
            f"final ledger requires {expected_rows} audit rows and "
            f"{expected_effective} effective rows"
        )
    return rows


def _prepare_adjudications(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    source_rows = _sqlite_rows(
        workspace.adjudication_dir / "judgment_room.sqlite3",
        "SELECT * FROM adjudication_decisions ORDER BY dilemma_id, decision_version",
    )
    rows = []
    for source in source_rows:
        row = dict(source)
        row["row_id"] = row["decision_id"]
        row["is_current"] = bool(row["is_current"])
        rows.append(_finalize(row))
    closeout = _read_json(workspace.results_dir / "closeout_v1.json")
    expected_current = int(closeout.get("human_article_decisions") or 0)
    current_count = sum(row["is_current"] for row in rows)
    if current_count != expected_current:
        raise CaseCloudExportError(
            "final export human-decision count does not match the closeout"
        )
    return rows


def _prepare_trajectory_decisions(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    source_rows = _sqlite_rows(
        workspace.adjudication_dir / "judgment_room.sqlite3",
        "SELECT * FROM trajectory_decisions ORDER BY trajectory_id, decision_version",
    )
    rows = []
    for source in source_rows:
        row = dict(source)
        row["row_id"] = row["decision_id"]
        row["is_current"] = bool(row["is_current"])
        rows.append(_finalize(row))
    if len(rows) != 9 or not all(row["is_current"] for row in rows):
        raise CaseCloudExportError("final export requires nine current T4 decisions")
    return rows


def _prepare_case_score_results(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    payload = _read_json(workspace.results_dir / "final_results_v1.json")
    if payload.get("status") != "CLOSED":
        raise CaseCloudExportError("case score results are not closed")
    frame_path = workspace.results_dir / "frame_intensity_results_v1.json"
    frame_payload = _read_json(frame_path) if frame_path.exists() else None
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
                "quality_effect": frame_payload.get(
                    "quality_effect", "INDEPENDENT_CORE_AXIS_NO_COMPOSITE"
                ),
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
                "quality_effect": frame_payload.get(
                    "quality_effect", "INDEPENDENT_CORE_AXIS_NO_COMPOSITE"
                ),
            }
        if frame_details is not None:
            details["frame_intensity"] = frame_details
        rows.append(
            _finalize(
                {
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
                }
            )
        )
    if len(rows) != 40:
        raise CaseCloudExportError(f"expected 40 final score rows, found {len(rows)}")
    return rows


def _prepare_case_closeout(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    source = _read_json(workspace.results_dir / "closeout_v1.json")
    if source.get("status") != "CLOSED":
        raise CaseCloudExportError("case closeout is not closed")
    return [
        _finalize(
            {
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
            }
        )
    ]


def _prepare_policy_records(workspace: CaseWorkspace) -> list[dict[str, Any]]:
    decisions = _sqlite_rows(
        workspace.adjudication_dir / "judgment_room.sqlite3",
        "SELECT * FROM adjudication_decisions WHERE is_current = 1 ORDER BY dilemma_id",
    )
    rows = []
    for source in decisions:
        record = {
            "decision_id": source["decision_id"],
            "dilemma_id": source["dilemma_id"],
            "component": source["component"],
            "outcome": source["outcome"],
            "final_score": source["final_score"],
            "final_na_reason": source["final_na_reason"],
            "rationale": source["rationale"],
            "policy_rule": source["policy_rule"],
            "policy_scope": json.loads(source["policy_scope_json"]),
            "precedent_mode": source["precedent_mode"],
            "decided_by": source["decided_by"],
            "decided_at": source["decided_at"],
        }
        rows.append(
            _finalize(
                {
                    "row_id": f"ACTIVE_PRECEDENT:{source['decision_id']}",
                    "case_id": workspace.case_id,
                    "record_type": "ACTIVE_PRECEDENT",
                    "record_id": source["decision_id"],
                    "status": "ACTIVE",
                    "component": source["component"],
                    "source": source["decided_by"],
                    "precedent_mode": source["precedent_mode"],
                    "record_json": _json(record),
                }
            )
        )
    closeout = _read_json(workspace.results_dir / "closeout_v1.json")
    expected_current = int(closeout.get("human_article_decisions") or 0)
    if len(rows) != expected_current:
        raise CaseCloudExportError(
            "policy-record count does not match the current human decisions"
        )
    return rows


def _stage_case_artifacts(
    workspace: CaseWorkspace,
    output_dir: Path,
    bucket: str,
    *,
    artifact_version: str = "pre-adjudication-v1",
) -> list[dict[str, Any]]:
    object_root = output_dir / "objects"
    prefix = f"case-artifacts/{workspace.case_id}/{artifact_version}"
    objects: list[dict[str, Any]] = []
    for source in sorted(workspace.case_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(workspace.case_root)
        if relative.parts[0] in {"snapshots", "cloud_stage"}:
            continue
        key = f"{prefix}/{relative.as_posix()}"
        target = object_root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        payload = target.read_bytes()
        objects.append(
            {
                "object_type": "CASE_ARTIFACT",
                "document_id": None,
                "object_key": key,
                "gcs_uri": f"gs://{bucket}/{key}",
                "staged_path": str(target.relative_to(output_dir)),
                "compressed_bytes": len(payload),
                "compressed_sha256": _sha256_bytes(payload),
                "payload_sha256": _sha256_bytes(payload),
                "content_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            }
        )
    return objects


def prepare_pre_adjudication_export(
    root: Path,
    workspace: CaseWorkspace,
    output_dir: Path,
    bucket: str,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(output_dir)
    (output_dir / "tables").mkdir(parents=True)
    (output_dir / "schemas").mkdir()
    contracts = _read_json(root / "cloud/m3_table_contracts.json")

    documents, objects = _prepare_documents(workspace, output_dir, bucket)
    objects.extend(_stage_case_artifacts(workspace, output_dir, bucket))
    tables = {
        "research_cases": _prepare_research_case(workspace),
        "corpus_documents": documents,
        "factual_anchors": _prepare_anchors(workspace),
        "analysis_runs": _prepare_analysis_runs(workspace),
        "article_measurements": _prepare_measurements(workspace),
        "component_score_ledger": _prepare_ledger(workspace),
    }
    if tuple(tables) != TABLE_NAMES:
        raise CaseCloudExportError("prepared table set does not match the pre-adjudication contract")

    table_manifest: dict[str, Any] = {}
    for table_name, rows in tables.items():
        row_ids = [row["row_id"] for row in rows]
        if len(row_ids) != len(set(row_ids)):
            raise CaseCloudExportError(f"{table_name}: duplicate row_id")
        schema = contracts["tables"][table_name]["schema"]
        fields = {field["name"] for field in schema}
        for row in rows:
            if set(row) != fields:
                raise CaseCloudExportError(
                    f"{table_name}: schema mismatch; missing={sorted(fields - set(row))}; "
                    f"extra={sorted(set(row) - fields)}"
                )
        table_path = output_dir / "tables" / f"{table_name}.jsonl"
        schema_path = output_dir / "schemas" / f"{table_name}.json"
        _write_jsonl(table_path, rows)
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        table_manifest[table_name] = {
            "rows": len(rows),
            "distinct_row_ids": len(set(row_ids)),
            "aggregate_source_hash": _aggregate_hash(rows),
            "jsonl_sha256": _sha256_bytes(table_path.read_bytes()),
            "fields": [field["name"] for field in schema],
        }

    object_ids = [item["object_key"] for item in objects]
    if len(set(object_ids)) != len(objects):
        raise CaseCloudExportError("duplicate GCS object key")
    object_manifest = output_dir / "object_manifest.jsonl"
    with object_manifest.open("w", encoding="utf-8") as handle:
        for item in sorted(objects, key=lambda value: value["object_key"]):
            handle.write(_json(item) + "\n")
    manifest = {
        "schema_version": "1.0",
        "export_version": EXPORT_VERSION,
        "case_id": workspace.case_id,
        "phase": "PRE_ADJUDICATION",
        "bucket": bucket,
        "tables": table_manifest,
        "objects": {
            "count": len(objects),
            "by_type": dict(sorted(Counter(item["object_type"] for item in objects).items())),
            "manifest_sha256": _sha256_bytes(object_manifest.read_bytes()),
            "bytes": sum(item["compressed_bytes"] for item in objects),
        },
    }
    (output_dir / "export_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def prepare_final_export(
    root: Path,
    workspace: CaseWorkspace,
    output_dir: Path,
    bucket: str,
) -> dict[str, Any]:
    """Prepare a complete, case-scoped closed-case export without touching other cases."""
    if output_dir.exists():
        raise FileExistsError(output_dir)
    (output_dir / "tables").mkdir(parents=True)
    (output_dir / "schemas").mkdir()
    contracts = _read_json(root / "cloud/m3_table_contracts.json")

    documents, objects = _prepare_documents(workspace, output_dir, bucket)
    objects.extend(
        _stage_case_artifacts(
            workspace,
            output_dir,
            bucket,
            artifact_version="final-v1",
        )
    )
    tables = {
        "research_cases": _prepare_research_case(workspace),
        "corpus_documents": documents,
        "factual_anchors": _prepare_anchors(workspace),
        "analysis_runs": _prepare_analysis_runs(workspace, final=True),
        "article_measurements": _prepare_measurements(workspace),
        "adjudication_decisions": _prepare_adjudications(workspace),
        "component_score_ledger": _prepare_final_ledger(workspace),
        "trajectory_decisions": _prepare_trajectory_decisions(workspace),
        "case_score_results": _prepare_case_score_results(workspace),
        "case_closeouts": _prepare_case_closeout(workspace),
        "policy_records": _prepare_policy_records(workspace),
    }
    if tuple(tables) != FINAL_TABLE_NAMES:
        raise CaseCloudExportError("prepared table set does not match the final contract")

    table_manifest: dict[str, Any] = {}
    for table_name, rows in tables.items():
        row_ids = [row["row_id"] for row in rows]
        if len(row_ids) != len(set(row_ids)):
            raise CaseCloudExportError(f"{table_name}: duplicate row_id")
        schema = contracts["tables"][table_name]["schema"]
        fields = {field["name"] for field in schema}
        for row in rows:
            if set(row) != fields:
                raise CaseCloudExportError(
                    f"{table_name}: schema mismatch; missing={sorted(fields - set(row))}; "
                    f"extra={sorted(set(row) - fields)}"
                )
        table_path = output_dir / "tables" / f"{table_name}.jsonl"
        schema_path = output_dir / "schemas" / f"{table_name}.json"
        _write_jsonl(table_path, rows)
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        table_manifest[table_name] = {
            "rows": len(rows),
            "distinct_row_ids": len(set(row_ids)),
            "aggregate_source_hash": _aggregate_hash(rows),
            "jsonl_sha256": _sha256_bytes(table_path.read_bytes()),
            "fields": [field["name"] for field in schema],
        }

    object_ids = [item["object_key"] for item in objects]
    if len(set(object_ids)) != len(objects):
        raise CaseCloudExportError("duplicate GCS object key")
    object_manifest = output_dir / "object_manifest.jsonl"
    with object_manifest.open("w", encoding="utf-8") as handle:
        for item in sorted(objects, key=lambda value: value["object_key"]):
            handle.write(_json(item) + "\n")
    manifest = {
        "schema_version": "1.0",
        "export_version": FINAL_EXPORT_VERSION,
        "case_id": workspace.case_id,
        "phase": "FINAL",
        "bucket": bucket,
        "tables": table_manifest,
        "objects": {
            "count": len(objects),
            "by_type": dict(
                sorted(Counter(item["object_type"] for item in objects).items())
            ),
            "manifest_sha256": _sha256_bytes(object_manifest.read_bytes()),
            "bytes": sum(item["compressed_bytes"] for item in objects),
        },
    }
    (output_dir / "export_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
