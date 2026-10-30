#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.frame_results import build_frame_results, render_frame_report


CASE_ROOT = ROOT / "data" / "cases" / "case-002"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=CASE_ROOT / "analysis" / "corpus.csv",
    )
    parser.add_argument(
        "--roster",
        type=Path,
        default=CASE_ROOT / "analysis" / "frame_full_roster_v1.json",
    )
    parser.add_argument(
        "--first-pass",
        type=Path,
        default=CASE_ROOT / "analysis" / "frame_intensity_full_first_pass_v1.jsonl",
    )
    parser.add_argument(
        "--second-pass",
        type=Path,
        default=CASE_ROOT / "analysis" / "frame_intensity_full_second_pass_v1.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CASE_ROOT / "results" / "frame_intensity_results_v1.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "analysis" / "FRAME_INTENSITY_CASE002_FULL_REPORT.md",
    )
    args = parser.parse_args()

    payload = build_frame_results(
        corpus_path=args.corpus,
        roster_path=args.roster,
        first_pass_path=args.first_pass,
        second_pass_path=args.second_pass,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = args.report.with_suffix(args.report.suffix + ".tmp")
    temporary_report.write_text(render_frame_report(payload), encoding="utf-8")
    temporary_report.replace(args.report)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "documents": payload["documents"],
                "component_rows": payload["component_rows"],
                "unresolved_human_reviews": payload["unresolved_human_reviews"],
                "mean_F": payload["distribution"]["mean"],
                "output": str(args.output),
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
