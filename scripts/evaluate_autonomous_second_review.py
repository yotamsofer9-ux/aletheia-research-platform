#!/usr/bin/env python3
"""Compare shadow appellate candidates with held-out human judgments."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.autonomous_review import promotion_gate  # noqa: E402
from aletheia_lite.judgment_room import JudgmentStore, load_case_material  # noqa: E402


DEFAULT_INPUT = ROOT / "analysis/autonomous_review/case001_shadow_reconciled_v1.json"
DEFAULT_OUTPUT = ROOT / "analysis/autonomous_review/case001_shadow_validation_v1.json"
DATABASE = ROOT / "data/judgment_room.sqlite3"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    material = load_case_material(ROOT)
    dilemmas = {item["dilemma_id"]: item for item in material.dilemmas}
    decisions = JudgmentStore(args.database).current_decisions()
    rows = [
        row
        for document in payload.get("documents", [])
        for row in document.get("decisions", [])
    ]
    evaluated = []
    language_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"candidates": 0, "exact": 0}
    )
    exact = within_one = severe = numeric_na = 0
    candidates_with_human = 0
    for row in rows:
        if not row.get("shadow_auto_candidate"):
            continue
        human = decisions.get(row["dilemma_id"])
        if not human or human.get("outcome") == "DEFER":
            continue
        candidates_with_human += 1
        predicted = row.get("proposed_score")
        target = human.get("final_score")
        is_numeric_na = (predicted is None) != (target is None)
        delta = (
            abs(predicted - target)
            if isinstance(predicted, int) and isinstance(target, int)
            else None
        )
        is_exact = predicted == target and not is_numeric_na
        is_within_one = delta is not None and delta <= 1
        if is_exact:
            exact += 1
        if is_within_one:
            within_one += 1
        if delta is not None and delta >= 2:
            severe += 1
        if is_numeric_na:
            numeric_na += 1
        language = dilemmas[row["dilemma_id"]]["language"]
        language_stats[language]["candidates"] += 1
        language_stats[language]["exact"] += int(is_exact)
        evaluated.append({
            "dilemma_id": row["dilemma_id"],
            "language": language,
            "component": dilemmas[row["dilemma_id"]]["component"],
            "predicted_score": predicted,
            "human_score": target,
            "exact": is_exact,
            "delta": delta,
            "numeric_na_error": is_numeric_na,
        })
    language_rates = {
        language: _rate(values["exact"], values["candidates"])
        for language, values in language_stats.items()
    }
    metrics = {
        "candidate_count": candidates_with_human,
        "exact_agreement": _rate(exact, candidates_with_human),
        "within_one_agreement": _rate(within_one, candidates_with_human),
        "severe_errors": severe,
        "numeric_na_errors": numeric_na,
        "language_exact_agreement": language_rates,
        "minimum_language_exact_agreement": min(language_rates.values(), default=0.0),
    }
    all_shadow_candidates = sum(
        row.get("shadow_auto_candidate", False) for row in rows
    )
    validation_complete = all_shadow_candidates == candidates_with_human
    gate = promotion_gate(metrics) if validation_complete else {
        "status": "HUMAN_VALIDATION_INCOMPLETE",
        "passed": False,
    }
    report = {
        "schema_version": "1.0",
        "protocol_version": payload.get("protocol_version"),
        "status": gate["status"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_auto_candidates": all_shadow_candidates,
        "candidates_with_human_verdict": candidates_with_human,
        "metrics": metrics,
        "promotion_gate": gate,
        "evaluated_rows": evaluated,
        "writes_human_verdicts": False,
    }
    _write_json(args.output.resolve(), report)
    print(json.dumps({
        "status": report["status"],
        "shadow_auto_candidates": all_shadow_candidates,
        "candidates_with_human_verdict": candidates_with_human,
        "metrics": metrics,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
