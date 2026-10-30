#!/usr/bin/env python3
"""Explicit local-first refresh and publish operations for the M4 workspace."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.cloud_migration import prepare_m3_export
from aletheia_lite.research_workspace import (
    backup_judgment_store,
    load_canonical_sync_record,
    workspace_sync_status,
    write_runtime_sync_record,
)
from scripts.migrate_case001_to_gcp import (
    DEFAULT_BUCKET,
    DEFAULT_DATASET,
    DEFAULT_LOCATION,
    DEFAULT_PROJECT,
    _verify_objects,
    _verify_tables,
    apply_migration,
    query_table_state,
)


def _normalized_table_state(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables = payload.get("tables", payload)
    return {
        name: {
            "rows": int(row.get("rows", row.get("row_count", 0))),
            "distinct_row_ids": int(row["distinct_row_ids"]),
            "aggregate_source_hash": row["aggregate_source_hash"].lower(),
        }
        for name, row in tables.items()
    }


def _assert_remote_base_is_canonical(
    root: Path, project: str, dataset: str, location: str
) -> None:
    canonical = load_canonical_sync_record(root)["manifest"]
    expected = _normalized_table_state(canonical)
    remote = _normalized_table_state(query_table_state(
        project, dataset, location, list(expected)
    ))
    differences = []
    for table_name, expected_row in expected.items():
        if remote.get(table_name) != expected_row:
            differences.append(table_name)
    if differences:
        raise RuntimeError(
            "REMOTE_BASE_CONFLICT: cloud tables changed since the local base: "
            + ", ".join(sorted(differences))
        )


def _output_dir(prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "exports" / f"{prefix}_{stamp}"


def verify_cloud(
    *, project: str, dataset: str, bucket: str, location: str
) -> dict[str, Any]:
    output_dir = _output_dir("case001_m4_refresh")
    manifest = prepare_m3_export(ROOT, output_dir, bucket)
    tables = _verify_tables(output_dir, project, dataset, location)
    objects = _verify_objects(output_dir, bucket)
    receipt = {
        "schema_version": "1.0",
        "status": "VERIFIED",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "dataset": dataset,
        "bucket": bucket,
        "location": location,
        "tables": tables,
        "objects": objects,
    }
    write_runtime_sync_record(
        ROOT, manifest, receipt, operation="REFRESH_VERIFY_ONLY"
    )
    return receipt


def publish_cloud(
    *, project: str, dataset: str, bucket: str, location: str
) -> dict[str, Any]:
    _assert_remote_base_is_canonical(ROOT, project, dataset, location)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = ROOT / "data/backups" / f"judgment_room_before_publish_{stamp}.sqlite3"
    backup_judgment_store(ROOT / "data/judgment_room.sqlite3", backup_path)
    output_dir = ROOT / "exports" / f"case001_m4_publish_{stamp}"
    manifest = prepare_m3_export(ROOT, output_dir, bucket)
    receipt = apply_migration(output_dir, project, dataset, bucket, location)
    write_runtime_sync_record(
        ROOT, manifest, receipt, operation="PUBLISH", backup_path=backup_path
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--verify-cloud", action="store_true")
    action.add_argument("--publish", action="store_true")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    args = parser.parse_args()
    if args.status:
        result = workspace_sync_status(ROOT)
    elif args.verify_cloud:
        result = verify_cloud(
            project=args.project, dataset=args.dataset,
            bucket=args.bucket, location=args.location,
        )
    else:
        result = publish_cloud(
            project=args.project, dataset=args.dataset,
            bucket=args.bucket, location=args.location,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
