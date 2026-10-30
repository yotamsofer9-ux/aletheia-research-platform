#!/usr/bin/env python3
"""Run one approved case's article analysis in bounded sequential batches."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--inter-batch-delay-seconds", type=float, default=0)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 3:
        raise ValueError("batch-size must remain between 1 and 3")

    case_root = args.case_root
    analysis = case_root / "analysis"
    budget_path = analysis / "analysis_budget_v1.json"
    if not budget_path.exists():
        budget_path = ROOT / "analysis" / "budgets_v1.json"
    roster_path = analysis / "analysis_roster.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster_ids = list(roster["document_ids"])
    if len(roster_ids) != 27 or len(set(roster_ids)) != 27:
        raise ValueError("article runner requires an exact unique 27-document roster")
    document_ids = list(args.document_id) or roster_ids
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("requested document IDs must be unique")
    unknown = set(document_ids) - set(roster_ids)
    if unknown:
        raise ValueError(f"requested documents are outside the frozen roster: {sorted(unknown)}")
    if not 0 <= args.inter_batch_delay_seconds <= 30:
        raise ValueError("inter-batch delay must be between 0 and 30 seconds")

    base = [
        sys.executable,
        str(ROOT / "scripts" / "run_article_analysis.py"),
        "--case-id",
        args.case_id,
        "--case-manifest",
        str(case_root / "manifest.json"),
        "--corpus",
        str(analysis / "corpus.csv"),
        "--anchors",
        str(case_root / "anchors" / "factual_anchors_v1.jsonl"),
        "--manifest",
        str(roster_path),
        "--budget",
        str(budget_path),
        "--prompt-version",
        args.prompt_version,
        "--output",
        str(analysis / "article_measurements_v1.jsonl"),
        "--summary",
        str(analysis / "article_measurements_v1_summary.json"),
    ]
    if args.live:
        base.append("--live")

    for batch_number, offset in enumerate(
        range(0, len(document_ids), args.batch_size), start=1
    ):
        batch = document_ids[offset : offset + args.batch_size]
        command = list(base)
        for document_id in batch:
            command.extend(("--document-id", document_id))
        subprocess.run(command, cwd=ROOT, check=True)
        print(
            json.dumps(
                {
                    "batch": batch_number,
                    "documents": batch,
                    "completed_through": offset + len(batch),
                }
            ),
            flush=True,
        )
        if offset + len(batch) < len(document_ids) and args.inter_batch_delay_seconds:
            time.sleep(args.inter_batch_delay_seconds)

    # A dry run validates every bounded batch but intentionally does not create
    # measurements.  Building a review queue at this point would therefore
    # treat the entire frozen roster as missing.
    if not args.live:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "documents_validated": len(document_ids),
                    "review_queue_built": False,
                }
            ),
            flush=True,
        )
        return 0

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_article_review_queue.py"),
            "--input",
            str(analysis / "article_measurements_v1.jsonl"),
            "--output",
            str(analysis / "article_review_queue_v1.json"),
            "--prompt-version",
            args.prompt_version,
            "--roster",
            str(roster_path),
        ],
        cwd=ROOT,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
