#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_pipeline import CasePipelineRunner
from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_run_receipt import existing_run_handlers, validate_existing_run
from aletheia_lite.case_workspace import CaseWorkspace


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate already-produced case artifacts and register real pipeline checkpoints."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / "data/case_registry.sqlite3")
    parser.add_argument("--memory-limit-mb", type=float, default=300)
    args = parser.parse_args()

    workspace = CaseWorkspace(ROOT, args.case_id)
    validation = validate_existing_run(workspace)
    result = CasePipelineRunner(
        registry=CaseRegistry(args.registry),
        workspace=workspace,
        handlers=existing_run_handlers(validation),
        memory_limit_mb=args.memory_limit_mb,
        # This command registers the four completed research stages only.
        # Final TF-2D result materialization, cloud verification and dashboard
        # publication are separate, auditable closeout operations.
        stop_after_running_stages=True,
    ).run()
    print(
        json.dumps(
            {"pipeline": result.__dict__, "receipt": validation.receipt},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
