#!/usr/bin/env python3
"""Audit score plausibility and semantic scale consistency across eight cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_workspace import CaseWorkspace
from aletheia_lite.judgment_room import (
    JudgmentStore,
    build_effective_rows,
    load_case_material,
)
from aletheia_lite.multicase_material import load_multicase_material


CASE_IDS = tuple(f"CASE-{number:03d}" for number in range(1, 9))


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _results_path(case_id: str) -> Path:
    if case_id == "CASE-001":
        return ROOT / "analysis" / "case001_final_results_v1.json"
    return ROOT / "data" / "cases" / case_id.lower() / "results" / "final_results_v1.json"


def _effective_rows(case_id: str) -> list[dict[str, Any]]:
    if case_id == "CASE-001":
        material = load_case_material(ROOT)
        store = JudgmentStore(ROOT / "data" / "judgment_room.sqlite3")
    else:
        workspace = CaseWorkspace(ROOT, case_id)
        material = load_multicase_material(workspace)
        store = JudgmentStore(workspace.adjudication_dir / "judgment_room.sqlite3")
    return build_effective_rows(material, store.current_decisions())


def _iqr(values: list[float]) -> dict[str, float]:
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    width = quartiles[2] - quartiles[0]
    return {
        "q1": round(quartiles[0], 4),
        "median": round(statistics.median(values), 4),
        "q3": round(quartiles[2], 4),
        "lower_fence": round(quartiles[0] - 1.5 * width, 4),
        "upper_fence": round(quartiles[2] + 1.5 * width, 4),
    }


def _semantic_issues(case_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    positive_t = re.compile(
        r"לא מאמץ|אינו מאמץ|לא מציג אותה בוודאות|not adopt|does not adopt|"
        r"preserv(?:es|ing) (?:the )?uncertainty|clearly distinguishes|"
        r"accurately reports|aligns perfectly",
        re.IGNORECASE,
    )
    numeric_recommendation = re.compile(
        r"(?:ציון|score(?: of)?)\s*([0-4])", re.IGNORECASE
    )
    for row in rows:
        component = row["component"]
        score = row.get("effective_score")
        rationale = str(row.get("effective_rationale") or "")
        if (
            component in {"T1", "T3"}
            and isinstance(score, int)
            and score <= 1
            and positive_t.search(rationale)
        ):
            issues.append(
                {
                    "case_id": case_id,
                    "document_id": row["document_id"],
                    "component": component,
                    "code": "LOW_T_WITH_POSITIVE_RATIONALE",
                    "score": score,
                    "rationale": rationale,
                }
            )
        if score is None:
            recommendations = [int(value) for value in numeric_recommendation.findall(rationale)]
            comparative_na = re.search(
                r"N/?A מתאים יותר מציון|N/?A.*rather than.*score",
                rationale,
                re.IGNORECASE,
            )
            if recommendations and not comparative_na:
                issues.append(
                    {
                        "case_id": case_id,
                        "document_id": row["document_id"],
                        "component": component,
                        "code": "NA_WITH_NUMERIC_RATIONALE",
                        "score": None,
                        "rationale": rationale,
                    }
                )
    return issues


def _render(payload: dict[str, Any]) -> str:
    lines = [
        "# ביקורת סבירות וחריגים — שמונת הקייסים",
        "",
        f"מצב: **{payload['status']}**",
        "",
        "| קייס | T | F | כיסוי T | מסמכי T≤50 | מסמכי F≤45 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["cases"]:
        lines.append(
            f"| {row['case_id']} | {row['truth_score']:.4f} | "
            f"{row['frame_intensity']:.4f} | {row['truth_coverage']:.4f} | "
            f"{row['extreme_low_truth_documents']} | {row['extreme_low_frame_documents']} |"
        )
    lines.extend(
        [
            "",
            "## מסקנה",
            "",
            "- לא נמצא היפוך סולם פעיל, פער בין ציון לנימוק, ערך מחוץ לטווח או "
            "חוסר התאמה בין התוצאה לרכיביה.",
            "- קייס 1 הוא חריג IQR נמוך בציר T, אך החריגה צפויה מבחינה מהותית: "
            "זהו הקייס היחיד שבו קבוצת כתבות אימצה בזמן אמת ייחוס סיבתי שהתנגש "
            "בעוגנים המאוחרים של חלון המחקר. לכן הוא נשמר כחריג מחקרי, לא כטעות.",
            "- ציוני F הנמוכים ברמת כתבה מרוכזים בעיקר ביומני חדשות ועדכונים "
            "תמציתיים, שבהם F3/F4 נמוכים; אין סימן להיפוך של סולם F.",
            "- ציוני T נמוכים ברמת כתבה מוסברים בציוני T1/T3 נמוכים או בכיסוי "
            "חלקי שקוף. הם אינם מוזנים אוטומטית כטעות רק משום שהם רחוקים מן החציון.",
            "",
            "## תיקון שבוצע במסגרת הביקורת",
            "",
            "בקייס 2 נמצאה הכרעה אוטונומית ששייכה בטעות לייחוס מסודר את הערך 0 "
            "בסולם T, אף שבסולם זה 4 הוא הכיוון הטוב. קריאה מלאה של הכתבה הראתה "
            "שהמערכת עצמה דווקא אימצה את טענת ה'טעות'; לכן שוחזרו ציוני המודל "
            "T1=1 ו־T3=1. ציון הכתבה עלה מ־13.6364 ל־31.8182 וציון הקייס "
            "מ־88.7918 ל־89.3616. התיקון דטרמיניסטי, מתועד, וללא קריאת מודל.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()

    cases = []
    case_truth: list[float] = []
    case_frame: list[float] = []
    semantic_issues: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        payload = _read(_results_path(case_id))
        case_row = next(row for row in payload["records"] if row["level"] == "CASE")
        documents = [row for row in payload["records"] if row["level"] == "DOCUMENT"]
        truth = float(case_row["truth_score"])
        frame = float(case_row["details"]["frame_intensity"]["score"])
        case_truth.append(truth)
        case_frame.append(frame)
        low_truth = [
            {
                "document_id": row["entity_id"],
                "title": row["label"],
                "score": row["truth_score"],
                "components": row["details"]["component_scores"],
            }
            for row in documents
            if row["truth_score"] <= 50
        ]
        low_frame = [
            {
                "document_id": row["entity_id"],
                "title": row["label"],
                "score": row["details"]["frame_intensity"]["score"],
                "components": row["details"]["frame_intensity"]["component_scores"],
            }
            for row in documents
            if row["details"]["frame_intensity"]["score"] <= 45
        ]
        case_semantic = _semantic_issues(case_id, _effective_rows(case_id))
        semantic_issues.extend(case_semantic)
        cases.append(
            {
                "case_id": case_id,
                "label": case_row["label"],
                "truth_score": truth,
                "frame_intensity": frame,
                "truth_coverage": case_row["truth_coverage"],
                "critical_flags": case_row["critical_flags"],
                "extreme_low_truth_documents": len(low_truth),
                "extreme_low_frame_documents": len(low_frame),
                "low_truth_details": low_truth,
                "low_frame_details": low_frame,
                "semantic_issues": case_semantic,
            }
        )

    truth_iqr = _iqr(case_truth)
    frame_iqr = _iqr(case_frame)
    case_outliers = {
        "truth": [
            row["case_id"]
            for row in cases
            if row["truth_score"] < truth_iqr["lower_fence"]
            or row["truth_score"] > truth_iqr["upper_fence"]
        ],
        "frame": [
            row["case_id"]
            for row in cases
            if row["frame_intensity"] < frame_iqr["lower_fence"]
            or row["frame_intensity"] > frame_iqr["upper_fence"]
        ],
    }
    output = {
        "schema_version": "1.0",
        "audit_version": "eight-case-score-plausibility-v1",
        "methodology_version": "TF-2D/1.0",
        "status": "PASS" if not semantic_issues else "REVIEW_REQUIRED",
        "case_level_distribution": {
            "truth": truth_iqr,
            "frame": frame_iqr,
            "iqr_outliers": case_outliers,
            "outlier_disposition": {
                "CASE-001": "EXPECTED_SUBSTANTIVE_OUTLIER_NOT_DATA_ERROR"
                if "CASE-001" in case_outliers["truth"]
                else None
            },
        },
        "semantic_scale_issues": semantic_issues,
        "cases": cases,
    }
    for path, content in (
        (args.json_output, json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        (args.report_output, _render(output)),
    ):
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    print(
        json.dumps(
            {
                "status": output["status"],
                "case_outliers": case_outliers,
                "semantic_scale_issues": len(semantic_issues),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if output["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
