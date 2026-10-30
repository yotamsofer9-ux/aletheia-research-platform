#!/usr/bin/env python3
"""Prepare and optionally apply the bounded CASE-001 M3 cloud snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.cloud_migration import prepare_m3_export


DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_DATASET = os.getenv("BQ_DATASET_NORMALIZED")
DEFAULT_BUCKET = os.getenv("GCS_BUCKET_RAW")
DEFAULT_LOCATION = "us-central1"
MAXIMUM_BYTES_BILLED = 1024 ** 3


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command, check=True, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _load_tables(
    output_dir: Path, project: str, dataset: str, location: str
) -> None:
    manifest = json.loads((output_dir / "m3_export_manifest.json").read_text())
    for table_name in sorted(manifest["tables"]):
        stage = f"_stage_case_001_{table_name}"
        stage_ref = f"{project}:{dataset}.{stage}"
        target_sql = f"`{project}.{dataset}.{table_name}`"
        stage_sql = f"`{project}.{dataset}.{stage}`"
        fields = manifest["tables"][table_name].get("fields")
        if fields is None:
            schema = json.loads(
                (output_dir / "schemas" / f"{table_name}.json").read_text()
            )
            fields = [item["name"] for item in schema]
        quoted_fields = ", ".join(f"`{field}`" for field in fields)
        try:
            _run([
                "bq", f"--project_id={project}", "load", "--replace",
                f"--location={location}",
                "--source_format=NEWLINE_DELIMITED_JSON",
                stage_ref,
                str(output_dir / "tables" / f"{table_name}.jsonl"),
                str(output_dir / "schemas" / f"{table_name}.json"),
            ])
            query = f"""
                BEGIN TRANSACTION;
                DELETE FROM {target_sql} WHERE case_id = @case_id;
                INSERT INTO {target_sql} ({quoted_fields})
                SELECT {quoted_fields} FROM {stage_sql};
                COMMIT TRANSACTION;
            """
            _run([
                "bq", f"--project_id={project}", "query",
                "--use_legacy_sql=false", f"--location={location}",
                f"--maximum_bytes_billed={MAXIMUM_BYTES_BILLED}",
                f"--parameter=case_id:STRING:CASE-001", query,
            ])
        finally:
            subprocess.run(
                ["bq", f"--project_id={project}", "rm", "-f", "-t", stage_ref],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )


def _upload_objects(output_dir: Path, bucket: str) -> None:
    _run([
        "gcloud", "storage", "rsync", "--recursive", "--checksums-only",
        str(output_dir / "objects"), f"gs://{bucket}",
    ])


def query_table_state(
    project: str, dataset: str, location: str, table_names: list[str]
) -> dict[str, Any]:
    selects = []
    for table_name in sorted(table_names):
        selects.append(
            "SELECT "
            f"'{table_name}' AS table_name, COUNT(*) AS row_count, "
            "COUNT(DISTINCT row_id) AS distinct_row_ids, "
            "TO_HEX(SHA256(STRING_AGG(source_row_hash, '' "
            "ORDER BY source_row_hash))) AS aggregate_source_hash "
            f"FROM `{project}.{dataset}.{table_name}` WHERE case_id = 'CASE-001'"
        )
    query = " UNION ALL ".join(selects)
    result = json.loads(_run([
        "bq", f"--project_id={project}", "query", "--format=json",
        "--use_legacy_sql=false", f"--location={location}",
        f"--maximum_bytes_billed={MAXIMUM_BYTES_BILLED}", query,
    ]))
    return {item["table_name"]: item for item in result}


def _verify_tables(
    output_dir: Path, project: str, dataset: str, location: str
) -> dict[str, Any]:
    manifest = json.loads((output_dir / "m3_export_manifest.json").read_text())
    by_table = query_table_state(
        project, dataset, location, list(manifest["tables"])
    )
    for table_name, expected in manifest["tables"].items():
        actual = by_table.get(table_name)
        if not actual:
            raise ValueError(f"Missing BigQuery verification row: {table_name}")
        if int(actual["row_count"]) != expected["rows"]:
            raise ValueError(f"BigQuery row count mismatch: {table_name}")
        if int(actual["distinct_row_ids"]) != expected["distinct_row_ids"]:
            raise ValueError(f"BigQuery key count mismatch: {table_name}")
        if actual["aggregate_source_hash"].lower() != expected["aggregate_source_hash"]:
            raise ValueError(f"BigQuery aggregate hash mismatch: {table_name}")
    return by_table


def _verify_objects(output_dir: Path, bucket: str) -> dict[str, Any]:
    records = [
        json.loads(line)
        for line in (output_dir / "m3_object_manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory() as temporary:
        readback = Path(temporary)
        for prefix in ("raw/CASE-001", "normalized/CASE-001", "case-artifacts/CASE-001/frame-v1"):
            destination = readback / prefix
            destination.mkdir(parents=True)
            _run([
                "gcloud", "storage", "rsync", "--recursive", "--checksums-only",
                f"gs://{bucket}/{prefix}", str(destination),
            ])

        verified = 0
        total_bytes = 0
        for item in records:
            downloaded = readback / item["object_key"]
            if not downloaded.is_file():
                raise ValueError(f"Missing GCS read-back object: {item['gcs_uri']}")
            compressed = downloaded.read_bytes()
            if len(compressed) != item["compressed_bytes"]:
                raise ValueError(f"GCS size mismatch: {item['gcs_uri']}")
            if hashlib.sha256(compressed).hexdigest() != item["compressed_sha256"]:
                raise ValueError(f"GCS compressed hash mismatch: {item['gcs_uri']}")
            payload = gzip.decompress(compressed)
            if item["object_type"] == "NORMALIZED_PAYLOAD":
                text = json.loads(payload)["text"].encode("utf-8")
                payload_hash = hashlib.sha256(text).hexdigest()
            else:
                payload_hash = hashlib.sha256(payload).hexdigest()
            if payload_hash != item["payload_sha256"]:
                raise ValueError(f"GCS payload hash mismatch: {item['gcs_uri']}")
            verified += 1
            total_bytes += len(compressed)
    expected = json.loads(
        (output_dir / "m3_export_manifest.json").read_text()
    )["objects"]["count"]
    if verified != expected:
        raise ValueError(f"Expected {expected} verified objects, found {verified}")
    return {
        "verified_objects": verified,
        "compressed_bytes": total_bytes,
        "readback_sha256_verified": True,
    }


def apply_migration(
    output_dir: Path, project: str, dataset: str, bucket: str, location: str
) -> dict[str, Any]:
    _upload_objects(output_dir, bucket)
    _load_tables(output_dir, project, dataset, location)
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
        "maximum_bytes_billed": MAXIMUM_BYTES_BILLED,
        "tables": tables,
        "objects": objects,
    }
    (output_dir / "m3_cloud_verification_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir or (
        ROOT / "exports" /
        f"case001_m3_migration_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    manifest = prepare_m3_export(ROOT, output_dir.resolve(), args.bucket)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    if args.apply:
        receipt = apply_migration(
            output_dir.resolve(), args.project, args.dataset, args.bucket, args.location
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("PREPARE_ONLY: no cloud data was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
