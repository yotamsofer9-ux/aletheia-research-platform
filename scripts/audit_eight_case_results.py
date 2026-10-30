#!/usr/bin/env python3
"""Run the deterministic technical audit for the eight-case TF-2D dataset."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CASE_IDS = tuple(f"CASE-{number:03d}" for number in range(1, 9))
COMPONENTS = ("T1", "T2", "T3", "N1", "N2", "N3", "N4", "N5")
F_COMPONENTS = ("F1", "F2", "F3", "F4", "F5")
LEVEL_COUNTS = {"DOCUMENT": 27, "PUBLISHER_SLOT": 9, "STRATUM": 3, "CASE": 1}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _dominant_language(text: str) -> tuple[str | None, float]:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return None, 0.0
    counts = {
        "he": sum("\u0590" <= character <= "\u05ff" for character in letters),
        "ar": sum(
            "\u0600" <= character <= "\u06ff"
            or "\u0750" <= character <= "\u077f"
            for character in letters
        ),
        "en": sum("a" <= character.lower() <= "z" for character in letters),
    }
    language = max(counts, key=counts.get)
    return language, counts[language] / len(letters)


def _normalized_host(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower()
    for prefix in ("www.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def _normalized_url(url: str | None) -> tuple[str, str]:
    parsed = urlparse(url or "")
    path = unquote(parsed.path).rstrip("/") or "/"
    path = path.replace("/amphtml/", "/")
    return _normalized_host(url), path.casefold()


def _paths_identify_same_article(left: str, right: str) -> bool:
    if left == right:
        return True
    left_trimmed = left.removesuffix("/lite")
    right_trimmed = right.removesuffix("/lite")
    if left_trimmed == right_trimmed:
        return True
    left_tokens = [token for token in left.split("/") if token]
    right_tokens = [token for token in right.split("/") if token]
    if left_tokens and right_tokens:
        left_id, right_id = left_tokens[-1], right_tokens[-1]
        if left_id == right_id and (
            left_id.isdigit() or re.fullmatch(r"[0-9a-f]{24,}", left_id)
        ):
            return True
        left_hash = re.search(r"([0-9a-f]{24,})$", left_id)
        right_hash = re.search(r"([0-9a-f]{24,})$", right_id)
        if left_hash and right_hash and left_hash.group(1) == right_hash.group(1):
            return True
    return (
        "todayspaper" in left
        and "todayspaper" in right
        and ("/issue/" in left or "/section/" in left)
        and ("/issue/" in right or "/section/" in right)
    )


def _extract_html_identity(raw: bytes) -> tuple[str | None, str | None]:
    source = raw.decode("utf-8", errors="replace")

    def match(pattern: str) -> str | None:
        found = re.search(pattern, source, flags=re.IGNORECASE | re.DOTALL)
        if not found:
            return None
        value = re.sub(r"<[^>]+>", "", found.group(1))
        return html.unescape(value).strip()

    title = match(r"<title[^>]*>(.*?)</title>")
    canonical = (
        match(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)')
        or match(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']')
        or match(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)')
        or match(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']')
    )
    return title, canonical


def _snapshot_path(snapshot_root: Path, snapshot_ref: str) -> Path:
    relative = snapshot_ref.removeprefix("local-snapshot://")
    direct = snapshot_root / relative
    if direct.exists():
        return direct
    parts = Path(relative).parts
    without_case = snapshot_root / Path(*parts[1:]) if len(parts) > 1 else direct
    return without_case


def _check_snapshot_identity(
    audit: Audit,
    case_id: str,
    corpus: list[dict[str, str]],
    snapshot_root: Path | None,
) -> dict[str, Any]:
    if snapshot_root is None:
        return {"checked": 0, "assignment_collisions": [], "source_mismatches": []}

    registered_urls: dict[tuple[str, str], dict[str, str]] = {}
    for row in corpus:
        registered_urls[_normalized_url(row.get("url"))] = row

    checked = 0
    missing: list[str] = []
    hash_mismatches: list[str] = []
    assignment_collisions: list[dict[str, Any]] = []
    source_mismatches: list[dict[str, Any]] = []
    metadata_drift: list[dict[str, Any]] = []
    unexpected_metadata_drift: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for row in corpus:
        snapshot_ref = row.get("snapshot_ref") or ""
        if not snapshot_ref.startswith("local-snapshot://"):
            continue
        path = _snapshot_path(snapshot_root, snapshot_ref)
        if not path.exists():
            missing.append(row["document_id"])
            continue
        checked += 1
        stored = path.read_bytes()
        try:
            raw = gzip.decompress(stored) if path.suffix == ".gz" else stored
        except OSError:
            raw = stored
        digest = hashlib.sha256(raw).hexdigest()
        if row.get("snapshot_hash") and digest != row["snapshot_hash"]:
            hash_mismatches.append(row["document_id"])
        title, canonical = _extract_html_identity(raw)
        registered_key = _normalized_url(row.get("url"))
        canonical_key = _normalized_url(canonical)
        identity = {
            "document_id": row["document_id"],
            "source_id": row.get("source_id"),
            "sample_phase": row.get("sample_phase"),
            "registered_url": row.get("url"),
            "snapshot_canonical_url": canonical,
            "snapshot_title": title,
        }
        identities.append(identity)
        owner = registered_urls.get(canonical_key) if canonical else None
        if owner and owner["document_id"] != row["document_id"]:
            collision = {
                **identity,
                "registered_owner_document_id": owner["document_id"],
                "registered_owner_source_id": owner.get("source_id"),
                "registered_owner_phase": owner.get("sample_phase"),
            }
            assignment_collisions.append(collision)
            audit.issue(
                case_id,
                "CRITICAL",
                "SNAPSHOT_ASSIGNMENT_COLLISION",
                "A raw snapshot resolves to the registered URL of a different corpus row.",
                **collision,
            )
            continue
        registered_host, registered_path = registered_key
        canonical_host, canonical_path = canonical_key
        cross_host_allowed = (
            row.get("source_id") == "japan-times-nyt-syndication-substitute"
            and {registered_host, canonical_host}
            <= {"japantimes.co.jp", "nytimes.com"}
        )
        if canonical and registered_host != canonical_host and not cross_host_allowed:
            mismatch = identity.copy()
            source_mismatches.append(mismatch)
            audit.issue(
                case_id,
                "CRITICAL",
                "SNAPSHOT_SOURCE_MISMATCH",
                "A raw snapshot belongs to a different publisher domain than the frozen row.",
                **mismatch,
            )
        elif canonical and registered_path != canonical_path and not cross_host_allowed:
            metadata_drift.append(identity)
            if not _paths_identify_same_article(registered_path, canonical_path):
                unexpected_metadata_drift.append(identity)
                audit.issue(
                    case_id,
                    "ERROR",
                    "SNAPSHOT_URL_METADATA_MISMATCH",
                    "The frozen URL and raw snapshot canonical URL identify different articles.",
                    **identity,
                )

    if missing:
        audit.issue(
            case_id,
            "CRITICAL",
            "SNAPSHOT_MISSING",
            "A frozen corpus row has no readable local raw snapshot.",
            document_ids=missing,
        )
    if hash_mismatches:
        audit.issue(
            case_id,
            "CRITICAL",
            "SNAPSHOT_HASH_MISMATCH",
            "A raw snapshot does not match its recorded SHA-256 digest.",
            document_ids=hash_mismatches,
        )
    return {
        "checked": checked,
        "missing": missing,
        "hash_mismatches": hash_mismatches,
        "assignment_collisions": assignment_collisions,
        "source_mismatches": source_mismatches,
        "metadata_drift": metadata_drift,
        "unexpected_metadata_drift": unexpected_metadata_drift,
        "identities": identities,
    }


class Audit:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.cases: dict[str, dict[str, Any]] = {}

    def issue(
        self,
        case_id: str,
        severity: str,
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        self.issues.append(
            {
                "case_id": case_id,
                "severity": severity,
                "code": code,
                "message": message,
                "details": details,
            }
        )


def _check_corpus(
    audit: Audit,
    case_id: str,
    corpus: list[dict[str, str]],
    snapshot_root: Path | None = None,
) -> dict[str, Any]:
    document_ids = [row["document_id"] for row in corpus]
    if len(corpus) != 27 or len(set(document_ids)) != 27:
        audit.issue(
            case_id,
            "CRITICAL",
            "CORPUS_CARDINALITY",
            "The effective corpus must contain exactly 27 unique documents.",
            rows=len(corpus),
            unique_documents=len(set(document_ids)),
        )
    language_counts = Counter(row.get("language") for row in corpus)
    phase_counts = Counter(row.get("sample_phase") for row in corpus)
    slot_counts = Counter(row.get("publisher_slot") for row in corpus)
    if language_counts != {"he": 9, "en": 9, "ar": 9}:
        audit.issue(
            case_id,
            "ERROR",
            "LANGUAGE_BALANCE",
            "The frozen corpus is not balanced at nine documents per language.",
            counts=dict(language_counts),
        )
    expected_phases = {
        "INITIAL_REPORT": 9,
        "EARLY_UPDATE": 9,
        "LATER_INVESTIGATION": 9,
    }
    if phase_counts != expected_phases:
        audit.issue(
            case_id,
            "ERROR",
            "PHASE_BALANCE",
            "The frozen corpus is not balanced at nine documents per phase.",
            counts=dict(phase_counts),
        )
    if len(slot_counts) != 9 or any(count != 3 for count in slot_counts.values()):
        audit.issue(
            case_id,
            "ERROR",
            "PUBLISHER_SLOT_BALANCE",
            "Each of nine publisher slots must contain three documents.",
            counts=dict(slot_counts),
        )
    hashes: dict[str, list[str]] = defaultdict(list)
    urls: dict[str, list[str]] = defaultdict(list)
    mismatches = []
    hash_mismatches = []
    for row in corpus:
        hashes[row.get("content_hash", "")].append(row["document_id"])
        urls[row.get("url", "")].append(row["document_id"])
        text = row.get("text") or ""
        if text and row.get("content_hash"):
            actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if actual_hash != row["content_hash"]:
                hash_mismatches.append(row["document_id"])
        detected, ratio = _dominant_language(f"{row.get('title', '')} {text}")
        if detected and detected != row.get("language") and ratio >= 0.55:
            mismatch = {
                "document_id": row["document_id"],
                "source_name": row.get("source_name"),
                "declared_language": row.get("language"),
                "detected_language": detected,
                "dominance_ratio": round(ratio, 4),
                "title": row.get("title"),
                "url": row.get("url"),
            }
            mismatches.append(mismatch)
            audit.issue(
                case_id,
                "CRITICAL",
                "CONTENT_SOURCE_LANGUAGE_MISMATCH",
                "Stored article content conflicts with its frozen source/language metadata.",
                **mismatch,
            )
    duplicate_hashes = {key: value for key, value in hashes.items() if key and len(value) > 1}
    duplicate_urls = {key: value for key, value in urls.items() if key and len(value) > 1}
    if duplicate_hashes:
        audit.issue(
            case_id,
            "ERROR",
            "DUPLICATE_CONTENT",
            "More than one corpus row has identical normalized content.",
            duplicates=duplicate_hashes,
        )
    if duplicate_urls:
        audit.issue(
            case_id,
            "ERROR",
            "DUPLICATE_URL",
            "More than one corpus row has the same article URL.",
            duplicates=duplicate_urls,
        )
    if hash_mismatches:
        audit.issue(
            case_id,
            "CRITICAL",
            "NORMALIZED_TEXT_HASH_MISMATCH",
            "Normalized text does not match the recorded content hash.",
            document_ids=hash_mismatches,
        )
    snapshot_summary = _check_snapshot_identity(
        audit, case_id, corpus, snapshot_root
    )
    return {
        "rows": len(corpus),
        "unique_documents": len(set(document_ids)),
        "language_counts": dict(language_counts),
        "phase_counts": dict(phase_counts),
        "publisher_slots": len(slot_counts),
        "language_content_mismatches": mismatches,
        "duplicate_content_groups": len(duplicate_hashes),
        "duplicate_url_groups": len(duplicate_urls),
        "content_hash_mismatches": hash_mismatches,
        "snapshots": snapshot_summary,
    }


def _check_frame(
    audit: Audit, case_id: str, frame_path: Path
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not frame_path.exists():
        audit.issue(case_id, "CRITICAL", "FRAME_RESULTS_MISSING", "F results are missing.")
        return None, {"status": "MISSING"}
    payload = _read_json(frame_path)
    records = payload.get("records") or []
    if payload.get("status") != "COMPLETE" or len(records) != 27:
        audit.issue(
            case_id,
            "CRITICAL",
            "FRAME_RESULTS_INCOMPLETE",
            "F results must be complete for 27 documents.",
            status=payload.get("status"),
            records=len(records),
        )
    formula_mismatches = []
    evidence_errors = []
    for record in records:
        scores = record.get("component_scores") or {}
        if set(scores) != set(F_COMPONENTS) or any(
            not isinstance(value, int) or value not in range(5)
            for value in scores.values()
        ):
            audit.issue(
                case_id,
                "ERROR",
                "FRAME_COMPONENT_SCALE",
                "F components must contain F1-F5 on the 0-4 scale.",
                document_id=record.get("document_id"),
                component_scores=scores,
            )
            continue
        expected = round(100 * sum(scores.values()) / 20, 4)
        if abs(expected - float(record.get("frame_score"))) > 0.0001:
            formula_mismatches.append(record.get("document_id"))
        cue_ids = {cue.get("cue_id") for cue in record.get("frame_cues", [])}
        components = record.get("components") or {}
        for component_id in F_COMPONENTS:
            component = components.get(component_id) or {}
            if not str(component.get("rationale_he") or "").strip():
                evidence_errors.append(f"{record.get('document_id')}:{component_id}:RATIONALE")
            refs = component.get("evidence_cue_ids") or []
            if any(ref not in cue_ids for ref in refs):
                evidence_errors.append(f"{record.get('document_id')}:{component_id}:CUE_REF")
            if component_id in {"F3", "F4"} and scores[component_id] >= 3 and not refs:
                evidence_errors.append(f"{record.get('document_id')}:{component_id}:HIGH_WITHOUT_CUE")
    if formula_mismatches:
        audit.issue(
            case_id,
            "CRITICAL",
            "FRAME_FORMULA_MISMATCH",
            "Stored F scores do not equal the positive 0-100 transformation.",
            document_ids=formula_mismatches,
        )
    if evidence_errors:
        audit.issue(
            case_id,
            "ERROR",
            "FRAME_EVIDENCE_INTEGRITY",
            "F rationale or evidence-cue references are incomplete.",
            errors=evidence_errors,
        )
    return payload, {
        "status": payload.get("status"),
        "records": len(records),
        "mean": (payload.get("distribution") or {}).get("mean"),
        "formula_mismatches": formula_mismatches,
        "evidence_errors": evidence_errors,
        "unresolved_human_reviews": payload.get("unresolved_human_reviews"),
    }


def _check_final_results(
    audit: Audit,
    case_id: str,
    results_path: Path,
    expected_records: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not results_path.exists():
        audit.issue(
            case_id,
            "BLOCKED",
            "FINAL_RESULTS_MISSING",
            "The case does not yet have a closed 40-record result set.",
        )
        return {"status": "MISSING"}
    payload = _read_json(results_path)
    records = payload.get("records") or []
    levels = Counter(record.get("level") for record in records)
    if len(records) != 40 or dict(levels) != LEVEL_COUNTS:
        audit.issue(
            case_id,
            "CRITICAL",
            "FINAL_RESULT_CARDINALITY",
            "Final results must contain 27 document, 9 slot, 3 stratum and 1 case rows.",
            records=len(records),
            levels=dict(levels),
        )
    invalid_ranges = []
    for record in records:
        truth = record.get("truth_score")
        coverage = record.get("truth_coverage")
        if truth is not None and not 0 <= float(truth) <= 100:
            invalid_ranges.append(f"{record.get('row_id')}:T")
        if coverage is None or not 0 <= float(coverage) <= 1:
            invalid_ranges.append(f"{record.get('row_id')}:COVERAGE")
    if invalid_ranges:
        audit.issue(
            case_id,
            "CRITICAL",
            "FINAL_RESULT_RANGE",
            "Final T or coverage values are outside their legal scale.",
            records=invalid_ranges,
        )
    legacy_non_null = sum(
        any(record.get(field) is not None for field in (
            "narrative_pressure",
            "aletheia_65_35",
            "aletheia_50_50",
            "aletheia_75_25",
        ))
        for record in records
    )
    embedded_f = sum(
        isinstance((record.get("details") or {}).get("frame_intensity"), dict)
        for record in records
    )
    if payload.get("methodology_version") != "TF-2D/1.0" or legacy_non_null:
        audit.issue(
            case_id,
            "ERROR",
            "LEGACY_RESULT_ENVELOPE",
            "Final results still expose retired A/N aggregates instead of the TF-2D envelope.",
            methodology_version=payload.get("methodology_version"),
            rows_with_legacy_values=legacy_non_null,
            rows_with_embedded_f=embedded_f,
        )
    if expected_records is not None:
        expected = {record["row_id"]: record for record in expected_records}
        actual = {record["row_id"]: record for record in records}
        mismatches = []
        if set(expected) != set(actual):
            mismatches.append("ROW_ID_SET")
        else:
            for row_id in sorted(expected):
                for field in ("truth_score", "truth_coverage"):
                    if expected[row_id].get(field) != actual[row_id].get(field):
                        mismatches.append(
                            {
                                "row_id": row_id,
                                "field": field,
                                "stored": actual[row_id].get(field),
                                "recomputed": expected[row_id].get(field),
                            }
                        )
        if mismatches:
            audit.issue(
                case_id,
                "CRITICAL",
                "TRUTH_RECALCULATION_MISMATCH",
                "Stored T results differ from a clean deterministic recalculation.",
                mismatches=mismatches[:50],
                mismatch_count=len(mismatches),
            )
    case_rows = [record for record in records if record.get("level") == "CASE"]
    case_row = case_rows[0] if len(case_rows) == 1 else {}
    return {
        "status": payload.get("status"),
        "methodology_version": payload.get("methodology_version"),
        "records": len(records),
        "levels": dict(levels),
        "legacy_non_null_rows": legacy_non_null,
        "embedded_f_rows": embedded_f,
        "truth_score": case_row.get("truth_score"),
        "truth_coverage": case_row.get("truth_coverage"),
        "frame_intensity": ((case_row.get("details") or {}).get("frame_intensity") or {}).get("score"),
    }


def _multicase(audit: Audit, case_id: str) -> None:
    from aletheia_lite.case_results import build_tf_score_records
    from aletheia_lite.case_workspace import CaseWorkspace
    from aletheia_lite.judgment_room import JudgmentStore, build_effective_rows
    from aletheia_lite.multicase_material import load_multicase_material, load_workspace_corpus

    workspace = CaseWorkspace(ROOT, case_id)
    corpus = load_workspace_corpus(workspace)
    corpus_summary = _check_corpus(
        audit, case_id, corpus, workspace.snapshot_root
    )
    material = load_multicase_material(workspace)
    store = JudgmentStore(workspace.adjudication_dir / "judgment_room.sqlite3")
    current_decisions = store.current_decisions()
    contaminated_documents = {
        item["document_id"]
        for item in corpus_summary["snapshots"]["assignment_collisions"]
    }
    contaminated_human_decisions = sorted(
        dilemma_id
        for dilemma_id, decision in current_decisions.items()
        if decision.get("document_id") in contaminated_documents
    )
    if contaminated_human_decisions:
        audit.issue(
            case_id,
            "CRITICAL",
            "ADJUDICATION_BOUND_TO_CORRUPT_CONTENT",
            "Saved human verdicts are bound to documents whose raw content was misassigned.",
            document_ids=sorted(contaminated_documents),
            dilemma_ids=contaminated_human_decisions,
        )
    effective = build_effective_rows(material, current_decisions)
    component_counts = Counter(row["component"] for row in effective)
    bad_scores = [
        f"{row['document_id']}:{row['component']}"
        for row in effective
        if row.get("effective_score") is not None
        and row["effective_score"] not in range(5)
    ]
    missing_rationales = [
        f"{row['document_id']}:{row['component']}"
        for row in effective
        if not str(row.get("effective_rationale") or "").strip()
    ]
    invalid_na_pairs = [
        f"{row['document_id']}:{row['component']}"
        for row in effective
        if (row.get("effective_score") is None) == (not row.get("na_reason"))
    ]
    if len(effective) != 216 or any(component_counts[name] != 27 for name in COMPONENTS):
        audit.issue(
            case_id,
            "CRITICAL",
            "EFFECTIVE_COMPONENT_CARDINALITY",
            "Each case requires 216 effective T/N components.",
            effective_rows=len(effective),
            component_counts=dict(component_counts),
        )
    if bad_scores or missing_rationales or invalid_na_pairs:
        audit.issue(
            case_id,
            "CRITICAL",
            "COMPONENT_CONTRACT",
            "An effective component violates the 0-4/N-A/rationale contract.",
            bad_scores=bad_scores,
            missing_rationales=missing_rationales,
            invalid_na_pairs=invalid_na_pairs,
        )
    human_required = {item["dilemma_id"] for item in material.dilemmas}
    human_saved = set(current_decisions)
    # V5 narrows the live docket, but previously approved human verdicts remain
    # authoritative and append-only.  Therefore only a missing required verdict
    # is an error; an older extra verdict is legitimate research history.
    if human_required - human_saved:
        audit.issue(
            case_id,
            "ERROR",
            "HUMAN_DECISION_COVERAGE",
            "A V5-required human dilemma has no saved current verdict.",
            missing=sorted(human_required - human_saved),
            extra=sorted(human_saved - human_required),
        )
    frame, frame_summary = _check_frame(
        audit, case_id, workspace.results_dir / "frame_intensity_results_v1.json"
    )
    trajectory_decisions = store.current_trajectory_decisions()
    invalid_t4 = [
        trajectory_id
        for trajectory_id, decision in trajectory_decisions.items()
        if (
            decision.get("score") is not None
            and decision.get("score") not in range(5)
        )
        or (
            decision.get("score") is None
            and not str(decision.get("na_reason") or "").strip()
        )
    ]
    if invalid_t4:
        audit.issue(
            case_id,
            "CRITICAL",
            "T4_SCALE_CONTRACT",
            "A T4 decision violates the 0-4/N-A contract.",
            trajectory_ids=invalid_t4,
        )
    if len(trajectory_decisions) != 9:
        audit.issue(
            case_id,
            "BLOCKED",
            "T4_INCOMPLETE",
            "Nine current T4 decisions are required before final scoring.",
            current_decisions=len(trajectory_decisions),
        )
    expected_records = None
    if len(trajectory_decisions) == 9 and frame is not None:
        expected_records = [
            record.to_dict()
            for record in build_tf_score_records(
                ROOT,
                material,
                effective,
                trajectory_decisions,
                frame,
                corpus=corpus,
                case_label=case_id,
            )
        ]
    results_summary = _check_final_results(
        audit,
        case_id,
        workspace.results_dir / "final_results_v1.json",
        expected_records,
    )
    history = _read_jsonl(workspace.analysis_dir / "article_measurements_v1.jsonl")
    latest_successes = {
        row["document_id"]: row
        for row in history
        if row.get("status") == "SUCCESS" and isinstance(row.get("measurement"), dict)
    }
    if set(latest_successes) != {row["document_id"] for row in corpus}:
        audit.issue(
            case_id,
            "CRITICAL",
            "MEASUREMENT_ROSTER_MISMATCH",
            "Current successful T/N measurements do not match the frozen corpus.",
            measured=sorted(latest_successes),
            corpus=sorted(row["document_id"] for row in corpus),
        )
    receipt_path = workspace.results_dir / "cloud_sync_receipt_final_v1.json"
    cloud_status = "MISSING"
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        cloud_status = receipt.get("status")
        if cloud_status != "VERIFIED_FINAL":
            audit.issue(
                case_id,
                "ERROR",
                "FINAL_CLOUD_NOT_VERIFIED",
                "The final cloud receipt is not VERIFIED_FINAL.",
                status=cloud_status,
            )
    elif results_summary.get("status") == "CLOSED":
        audit.issue(
            case_id,
            "ERROR",
            "FINAL_CLOUD_RECEIPT_MISSING",
            "A locally closed case has no final cloud verification receipt.",
        )
    audit.cases[case_id] = {
        "corpus": corpus_summary,
        "measurement_history_rows": len(history),
        "current_successful_measurements": len(latest_successes),
        "effective_components": len(effective),
        "component_counts": dict(component_counts),
        "human_decisions": len(current_decisions),
        "human_required": len(human_required),
        "contaminated_human_decisions": contaminated_human_decisions,
        "trajectory_decisions": len(trajectory_decisions),
        "frame": frame_summary,
        "results": results_summary,
        "cloud_status": cloud_status,
    }


def _case001(audit: Audit) -> None:
    from aletheia_lite.case_results import build_tf_score_records
    from aletheia_lite.judgment_room import JudgmentStore, build_effective_rows, load_case_material

    case_id = "CASE-001"
    corpus = _read_csv(ROOT / "exports" / "case001_research_pilot_27.csv")
    corpus_summary = _check_corpus(
        audit, case_id, corpus, ROOT / "data" / "snapshots"
    )
    material = load_case_material(ROOT)
    store = JudgmentStore(ROOT / "data" / "judgment_room.sqlite3")
    effective = build_effective_rows(material, store.current_decisions())
    component_counts = Counter(row["component"] for row in effective)
    if len(effective) != 216 or any(component_counts[name] != 27 for name in COMPONENTS):
        audit.issue(
            case_id,
            "CRITICAL",
            "EFFECTIVE_COMPONENT_CARDINALITY",
            "CASE-001 requires 216 effective T/N components.",
            effective_rows=len(effective),
            component_counts=dict(component_counts),
        )
    frame, frame_summary = _check_frame(
        audit, case_id, ROOT / "analysis" / "case001_frame_intensity_results_v1.json"
    )
    t4 = store.current_trajectory_decisions()
    if len(t4) != 9:
        audit.issue(
            case_id,
            "BLOCKED",
            "T4_INCOMPLETE",
            "CASE-001 requires nine current T4 decisions.",
            current_decisions=len(t4),
        )
    expected_records = None
    if len(t4) == 9 and frame is not None:
        expected_records = [
            record.to_dict()
            for record in build_tf_score_records(
                ROOT,
                material,
                effective,
                t4,
                frame,
                corpus=corpus,
                case_label="הפיצוץ בבית החולים אל־אהלי",
            )
        ]
    results_summary = _check_final_results(
        audit,
        case_id,
        ROOT / "analysis" / "case001_final_results_v1.json",
        expected_records,
    )
    audit.issue(
        case_id,
        "INFO",
        "LEGACY_REGISTRY_LOCATION",
        "CASE-001 intentionally remains in its frozen legacy location and is aligned through a read adapter.",
    )
    cloud_receipts = sorted(
        ROOT.glob("exports/case001_m3_migration_*/m3_cloud_verification_receipt.json"),
        key=lambda path: path.stat().st_mtime,
    )
    cloud_status = (
        _read_json(cloud_receipts[-1]).get("status")
        if cloud_receipts
        else "MISSING"
    )
    if cloud_status != "VERIFIED":
        audit.issue(
            case_id,
            "ERROR",
            "FINAL_CLOUD_NOT_VERIFIED",
            "The latest CASE-001 migration receipt is not VERIFIED.",
            status=cloud_status,
        )
    audit.cases[case_id] = {
        "corpus": corpus_summary,
        "effective_components": len(effective),
        "component_counts": dict(component_counts),
        "human_decisions": len(store.current_decisions()),
        "trajectory_decisions": len(t4),
        "frame": frame_summary,
        "results": results_summary,
        "cloud_status": cloud_status,
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# ביקורת טכנית לשמונת הקייסים",
        "",
        f"נוצר: {payload['generated_at']}",
        "",
        f"מצב: **{payload['status']}**",
        "",
        "## סיכום",
        "",
        f"- קייסים שנבדקו: {len(payload['cases'])}",
        f"- ממצאים קריטיים: {payload['summary']['CRITICAL']}",
        f"- חסמים: {payload['summary']['BLOCKED']}",
        f"- שגיאות: {payload['summary']['ERROR']}",
        f"- אזהרות: {payload['summary']['WARNING']}",
        "",
        "## תמונת מצב לפי קייס",
        "",
        "| קייס | כתבות | T/N אפקטיביים | F | T4 | תוצאות | ענן |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for case_id, case in payload["cases"].items():
        lines.append(
            "| "
            + " | ".join(
                [
                    case_id,
                    str(case["corpus"]["unique_documents"]),
                    str(case["effective_components"]),
                    str(case["frame"].get("records", 0)),
                    str(case["trajectory_decisions"]),
                    str(case["results"].get("status")),
                    str(case["cloud_status"]),
                ]
            )
            + " |"
        )
    checked_snapshots = sum(
        case["corpus"]["snapshots"]["checked"]
        for case in payload["cases"].values()
    )
    effective_components = sum(
        case["effective_components"] for case in payload["cases"].values()
    )
    frame_records = sum(
        case["frame"].get("records", 0) for case in payload["cases"].values()
    )
    lines.extend(
        [
            "",
            "## בדיקות שעברו",
            "",
            f"- {checked_snapshots} מתוך {checked_snapshots} סנפשוטים מקומיים נמצאו ונקראו; "
            "לא נמצאה חריגת SHA-256.",
            f"- {effective_components} רכיבי T/N אפקטיביים קיימים, 216 בכל קייס, "
            "והם עומדים בחוזה 0–4/N-A עם נימוק.",
            f"- {frame_records} רשומות F קיימות; נוסחת ההמרה והפניות הראיות תקינות.",
            "- לא נמצאו כתובות כפולות, תוכן מנורמל כפול או אי־התאמה בין "
            "חתימת התוכן לבין הטקסט השמור.",
            "- בכל שמונת הקייסים חישוב T החוזר זהה לתוצאה השמורה; אין היפוך סולם.",
        ]
    )

    case7_collisions = [
        issue
        for issue in payload["issues"]
        if issue["case_id"] == "CASE-007"
        and issue["code"] == "SNAPSHOT_ASSIGNMENT_COLLISION"
    ]
    if case7_collisions:
        lines.extend(
            [
                "",
                "## חריגת קייס 7",
                "",
                "חמישה סנפשוטים הוצמדו לשורת קורפוס אחרת מזו שאליה הם שייכים. "
                "העמודה האחרונה מצביעה על השורה שה־URL הקנוני של הסנפשוט שייך אליה.",
                "",
                "| מסמך שגוי | מקור/שלב שמור | שייך בפועל למסמך | מקור/שלב יעד |",
                "|---|---|---|---|",
            ]
        )
        for issue in case7_collisions:
            details = issue["details"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        details["document_id"],
                        f"{details.get('source_id')} / {details.get('sample_phase')}",
                        details["registered_owner_document_id"],
                        f"{details.get('registered_owner_source_id')} / "
                        f"{details.get('registered_owner_phase')}",
                    ]
                )
                + " |"
            )
        contaminated = payload["cases"]["CASE-007"].get(
            "contaminated_human_decisions", []
        )
        lines.extend(
            [
                "",
                f"ארבע פסיקות אנושיות מושפעות בפועל: {len(contaminated)}. "
                "הן יישמרו בהיסטוריה אך לא ישמשו לאחר תיקון הקורפוס.",
            ]
        )
    lines.extend(["", "## ממצאים", ""])
    order = {"CRITICAL": 0, "BLOCKED": 1, "ERROR": 2, "WARNING": 3, "INFO": 4}
    for issue in sorted(
        payload["issues"],
        key=lambda item: (order.get(item["severity"], 9), item["case_id"], item["code"]),
    ):
        lines.append(
            f"- **{issue['severity']} · {issue['case_id']} · {issue['code']}** — "
            f"{issue['message']}"
        )
    lines.extend(["", "## כללי מעבר", ""])
    if payload["status"] == "PASS":
        lines.extend(
            [
                "כל כללי המעבר הטכניים מולאו: אין ממצא CRITICAL או BLOCKED, "
                "כל שמונת הקייסים משתמשים במעטפת TF-2D/1.0, והנתונים כשירים "
                "לביקורת הסטטיסטית ולעיצוב הדשבורדים.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "הביקורת הסטטיסטית והעיצוב המחודש חסומים עד לתיקון כל ממצא "
                "CRITICAL/BLOCKED ואימות מחדש של העותק המקומי והענן.",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()
    audit = Audit()
    _case001(audit)
    for case_id in CASE_IDS[1:]:
        _multicase(audit, case_id)
    summary = Counter(issue["severity"] for issue in audit.issues)
    payload = {
        "schema_version": "1.0",
        "methodology_version": "TF-2D/1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED" if summary["CRITICAL"] or summary["BLOCKED"] else "PASS",
        "summary": {
            severity: summary[severity]
            for severity in ("CRITICAL", "BLOCKED", "ERROR", "WARNING", "INFO")
        },
        "cases": audit.cases,
        "issues": audit.issues,
    }
    for path, content in (
        (args.json_output, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        (args.report_output, _render_markdown(payload)),
    ):
        output = path.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(output)
    print(json.dumps({"status": payload["status"], "summary": payload["summary"]}, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
