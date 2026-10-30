#!/usr/bin/env python3
"""Materialize model-reviewed factual anchors without substantive auto-edits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.anchor_review_materializer import (
    materialize_model_reviewed_anchors,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--basis-overrides", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = materialize_model_reviewed_anchors(
        _read_jsonl(args.anchors),
        _read_jsonl(args.reviews),
        revised_basis_summaries=json.loads(
            args.basis_overrides.read_text(encoding="utf-8")
        ),
    )
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        f"{row['anchor_id']}: {error.message}"
        for row in rows
        for error in validator.iter_errors(row)
    ]
    if errors:
        raise ValueError(f"materialized anchors violate schema: {errors}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    temporary.replace(args.output)
    print(json.dumps({
        "valid": True,
        "anchors": len(rows),
        "assessments": sum(len(row["assessments"]) for row in rows),
        "output": str(args.output),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
