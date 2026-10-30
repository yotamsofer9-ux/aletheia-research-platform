from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "bibliography" / "bigquery_schema.json"
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_DATASET = os.getenv("BQ_DATASET_NORMALIZED")
DEFAULT_LOCATION = "us-central1"
REGISTRIES = {
    "research": {
        "path": ROOT / "bibliography" / "research_sources.jsonl",
        "table": "research_sources",
    },
    "journalistic": {
        "path": ROOT / "bibliography" / "journalistic_sources.jsonl",
        "table": "journalistic_sources",
    },
}

REQUIRED_FIELDS = {
    "source_id",
    "title",
    "authors_or_organization",
    "source_type",
    "quality_tier",
    "bibliography_status",
    "url",
    "used_for",
    "first_used_on",
    "accessed_on",
    "quality_notes",
    "citation_text",
}
ALLOWED_STATUSES = {"INCLUDE", "SUPPORTING_ONLY"}
JOURNALISTIC_SOURCE_TYPES = {
    "NEWS_REPORT",
    "JOURNALISTIC_INVESTIGATION",
    "JOURNALISTIC_VISUAL_INVESTIGATION",
    "OPEN_SOURCE_INVESTIGATION",
}


def load_and_validate(data_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(data_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        missing = REQUIRED_FIELDS - record.keys()
        if missing:
            raise ValueError(f"line {line_number} missing fields: {sorted(missing)}")
        source_id = str(record["source_id"])
        if source_id in seen:
            raise ValueError(f"duplicate source_id: {source_id}")
        if record["bibliography_status"] not in ALLOWED_STATUSES:
            raise ValueError(
                f"line {line_number} has invalid bibliography_status: "
                f"{record['bibliography_status']}"
            )
        citation_text = record["citation_text"]
        if not isinstance(citation_text, str) or not citation_text.strip():
            raise ValueError(
                f"line {line_number} has an empty APA citation_text value"
            )
        source_url = str(record["url"])
        if source_url not in citation_text:
            raise ValueError(
                f"line {line_number} APA citation_text does not contain its "
                f"canonical URL or DOI URL: {source_id}"
            )
        seen.add(source_id)
        records.append(record)
    if not records:
        raise ValueError(f"source registry is empty: {data_path}")
    json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return records


def validate_global_uniqueness() -> None:
    records_by_kind = {
        kind: load_and_validate(config["path"])
        for kind, config in REGISTRIES.items()
    }
    misplaced_research = {
        str(record["source_id"])
        for record in records_by_kind["research"]
        if record["source_type"] in JOURNALISTIC_SOURCE_TYPES
    }
    misplaced_journalism = {
        str(record["source_id"])
        for record in records_by_kind["journalistic"]
        if record["source_type"] not in JOURNALISTIC_SOURCE_TYPES
    }
    if misplaced_research or misplaced_journalism:
        raise ValueError(
            "source_type does not match registry: "
            f"research={sorted(misplaced_research)}, "
            f"journalistic={sorted(misplaced_journalism)}"
        )
    ids_by_kind = {
        kind: {record["source_id"] for record in records}
        for kind, records in records_by_kind.items()
    }
    overlap = ids_by_kind["research"] & ids_by_kind["journalistic"]
    if overlap:
        raise ValueError(
            "source_id values must be globally unique across registries; overlap: "
            f"{sorted(overlap)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and synchronize a source registry to BigQuery."
    )
    parser.add_argument("--kind", choices=sorted(REGISTRIES), default="research")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--table", default=None)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    registry = REGISTRIES[args.kind]
    data_path = registry["path"]
    table = args.table or registry["table"]
    validate_global_uniqueness()
    records = load_and_validate(data_path)
    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "kind": args.kind,
                    "sources": len(records),
                    "table": table,
                }
            )
        )
        return 0

    destination = f"{args.project}:{args.dataset}.{table}"
    command = [
        "bq",
        f"--project_id={args.project}",
        "load",
        "--replace",
        f"--location={args.location}",
        "--source_format=NEWLINE_DELIMITED_JSON",
        destination,
        str(data_path),
        str(SCHEMA_PATH),
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "BigQuery synchronization requires bigquery.jobs.create on project "
            f"{args.project}; grant roles/bigquery.jobUser to the active account "
            "and rerun this command. The validated local registry remains canonical."
        ) from exc
    print(
        json.dumps(
            {
                "synced": len(records),
                "kind": args.kind,
                "destination": destination,
                "mode": "replace_from_versioned_registry",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
