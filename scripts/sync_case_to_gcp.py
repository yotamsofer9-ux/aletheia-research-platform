#!/usr/bin/env python3
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

from aletheia_lite.case_cloud_sync import (
    prepare_final_export,
    prepare_pre_adjudication_export,
)
from aletheia_lite.case_workspace import CaseWorkspace


DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_DATASET = os.getenv("BQ_DATASET_NORMALIZED")
DEFAULT_BUCKET = os.getenv("GCS_BUCKET_RAW")
DEFAULT_LOCATION = "us-central1"
MAXIMUM_BYTES_BILLED = 1024 ** 3
CLOUD_COMMAND_TIMEOUT_SECONDS = 180
EMPTY_AGGREGATE_SOURCE_HASH = hashlib.sha256(b"").hexdigest()


def _normalize_aggregate_source_hash(value: Any) -> str:
    """Match BigQuery's NULL STRING_AGG result to the local empty-set hash."""
    if value is None:
        return EMPTY_AGGREGATE_SOURCE_HASH
    return str(value).lower()


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=CLOUD_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


def _load_case_tables(
    output_dir: Path,
    *,
    case_id: str,
    project: str,
    dataset: str,
    location: str,
) -> None:
    manifest = json.loads((output_dir / "export_manifest.json").read_text())
    suffix = hashlib.sha256(
        (case_id + manifest["objects"]["manifest_sha256"]).encode("utf-8")
    ).hexdigest()[:12]
    for table_name, table in manifest["tables"].items():
        stage = f"_stage_{case_id.lower().replace('-', '_')}_{table_name}_{suffix}"
        stage_ref = f"{project}:{dataset}.{stage}"
        target_sql = f"`{project}.{dataset}.{table_name}`"
        stage_sql = f"`{project}.{dataset}.{stage}`"
        fields = table["fields"]
        quoted_fields = ", ".join(f"`{field}`" for field in fields)
        try:
            _run(
                [
                    "bq",
                    f"--project_id={project}",
                    "load",
                    "--replace",
                    f"--location={location}",
                    "--source_format=NEWLINE_DELIMITED_JSON",
                    stage_ref,
                    str(output_dir / "tables" / f"{table_name}.jsonl"),
                    str(output_dir / "schemas" / f"{table_name}.json"),
                ]
            )
            query = f"""
                BEGIN TRANSACTION;
                DELETE FROM {target_sql} WHERE case_id = @case_id;
                INSERT INTO {target_sql} ({quoted_fields})
                SELECT {quoted_fields} FROM {stage_sql};
                COMMIT TRANSACTION;
            """
            _run(
                [
                    "bq",
                    f"--project_id={project}",
                    "query",
                    "--use_legacy_sql=false",
                    f"--location={location}",
                    f"--maximum_bytes_billed={MAXIMUM_BYTES_BILLED}",
                    f"--parameter=case_id:STRING:{case_id}",
                    query,
                ]
            )
        finally:
            subprocess.run(
                ["bq", f"--project_id={project}", "rm", "-f", "-t", stage_ref],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )


def _verify_tables(
    output_dir: Path,
    *,
    case_id: str,
    project: str,
    dataset: str,
    location: str,
) -> dict[str, Any]:
    manifest = json.loads((output_dir / "export_manifest.json").read_text())
    selects = []
    for table_name in manifest["tables"]:
        selects.append(
            "SELECT "
            f"'{table_name}' AS table_name, COUNT(*) AS row_count, "
            "COUNT(DISTINCT row_id) AS distinct_row_ids, "
            "TO_HEX(SHA256(STRING_AGG(source_row_hash, '' ORDER BY source_row_hash))) "
            "AS aggregate_source_hash "
            f"FROM `{project}.{dataset}.{table_name}` WHERE case_id = @case_id"
        )
    output = _run(
        [
            "bq",
            f"--project_id={project}",
            "query",
            "--format=json",
            "--use_legacy_sql=false",
            f"--location={location}",
            f"--maximum_bytes_billed={MAXIMUM_BYTES_BILLED}",
            f"--parameter=case_id:STRING:{case_id}",
            " UNION ALL ".join(selects),
        ]
    )
    actual = {row["table_name"]: row for row in json.loads(output)}
    for table_name, expected in manifest["tables"].items():
        row = actual.get(table_name)
        if not row:
            raise ValueError(f"missing BigQuery verification row: {table_name}")
        if int(row["row_count"]) != expected["rows"]:
            raise ValueError(f"BigQuery row count mismatch: {table_name}")
        if int(row["distinct_row_ids"]) != expected["distinct_row_ids"]:
            raise ValueError(f"BigQuery distinct key mismatch: {table_name}")
        actual_hash = _normalize_aggregate_source_hash(
            row.get("aggregate_source_hash")
        )
        if actual_hash != expected["aggregate_source_hash"]:
            raise ValueError(f"BigQuery aggregate hash mismatch: {table_name}")
    return actual


def _upload_objects(output_dir: Path, bucket: str) -> None:
    _run(
        [
            "gcloud",
            "storage",
            "rsync",
            "--recursive",
            "--checksums-only",
            str(output_dir / "objects"),
            f"gs://{bucket}",
        ]
    )


