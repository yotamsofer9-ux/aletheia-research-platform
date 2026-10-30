from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
from typing import Any, Iterable

from .frame_intensity import COMPONENTS, METHODOLOGY_VERSION, frame_score


class FrameResultsError(ValueError):
    """Raised when the full-corpus frame result cannot be closed safely."""


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _latest_successes(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        latest[row["document_id"]] = row
    failures = [document_id for document_id, row in latest.items() if row.get("status") != "SUCCESS"]
    if failures:
        raise FrameResultsError(f"latest frame records are not successful: {failures}")
    return latest


def _group_stats(values: Iterable[float]) -> dict[str, float | int]:
    items = list(values)
    return {
        "count": len(items),
        "mean": round(statistics.fmean(items), 4),
        "median": round(statistics.median(items), 4),
        "minimum": round(min(items), 4),
        "maximum": round(max(items), 4),
    }


def build_frame_results(
    *,
    corpus_path: Path,
    roster_path: Path,
    first_pass_path: Path,
    second_pass_path: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    roster = _read_json(roster_path)
    case_id = roster["case_id"]
    roster_ids = roster["document_ids"]
    if len(roster_ids) != 27 or len(set(roster_ids)) != 27:
        raise FrameResultsError("full frame roster must contain 27 unique documents")

    with corpus_path.open(encoding="utf-8-sig") as handle:
        corpus = {row["document_id"]: row for row in csv.DictReader(handle)}
    if set(roster_ids) != set(corpus):
        raise FrameResultsError("full frame roster must exactly match the frozen corpus")

    first = _latest_successes(first_pass_path)
    second = _latest_successes(second_pass_path)
    if set(first) != set(roster_ids) or set(second) != set(roster_ids):
        raise FrameResultsError("both frame passes must cover the exact 27-document roster")

    records = []
    all_gaps: list[int] = []
    for document_id in roster_ids:
        document = corpus[document_id]
        first_row = first[document_id]
        second_row = second[document_id]
        comparison = second_row["comparison"]
        if comparison["requires_human_review"]:
            raise FrameResultsError(f"unresolved frame review remains: {document_id}")
        measurement = second_row["measurement"]
        component_scores = {
            code: measurement["components"][code]["score"] for code in COMPONENTS
        }
        gaps = {code: comparison["components"][code]["gap"] for code in COMPONENTS}
        all_gaps.extend(gaps.values())
        final_score = frame_score(measurement)
        if final_score != second_row["second_frame_score"]:
            raise FrameResultsError(f"second-pass frame score mismatch: {document_id}")
        records.append(
            {
                "case_id": case_id,
                "document_id": document_id,
                "source_id": document["source_id"],
                "source_name": document["source_name"],
                "publisher_slot": document["publisher_slot"],
                "language": document["language"],
                "sample_phase": document["sample_phase"],
                "published_at": document["published_at"],
                "title": document["title"],
                "frame_score": final_score,
                "component_scores": component_scores,
                "first_frame_score": first_row["frame_score"],
                "second_frame_score": second_row["second_frame_score"],
                "component_gaps": gaps,
                "authority": "INDEPENDENT_SECOND_REVIEW_ACCEPTED",
                "model": second_row["model"],
                "prompt_version": measurement["prompt_version"],
                "review_prompt_version": second_row["review_prompt_version"],
                "recorded_at": second_row["recorded_at"],
                "frame_statement_he": measurement["frame_statement_he"],
                "frame_families": measurement["frame_families"],
                "components": measurement["components"],
                "frame_cues": measurement["frame_cues"],
                "first_pass_input_hash": first_row["input_hash"],
                "second_pass_input_hash": second_row["input_hash"],
            }
        )

    frame_by_document = {row["document_id"]: row["frame_score"] for row in records}
    ordered_f = [frame_by_document[document_id] for document_id in roster_ids]

    grouped: dict[str, dict[str, list[float]]] = {
        "language": defaultdict(list),
        "sample_phase": defaultdict(list),
        "publisher_slot": defaultdict(list),
    }
    for row in records:
        for dimension in grouped:
            grouped[dimension][row[dimension]].append(row["frame_score"])

    return {
        "schema_version": "1.0",
        "status": "COMPLETE",
        "case_id": case_id,
        "methodology_version": METHODOLOGY_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "scoring_rule": "F = 100 * (F1 + F2 + F3 + F4 + F5) / 20",
        "outcome_architecture": "TF-2D/1.0",
        "quality_effect": "INDEPENDENT_CORE_AXIS_NO_COMPOSITE",
        "documents": 27,
        "component_rows": 135,
        "unresolved_human_reviews": 0,
        "agreement": {
            "component_comparisons": len(all_gaps),
            "exact_rate": round(sum(gap == 0 for gap in all_gaps) / len(all_gaps), 4),
            "within_one_rate": round(sum(gap <= 1 for gap in all_gaps) / len(all_gaps), 4),
            "mean_absolute_gap": round(statistics.fmean(all_gaps), 4),
            "documents_escalated": 0,
        },
        "distribution": _group_stats(ordered_f),
        "component_ceiling_counts": {
            code: sum(row["component_scores"][code] == 4 for row in records)
            for code in COMPONENTS
        },
        "breakdowns": {
            dimension: {
                key: _group_stats(values) for key, values in sorted(groups.items())
            }
            for dimension, groups in grouped.items()
        },
        "records": records,
    }


def render_frame_report(payload: dict[str, Any]) -> str:
    distribution = payload["distribution"]
    agreement = payload["agreement"]
    lines = [
        f"# דו״ח מלא — עוצמת מסגור F ב־{payload['case_id']}",
        "",
        f"סטטוס: הושלמו {payload['documents']} מתוך {payload['documents']} כתבות; אין הכרעות אנושיות פתוחות.",
        "",
        "## תוצאות מרכזיות",
        "",
        f"- ממוצע F: {distribution['mean']}; חציון: {distribution['median']}.",
        f"- טווח: {distribution['minimum']}–{distribution['maximum']}.",
        f"- הסכמה מדויקת בין הקריאות: {agreement['exact_rate'] * 100:.1f}%.",
        f"- הסכמה בטווח נקודה: {agreement['within_one_rate'] * 100:.1f}%.",
        f"- פער רכיב ממוצע: {agreement['mean_absolute_gap']}.",
        "",
        "F הוא ציר ליבה עצמאי לצד T. אין ציון משוקלל, ואין להסיק מעוצמת מסגור אמת, איכות, הטיה או כוונה.",
        "",
        "## ציוני 27 הכתבות",
        "",
        "| מקור | שפה | שלב | F1 | F2 | F3 | F4 | F5 | F |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["records"]:
        scores = row["component_scores"]
        lines.append(
            f"| {row['source_name']} | {row['language']} | {row['sample_phase']} | "
            f"{scores['F1']} | {scores['F2']} | {scores['F3']} | {scores['F4']} | "
            f"{scores['F5']} | {row['frame_score']:.0f} |"
        )
    lines.extend(
        [
            "",
            "## בקרת תקרה",
            "",
            "מספר הכתבות שקיבלו 4 בכל רכיב: "
            + ", ".join(
                f"{code}={count}"
                for code, count in payload["component_ceiling_counts"].items()
            )
            + ".",
            "",
        ]
    )
    return "\n".join(lines)
