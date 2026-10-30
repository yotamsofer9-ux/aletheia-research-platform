#!/usr/bin/env python3
"""Run both frame-intensity passes for one exact 27-document case."""

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
    parser.add_argument("--batch-size", type=int, default=9)
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--inter-batch-delay-seconds", type=float, default=0)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 9:
        raise ValueError("batch-size must remain between 1 and 9")

    analysis = args.case_root / "analysis"
    roster_path = analysis / "frame_full_roster_v1.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster_ids = list(roster["document_ids"])
    if len(roster_ids) != 27 or len(set(roster_ids)) != 27:
        raise ValueError("frame runner requires an exact unique 27-document roster")
    document_ids = list(args.document_id) or roster_ids
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("requested document IDs must be unique")
    unknown = set(document_ids) - set(roster_ids)
    if unknown:
        raise ValueError(f"requested documents are outside the frozen roster: {sorted(unknown)}")
    if not 0 <= args.inter_batch_delay_seconds <= 30:
        raise ValueError("inter-batch delay must be between 0 and 30 seconds")

    shared = [
        "--case-id",
        args.case_id,
        "--corpus",
        str(analysis / "corpus.csv"),
        "--roster",
        str(roster_path),
        "--budget",
        str(analysis / "frame_intensity_budget_v1.json"),
        "--first-pass",
        str(analysis / "frame_intensity_full_first_pass_v1.jsonl"),
        "--second-pass",
        str(analysis / "frame_intensity_full_second_pass_v1.jsonl"),
    ]
    script = str(ROOT / "scripts" / "run_case_frame_intensity.py")
    for stage in ("first", "second"):
        summary = analysis / f"frame_intensity_full_{stage}_pass_v1_summary.json"
        for batch_number, offset in enumerate(
            range(0, len(document_ids), args.batch_size), start=1
        ):
            batch = document_ids[offset : offset + args.batch_size]
            command = [sys.executable, script, stage, *shared, "--summary", str(summary)]
            if args.live:
                command.append("--live")
            for document_id in batch:
                command.extend(("--document-id", document_id))
            subprocess.run(command, cwd=ROOT, check=True)
            print(
                json.dumps(
                    {
                        "stage": stage,
                        "batch": batch_number,
                        "completed_through": offset + len(batch),
                    }
                ),
                flush=True,
            )
            if offset + len(batch) < len(document_ids) and args.inter_batch_delay_seconds:
                time.sleep(args.inter_batch_delay_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
