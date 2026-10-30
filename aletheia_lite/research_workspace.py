from __future__ import annotations

from collections import Counter
import csv
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import platform
import resource
import sqlite3
import tempfile
from typing import Any

from .cloud_migration import prepare_m3_export
from .judgment_room import CaseMaterial, decision_status


CANONICAL_RECEIPT = Path("cloud/case001_m3_migration_receipt.json")
RUNTIME_SYNC_STATE = Path("data/m4_sync_state.json")
CORPUS_EXPORT = Path("exports/case001_research_pilot_27.csv")
LANGUAGE_LABELS_HE = {"he": "עברית", "en": "אנגלית", "ar": "ערבית"}
PHASE_LABELS_HE = {
    "INITIAL_REPORT": "דיווח ראשוני",
    "EARLY_UPDATE": "עדכון מוקדם",
    "LATER_INVESTIGATION": "בדיקה מאוחרת",
}
DOCUMENT_STATUS_HE = {
    "NOT_ANALYZED": "טרם נותחה",
    "NEEDS_REVIEW": "דורשת שיפוט",
    "REVIEWED": "הושלמה ונבדקה",
}


@dataclass(frozen=True)
class WorkspaceOverview:
    case_id: str
    corpus_documents: int
    analyzed_documents: int
    not_analyzed_documents: int
    reviewed_documents: int
    documents_needing_review: int
    effective_components: int
    current_decisions: int
    ledger_records: int
    active_precedents: int
    precedent_conflicts: int
    documents: list[dict[str, Any]]
    matrix: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_corpus_index(root: Path) -> list[dict[str, Any]]:
    """Read bounded corpus metadata while discarding article bodies immediately."""
    path = root / CORPUS_EXPORT
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            rows.append({
                "document_id": source["document_id"],
                "source_id": source["source_id"],
                "source_name": source["source_name"],
                "group_name": source["group_name"],
                "language": source["language"],
                "publisher_slot": source["publisher_slot"],
                "substitute_for": source["substitute_for"],
                "url": source["url"],
                "title": source["title"],
                "published_at": source["published_at"],
                "sample_phase": source["sample_phase"],
                "content_hash": source["content_hash"],
                "snapshot_hash": source["snapshot_hash"],
                "truncated": source["truncated"] == "1",
                "text_chars": len(source.get("text") or ""),
            })
    document_ids = [row["document_id"] for row in rows]
    if len(rows) != 27 or len(set(document_ids)) != 27:
        raise ValueError("CASE-001 overview requires exactly 27 unique documents")
    rows.sort(key=lambda row: (
        row["language"], row["publisher_slot"], row["published_at"],
        row["document_id"],
    ))
    return rows


