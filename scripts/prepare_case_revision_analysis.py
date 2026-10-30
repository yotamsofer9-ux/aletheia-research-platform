#!/usr/bin/env python3
"""Retain valid cached work after an approved basket revision."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_workspace import CaseWorkspace


JSONL_FILES = (
    "analysis/article_measurements_v1.jsonl",
    "analysis/frame_intensity_full_first_pass_v1.jsonl",
    "analysis/frame_intensity_full_second_pass_v1.jsonl",
    "adjudication/second_review_ballots_v1.jsonl",
)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare(case_id: str, previous_hash: str) -> dict[str, object]:
    workspace = CaseWorkspace(ROOT, case_id)
    roster = json.loads(
        (workspace.analysis_dir / "analysis_roster.json").read_text(encoding="utf-8")
    )
    active = set(roster["document_ids"])
    if len(active) != 27:
        raise ValueError("revised case roster must contain exactly 27 documents")
    archive = workspace.revisions_dir / previous_hash[:16]
    if not (archive / "approved_basket.json").exists():
        raise ValueError("previous basket revision archive is missing")

    filtered: dict[str, dict[str, int]] = {}
    for relative in JSONL_FILES:
        source = workspace.case_root / relative
        if not source.exists():
            continue
        destination = archive / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        kept = [row for row in rows if row.get("document_id") in active]
        _write_text_atomic(
            source,
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in kept
            ),
        )
        filtered[relative] = {"before": len(rows), "after": len(kept)}

    old_frame_roster = workspace.analysis_dir / "frame_full_roster_v1.json"
    frame = (
        json.loads(old_frame_roster.read_text(encoding="utf-8"))
        if old_frame_roster.exists()
        else {}
    )
    if old_frame_roster.exists() and not (archive / "analysis/frame_full_roster_v1.json").exists():
        (archive / "analysis").mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_frame_roster, archive / "analysis/frame_full_roster_v1.json")
    frame.update(
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "methodology_version": "frame-intensity-v0.1.0",
            "prompt_version": frame.get(
                "prompt_version", "case002-frame-intensity-v0.1.0"
            ),
            "selection_rule": (
                f"Exact Gate-2-approved {case_id} revised corpus; retained results "
                "are reused only for unchanged document IDs."
            ),
            "basket_hash": roster["basket_hash"],
            "document_ids": roster["document_ids"],
        }
    )
    _write_text_atomic(
        old_frame_roster,
        json.dumps(frame, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "case_id": case_id,
        "basket_hash": roster["basket_hash"],
        "active_documents": len(active),
        "filtered": filtered,
        "archive": str(archive.relative_to(ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--previous-basket-hash", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.case_id, args.previous_basket_hash),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
