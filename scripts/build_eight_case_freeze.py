#!/usr/bin/env python3
"""Create a read-only hash inventory before the eight-case audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = tuple(f"CASE-{number:03d}" for number in range(1, 9))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _case_files(case_id: str) -> list[Path]:
    if case_id == "CASE-001":
        patterns = (
            "analysis/case001_*",
            "analysis/runs/case001_*",
            "calibration/case001_*",
            "exports/case001_*",
            "data/*.sqlite3",
            "data/m4_*",
        )
        files = {
            path
            for pattern in patterns
            for path in ROOT.glob(pattern)
            if path.is_file()
        }
        return sorted(files)
    case_root = ROOT / "data" / "cases" / case_id.lower()
    return sorted(
        path
        for path in case_root.rglob("*")
        if path.is_file() and "cloud_stage" not in path.parts
    )


def _registry_states() -> dict[str, str]:
    database = ROOT / "data" / "case_registry.sqlite3"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT case_id, current_state FROM case_runs ORDER BY case_id"
        ).fetchall()
    states = {case_id: state for case_id, state in rows}
    states.setdefault("CASE-001", "LEGACY_CLOSED")
    return {case_id: states.get(case_id, "MISSING") for case_id in CASE_IDS}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    case_counts: dict[str, int] = {}
    for case_id in CASE_IDS:
        files = _case_files(case_id)
        case_counts[case_id] = len(files)
        for path in files:
            stat = path.stat()
            records.append(
                {
                    "case_id": case_id,
                    "path": str(path.relative_to(ROOT)),
                    "bytes": stat.st_size,
                    "sha256": _sha256(path),
                }
            )
    aggregate = hashlib.sha256(
        "".join(
            f"{item['case_id']}|{item['path']}|{item['sha256']}\n"
            for item in records
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": "1.0",
        "purpose": "PRE_AUDIT_IMMUTABLE_HASH_INVENTORY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology_version": "TF-2D/1.0",
        "case_ids": list(CASE_IDS),
        "registry_states": _registry_states(),
        "case_file_counts": case_counts,
        "file_count": len(records),
        "aggregate_sha256": aggregate,
        "files": records,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "file_count": len(records),
                "aggregate_sha256": aggregate,
                "registry_states": payload["registry_states"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