def build_workspace_overview(
    root: Path,
    material: CaseMaterial,
    decisions: dict[str, dict[str, Any]],
    *,
    effective_rows: list[dict[str, Any]],
    ledger_records: int,
    active_precedents: int,
    precedent_conflicts: int,
) -> WorkspaceOverview:
    corpus = load_corpus_index(root)
    analyzed_ids = {
        document["document_id"] for document in material.documents
    } | {
        row["document_id"] for row in material.reference_scores
    }
    dilemma_by_document: dict[str, list[dict[str, Any]]] = {}
    for dilemma in material.dilemmas:
        dilemma_by_document.setdefault(dilemma["document_id"], []).append(dilemma)

    documents = []
    for row in corpus:
        document_id = row["document_id"]
        if document_id not in analyzed_ids:
            status = "NOT_ANALYZED"
        else:
            dilemmas = dilemma_by_document.get(document_id, [])
            open_dilemmas = [
                item for item in dilemmas
                if decision_status(decisions.get(item["dilemma_id"])) != "DECIDED"
                and item.get("principal_review_required", True)
            ]
            status = "NEEDS_REVIEW" if open_dilemmas else "REVIEWED"
        documents.append({
            **row,
            "language_he": LANGUAGE_LABELS_HE[row["language"]],
            "phase_he": PHASE_LABELS_HE[row["sample_phase"]],
            "research_status": status,
            "research_status_he": DOCUMENT_STATUS_HE[status],
        })

    matrix_counter = Counter(
        (row["language"], row["sample_phase"], row["research_status"])
        for row in documents
    )
    matrix = []
    for language in ("he", "en", "ar"):
        for phase in ("INITIAL_REPORT", "EARLY_UPDATE", "LATER_INVESTIGATION"):
            reviewed = matrix_counter[(language, phase, "REVIEWED")]
            needs_review = matrix_counter[(language, phase, "NEEDS_REVIEW")]
            not_analyzed = matrix_counter[(language, phase, "NOT_ANALYZED")]
            matrix.append({
                "language": language,
                "language_he": LANGUAGE_LABELS_HE[language],
                "phase": phase,
                "phase_he": PHASE_LABELS_HE[phase],
                "reviewed": reviewed,
                "needs_review": needs_review,
                "not_analyzed": not_analyzed,
                "total": reviewed + needs_review + not_analyzed,
            })

    status_counts = Counter(row["research_status"] for row in documents)
    return WorkspaceOverview(
        case_id=material.case_id,
        corpus_documents=len(documents),
        analyzed_documents=len(analyzed_ids),
        not_analyzed_documents=status_counts["NOT_ANALYZED"],
        reviewed_documents=status_counts["REVIEWED"],
        documents_needing_review=status_counts["NEEDS_REVIEW"],
        effective_components=sum(
            row["status"] != "PENDING" for row in effective_rows
        ),
        current_decisions=len(decisions),
        ledger_records=ledger_records,
        active_precedents=active_precedents,
        precedent_conflicts=precedent_conflicts,
        documents=documents,
        matrix=matrix,
    )



def _priority_for_dilemma(
    dilemma: dict[str, Any], decision: dict[str, Any] | None
) -> tuple[int, str]:
    status = decision_status(decision)
    if status == "DECIDED":
        return 90, "היסטוריה — הוכרע"
    if status == "DEFERRED":
        return 10, "הכרעה שנדחתה"
    reasons = set(dilemma.get("model", {}).get("reasons", []))
    if "CROSS_FIELD_CAUSAL_CONFLICT" in reasons:
        return 20, "סתירה בין רכיבים"
    if any(reason.startswith("CRITICAL_") for reason in reasons):
        return 30, "רכיב קריטי"
    if dilemma.get("model", {}).get("score") is None:
        return 40, "N/A או תשתית חסרה"
    if dilemma.get("model", {}).get("confidence") in {"LOW", "MEDIUM"}:
        return 50, "ביטחון נמוך או בינוני"
    return 60, "ביקורת אנושית"


