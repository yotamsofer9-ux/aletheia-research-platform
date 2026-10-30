from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "bibliography" / "academic_articles.jsonl"
SCHEMA_PATH = ROOT / "bibliography" / "academic_articles_schema.json"
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_DATASET = os.getenv("BQ_DATASET_NORMALIZED")
DEFAULT_TABLE = "academic_articles"
DEFAULT_LOCATION = "us-central1"
STORAGE_PREFIX = (
    f"gs://{os.environ['GCS_BUCKET_RAW']}/academic_articles/"
    if os.getenv("GCS_BUCKET_RAW")
    else None
)
APPROVED_STATUS = "APPROVED_FOR_USE"
FORBIDDEN_ID_TOKENS = {"draft", "legacy", "old", "test", "tmp", "trial"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_FIELDS = {
    field["name"]
    for field in json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if field["mode"] == "REQUIRED"
}


def load_and_validate() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    article_ids: set[str] = set()
    dois: set[str] = set()
    storage_uris: set[str] = set()

    for line_number, line in enumerate(
        MANIFEST_PATH.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        missing = REQUIRED_FIELDS - record.keys()
        if missing:
            raise ValueError(f"line {line_number} missing fields: {sorted(missing)}")

        article_id = str(record["article_id"])
        article_id_tokens = set(article_id.lower().replace("_", "-").split("-"))
        if article_id_tokens & FORBIDDEN_ID_TOKENS:
            raise ValueError(f"non-canonical article_id rejected: {article_id}")
        if article_id in article_ids:
            raise ValueError(f"duplicate article_id: {article_id}")

        doi = str(record["doi"]).lower().removeprefix("https://doi.org/")
        if doi in dois:
            raise ValueError(f"duplicate DOI: {doi}")
        if str(record["canonical_url"]).lower() != f"https://doi.org/{doi}":
            raise ValueError(f"canonical DOI URL mismatch: {article_id}")

        if record["bibliography_status"] != APPROVED_STATUS:
            raise ValueError(
                f"only explicitly approved articles may be synchronized: {article_id}"
            )
        if record["is_current"] is not True:
            raise ValueError(f"non-current article rejected: {article_id}")

        storage_uri = str(record["storage_uri"])
        manifest_prefix = storage_uri.split("academic_articles/", 1)[0] + "academic_articles/"
        expected_prefix = f"{STORAGE_PREFIX or manifest_prefix}{article_id}/"
        if not storage_uri.startswith(expected_prefix) or not storage_uri.endswith(
            ".pdf"
        ):
            raise ValueError(f"storage URI is outside canonical location: {article_id}")
        if storage_uri in storage_uris:
            raise ValueError(f"duplicate storage URI: {storage_uri}")
        if not SHA256_PATTERN.fullmatch(str(record["storage_object_sha256"])):
            raise ValueError(f"invalid SHA-256: {article_id}")
        if record["storage_content_type"] != "application/pdf":
            raise ValueError(f"non-PDF academic article rejected: {article_id}")
        if int(record["storage_size_bytes"]) <= 0 or int(record["page_count"]) <= 0:
            raise ValueError(f"invalid PDF size or page count: {article_id}")
        if str(record["canonical_url"]) not in str(record["apa7_citation"]):
            raise ValueError(f"APA 7 citation omits canonical URL: {article_id}")

        article_ids.add(article_id)
        dois.add(doi)
        storage_uris.add(storage_uri)
        records.append(record)

    if not records:
        raise ValueError("academic article manifest must not be empty")
    return records


def list_storage_objects() -> set[str]:
    if STORAGE_PREFIX is None:
        raise ValueError("GCS_BUCKET_RAW is required for a live storage check")
    result = subprocess.run(
        ["gcloud", "storage", "ls", f"{STORAGE_PREFIX}**"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode and "matched no objects" not in result.stderr:
        raise RuntimeError(result.stderr.strip())
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("gs://") and not line.strip().endswith("/")
    }


def validate_storage_inventory(records: list[dict[str, object]]) -> None:
    expected = {str(record["storage_uri"]) for record in records}
    actual = list_storage_objects()
    missing = expected - actual
    unexpected = actual - expected
    if missing or unexpected:
        raise ValueError(
            "academic article storage is not canonical: "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replace the canonical BigQuery academic-article table from the "
            "validated manifest. The replacement prevents legacy rows from surviving."
        )
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--skip-storage-check", action="store_true")
    args = parser.parse_args()

    records = load_and_validate()
    if not args.skip_storage_check:
        validate_storage_inventory(records)

    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "articles": len(records),
                    "table": args.table,
                    "replacement_mode": True,
                }
            )
        )
        return 0

    destination = f"{args.project}:{args.dataset}.{args.table}"
    subprocess.run(
        [
            "bq",
            f"--project_id={args.project}",
            "load",
            "--replace",
            f"--location={args.location}",
            "--source_format=NEWLINE_DELIMITED_JSON",
            destination,
            str(MANIFEST_PATH),
            str(SCHEMA_PATH),
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "synced": len(records),
                "destination": destination,
                "mode": "replace_from_approved_manifest",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