def _verify_objects(output_dir: Path, bucket: str, case_id: str) -> dict[str, Any]:
    manifest = json.loads((output_dir / "export_manifest.json").read_text())
    artifact_version = (
        "final-v1" if manifest.get("phase") == "FINAL" else "pre-adjudication-v1"
    )
    records = [
        json.loads(line)
        for line in (output_dir / "object_manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory() as temporary:
        readback = Path(temporary)
        prefixes = (
            f"raw/{case_id}",
            f"normalized/{case_id}",
            f"case-artifacts/{case_id}/{artifact_version}",
        )
        for prefix in prefixes:
            destination = readback / prefix
            destination.mkdir(parents=True)
            _run(
                [
                    "gcloud",
                    "storage",
                    "rsync",
                    "--recursive",
                    "--checksums-only",
                    f"gs://{bucket}/{prefix}",
                    str(destination),
                ]
            )
        total_bytes = 0
        for item in records:
            downloaded = readback / item["object_key"]
            if not downloaded.is_file():
                raise ValueError(f"missing GCS read-back object: {item['gcs_uri']}")
            payload = downloaded.read_bytes()
            if len(payload) != item["compressed_bytes"]:
                raise ValueError(f"GCS size mismatch: {item['gcs_uri']}")
            if hashlib.sha256(payload).hexdigest() != item["compressed_sha256"]:
                raise ValueError(f"GCS object hash mismatch: {item['gcs_uri']}")
            if item["object_type"] == "RAW_SNAPSHOT":
                content_hash = hashlib.sha256(gzip.decompress(payload)).hexdigest()
            elif item["object_type"] == "NORMALIZED_PAYLOAD":
                text = json.loads(gzip.decompress(payload))["text"].encode("utf-8")
                content_hash = hashlib.sha256(text).hexdigest()
            else:
                content_hash = hashlib.sha256(payload).hexdigest()
            if content_hash != item["payload_sha256"]:
                raise ValueError(f"GCS payload hash mismatch: {item['gcs_uri']}")
            total_bytes += len(payload)
    return {
        "verified_objects": len(records),
        "bytes": total_bytes,
        "readback_sha256_verified": True,
    }


def apply_sync(
    output_dir: Path,
    *,
    case_id: str,
    project: str,
    dataset: str,
    bucket: str,
    location: str,
) -> dict[str, Any]:
    manifest = json.loads((output_dir / "export_manifest.json").read_text())
    is_final = manifest.get("phase") == "FINAL"
    artifact_version = "final-v1" if is_final else "pre-adjudication-v1"
    _upload_objects(output_dir, bucket)
    _load_case_tables(
        output_dir,
        case_id=case_id,
        project=project,
        dataset=dataset,
        location=location,
    )
    tables = _verify_tables(
        output_dir,
        case_id=case_id,
        project=project,
        dataset=dataset,
        location=location,
    )
    objects = _verify_objects(output_dir, bucket, case_id)
    receipt = {
        "schema_version": "1.0",
        "status": "VERIFIED_FINAL" if is_final else "VERIFIED_PRE_ADJUDICATION",
        "case_id": case_id,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "dataset": dataset,
        "bucket": bucket,
        "location": location,
        "maximum_bytes_billed": MAXIMUM_BYTES_BILLED,
        "tables": tables,
        "objects": objects,
    }
    receipt_path = output_dir / "cloud_verification_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_name = (
        "cloud_sync_receipt_final_v1.json"
        if is_final
        else "cloud_sync_receipt_pre_adjudication_v1.json"
    )
    workspace_receipt = ROOT / "data" / "cases" / case_id.lower() / "results" / receipt_name
    workspace_receipt.parent.mkdir(parents=True, exist_ok=True)
    workspace_receipt.write_bytes(receipt_path.read_bytes())
    receipt_key = f"case-artifacts/{case_id}/{artifact_version}/cloud_verification_receipt.json"
    _run(["gcloud", "storage", "cp", str(receipt_path), f"gs://{bucket}/{receipt_key}"])
    readback = _run(["gcloud", "storage", "cat", f"gs://{bucket}/{receipt_key}"])
    if hashlib.sha256(readback.encode("utf-8")).hexdigest() != hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest():
        raise ValueError("cloud verification receipt read-back hash mismatch")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and sync one case without replacing other cases.")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument(
        "--phase",
        choices=("pre-adjudication", "final"),
        default="final",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    workspace = CaseWorkspace(ROOT, args.case_id)
    output_dir = args.output_dir or (
        workspace.cloud_stage_dir
        / f"{args.phase.replace('-', '_')}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    prepare = (
        prepare_final_export
        if args.phase == "final"
        else prepare_pre_adjudication_export
    )
    manifest = prepare(ROOT, workspace, output_dir.resolve(), args.bucket)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    if args.apply:
        receipt = apply_sync(
            output_dir.resolve(),
            case_id=args.case_id,
            project=args.project,
            dataset=args.dataset,
            bucket=args.bucket,
            location=args.location,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("PREPARE_ONLY: no cloud data was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
