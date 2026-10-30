#!/usr/bin/env python3
"""Run the frozen frame-intensity instrument for any approved 27-item case."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _document_args(document_ids: list[str]) -> list[str]:
    result: list[str] = []
    for document_id in document_ids:
        result.extend(["--document-id", document_id])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("first", "second"))
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--first-pass", type=Path, required=True)
    parser.add_argument("--second-pass", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reuse-latest-candidate", action="store_true")
    parser.add_argument("--recover-saved-candidate", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    first = importlib.import_module("scripts.run_frame_intensity_analysis")
    first.CASE_ID = args.case_id
    first.CORPUS_PATH = args.corpus
    first.ROSTER_PATH = args.roster
    first.BUDGET_PATH = args.budget

    if args.stage == "first":
        first.DEFAULT_OUTPUT = args.first_pass
        forwarded = [
            "run_frame_intensity_analysis.py",
            "--corpus",
            str(args.corpus),
            "--roster",
            str(args.roster),
            "--output",
            str(args.first_pass),
            "--summary",
            str(args.summary),
        ]
        if args.live:
            forwarded.append("--live")
        if args.reuse_latest_candidate:
            forwarded.append("--reuse-latest-candidate")
        if args.recover_saved_candidate:
            forwarded.append("--recover-saved-candidate")
        forwarded.extend(_document_args(args.document_id))
        sys.argv = forwarded
        return first.main()

    second = importlib.import_module("scripts.run_frame_intensity_second_review")
    second.CASE_ID = args.case_id
    second.CORPUS_PATH = args.corpus
    second.ROSTER_PATH = args.roster
    second.BUDGET_PATH = args.budget
    second.FIRST_PASS_PATH = args.first_pass
    second.DEFAULT_OUTPUT = args.second_pass
    forwarded = [
        "run_frame_intensity_second_review.py",
        "--first-pass",
        str(args.first_pass),
        "--output",
        str(args.second_pass),
        "--summary",
        str(args.summary),
    ]
    if args.live:
        forwarded.append("--live")
    if args.force:
        forwarded.append("--force")
    if args.recover_saved_candidate:
        forwarded.append("--recover-saved-candidate")
    forwarded.extend(_document_args(args.document_id))
    sys.argv = forwarded
    return second.main()


if __name__ == "__main__":
    raise SystemExit(main())