def build_deterministic_queue(
    material: CaseMaterial,
    decisions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for dilemma in material.dilemmas:
        decision = decisions.get(dilemma["dilemma_id"])
        status = decision_status(decision)
        if (
            status != "DECIDED"
            and not dilemma.get("principal_review_required", True)
        ):
            continue
        priority, reason_he = _priority_for_dilemma(dilemma, decision)
        article = dilemma.get("article", {})
        rows.append({
            "dilemma_id": dilemma["dilemma_id"],
            "priority": priority,
            "priority_reason_he": reason_he,
            "status": status,
            "document_id": dilemma["document_id"],
            "source": article.get("source_name") or dilemma["source_id"],
            "language": dilemma["language"],
            "sample_phase": dilemma["sample_phase"],
            "published_at": article.get("published_at", ""),
            "component": dilemma["component"],
            "model_score": dilemma["model"].get("score"),
            "model_confidence": dilemma["model"].get("confidence"),
            "routing_reasons": dilemma["model"].get("reasons", []),
        })
    rows.sort(key=lambda row: (
        row["priority"], row["published_at"], row["source"], row["component"],
        row["dilemma_id"],
    ))
    return rows


def _receipt_table_values(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for name, table in receipt.get("tables", {}).items():
        result[name] = {
            "rows": int(table.get("rows", table.get("row_count", 0))),
            "distinct_row_ids": int(table.get("distinct_row_ids", 0)),
            "aggregate_source_hash": table.get("aggregate_source_hash", "").lower(),
        }
    return result


def load_canonical_sync_record(root: Path) -> dict[str, Any]:
    runtime = root / RUNTIME_SYNC_STATE
    if runtime.exists():
        return _read_json(runtime)
    receipt = _read_json(root / CANONICAL_RECEIPT)
    return {
        "schema_version": "1.0",
        "status": receipt["status"],
        "published_at": receipt["verified_at"],
        "manifest": {
            "tables": _receipt_table_values(receipt),
            "objects": receipt["objects"],
        },
        "source": str(CANONICAL_RECEIPT),
    }


def write_runtime_sync_record(
    root: Path,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    *,
    operation: str,
    backup_path: Path | None = None,
) -> Path:
    path = root / RUNTIME_SYNC_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "status": "VERIFIED",
        "operation": operation,
        "published_at": receipt["verified_at"],
        "manifest_fingerprint": manifest_fingerprint(manifest),
        "manifest": manifest,
        "cloud_verification": receipt,
        "backup_path": str(backup_path) if backup_path else None,
        "source": str(path.relative_to(root)),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def local_snapshot_manifest(root: Path) -> dict[str, Any]:
    bucket = _read_json(root / CANONICAL_RECEIPT)["bucket"]
    with tempfile.TemporaryDirectory(prefix="aletheia-m4-status-") as temporary:
        output_dir = Path(temporary) / "snapshot"
        return prepare_m3_export(root, output_dir, bucket)


def manifest_fingerprint(manifest: dict[str, Any]) -> str:
    essential = {
        "case_id": manifest["case_id"],
        "migration_version": manifest["migration_version"],
        "objects": manifest["objects"],
        "tables": manifest["tables"],
    }
    return _sha256(_canonical_json(essential).encode("utf-8"))


def workspace_sync_status(root: Path) -> dict[str, Any]:
    local = local_snapshot_manifest(root)
    canonical_record = load_canonical_sync_record(root)
    canonical = canonical_record["manifest"]
    canonical_tables = _receipt_table_values(canonical)
    table_differences = []
    for name, local_table in local["tables"].items():
        remote_table = canonical_tables.get(name)
        if remote_table is None:
            table_differences.append(f"{name}:MISSING_CANONICAL")
            continue
        for field in ("rows", "distinct_row_ids", "aggregate_source_hash"):
            if local_table[field] != remote_table[field]:
                table_differences.append(f"{name}:{field}")
    canonical_objects = canonical.get("objects", {})
    object_match = all(
        local["objects"].get(field) == canonical_objects.get(field)
        for field in ("count", "manifest_sha256", "compressed_bytes")
        if field in canonical_objects
    )
    synchronized = not table_differences and object_match
    return {
        "status": "SYNCHRONIZED" if synchronized else "LOCAL_CHANGES",
        "synchronized": synchronized,
        "local_fingerprint": manifest_fingerprint(local),
        "published_at": canonical_record.get("published_at"),
        "canonical_source": canonical_record.get("source", str(RUNTIME_SYNC_STATE)),
        "table_differences": table_differences,
        "object_manifest_matches": object_match,
        "local_manifest": local,
    }


def workspace_input_signature(root: Path) -> str:
    paths = [
        root / "data/judgment_room.sqlite3",
        root / "data/judgment_room.sqlite3-wal",
        root / "analysis/runs/case001_article_measurements_v1.jsonl",
        root / "calibration/case001_policy_decisions_v1.json",
        root / "calibration/case001_policy_application_dilemma_d3_v1.json",
        root / "anchors/case001_factual_anchors_v1.jsonl",
        root / "exports/case001_research_pilot_27.csv",
        root / RUNTIME_SYNC_STATE,
        root / CANONICAL_RECEIPT,
    ]
    values = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            values.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
        else:
            values.append((str(path.relative_to(root)), None, None))
    return _sha256(_canonical_json(values).encode("utf-8"))


def backup_judgment_store(database_path: Path, backup_path: Path) -> Path:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(database_path)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"Backup integrity check failed: {integrity}")
    finally:
        target.close()
        source.close()
    return backup_path


def current_peak_rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if platform.system() == "Darwin":
        return value / (1024 * 1024)
    return value / 1024
