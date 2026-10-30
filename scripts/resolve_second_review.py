#!/usr/bin/env python3
"""Apply the principal-researcher threshold to autonomous second review."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.second_review_resolution import resolve_second_review  # noqa: E402


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dilemmas", type=Path, required=True)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--reconciled", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    latest = {}
    for line in args.measurements.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("status") == "SUCCESS":
                latest[row["document_id"]] = row
    packet = _read(args.dilemmas)
    normalized = {"case_id": packet["case_id"], "dilemmas": []}
    for dilemma in packet["dilemmas"]:
        component = latest[dilemma["document_id"]]["measurement"]["components"][
            dilemma["component"]
        ]
        normalized["dilemmas"].append({
            "dilemma_id": dilemma["dilemma_id"],
            "document_id": dilemma["document_id"],
            "component": dilemma["component"],
            "model_score": component.get("score"),
            "model_na_reason": component.get("na_reason"),
            "full_dilemma": dilemma,
        })
    result = resolve_second_review(normalized, _read(args.reconciled))
    with args.corpus.open(encoding="utf-8-sig", newline="") as handle:
        corpus = {row["document_id"]: row for row in csv.DictReader(handle)}
    anchors = {
        row["anchor_id"]: row
        for row in (
            json.loads(line)
            for line in args.anchors.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    translations = {
        (row["document_id"], row["source_text"]): row
        for row in _read(args.translations)["records"]
    }
    for dilemma in result["human_dilemmas"]:
        record = latest[dilemma["document_id"]]
        component = record["measurement"]["components"][dilemma["component"]]
        spans = component.get("evidence_spans") or []
        evidence = dilemma.get("curated_exhibit") or (spans[0] if spans else None)
        refs = list(dict.fromkeys(
            list(component.get("anchor_refs") or [])
            + list(dilemma["challenge"].get("anchor_refs") or [])
        ))
        article = corpus[dilemma["document_id"]]
        dilemma.update({
            "case_id": result["case_id"],
            "source_id": article["source_id"],
            "language": article["language"],
            "sample_phase": article["sample_phase"],
            "prompt_version": record["prompt_version"],
            "article": article,
            "model": {
                "score": component.get("score"),
                "na_reason": component.get("na_reason"),
                "confidence": component.get("confidence"),
                "rationale": component.get("rationale"),
                "evidence_span": evidence,
                "anchor_refs": component.get("anchor_refs", []),
                "reasons": dilemma.get("issue_tags", []),
            },
            "decisive_exhibit": evidence,
            "evidence_translation": (
                translations.get((dilemma["document_id"], evidence["quote_original"]))
                if evidence else None
            ),
            "visual_exhibits": [],
            "anchor_review": [],
            "anchors": [anchors[ref] for ref in refs],
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
