#!/usr/bin/env python3
"""Run the canonicality gate over a reviewed public-source pack and register candidates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_candidate_annotation import VertexClusterAnnotator
from aletheia_lite.case_discovery import EventCluster, EventLead, build_case_candidate
from aletheia_lite.case_registry import CaseRegistry
from scripts.run_case_discovery import (
    candidate_version,
    same_candidate_content,
    sample_composition,
    write_json,
)


DEFAULT_PACK = (
    ROOT / "case_generator" / "reviewed_leads" / "canonical-expansion-v1.json"
)
DEFAULT_REGISTRY = ROOT / "data" / "case_registry.sqlite3"
DEFAULT_REPORT_DIR = ROOT / "analysis" / "case_generator" / "reviewed_runs"
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL = "gemini-2.5-flash"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024 * 1024) if sys.platform == "darwin" else float(value) / 1024


def _lead(event: dict[str, Any], raw: dict[str, Any]) -> EventLead:
    url = str(raw["url"])
    provider = str(raw["provider_id"])
    lead_id = hashlib.sha256(f"{provider}|{url}".encode("utf-8")).hexdigest()
    return EventLead(
        lead_id=lead_id,
        provider_id=provider,
        provider_record_id=url,
        source_name=str(raw["source_name"]),
        source_domain=urlparse(url).hostname or str(raw["source_name"]),
        url=url,
        canonical_url=url,
        title=str(raw["title"]),
        summary=str(raw["summary"]),
        published_at=str(raw["published_at"]),
        language=str(raw["language"]),
        topic_tag=str(event["topic_tag"]),
    )


def _cluster(event: dict[str, Any], leads: list[EventLead]) -> EventCluster:
    return EventCluster(
        cluster_id=str(event["cluster_id"]),
        topic_tag=str(event["topic_tag"]),
        lead_ids=tuple(sorted(lead.lead_id for lead in leads)),
        provider_ids=tuple(sorted({lead.provider_id for lead in leads})),
        source_domains=tuple(sorted({lead.source_domain for lead in leads})),
        languages=tuple(sorted({lead.language for lead in leads})),
        window_start=str(event["window_start"]),
        window_end=str(event["window_end"]),
    )


def register_pack(
    *,
    pack_path: Path,
    registry_path: Path,
    report_dir: Path,
    project: str,
    location: str,
    model: str,
    apply: bool,
    minimum: int,
    cluster_ids: set[str] | None = None,
) -> dict[str, Any]:
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    events = list(pack.get("events") or [])
    if cluster_ids:
        events = [event for event in events if event["cluster_id"] in cluster_ids]
        missing = cluster_ids - {event["cluster_id"] for event in events}
        if missing:
            raise ValueError(f"unknown cluster ids: {sorted(missing)}")
    if len(events) < minimum:
        raise ValueError(f"pack has {len(events)} events; minimum is {minimum}")
    if len({event["cluster_id"] for event in events}) != len(events):
        raise ValueError("cluster_id values must be unique")

    registry = CaseRegistry(registry_path)
    composition = sample_composition(registry)
    annotator = VertexClusterAnnotator(
        project=project, location=location, model=model
    )
    results: list[dict[str, Any]] = []
    accepted = 0
    for event in events:
        try:
            leads = [_lead(event, raw) for raw in event["leads"]]
            cluster = _cluster(event, leads)
            result = annotator.annotate(cluster, leads, composition)
            candidate_id = "CAND-" + cluster.cluster_id.removeprefix("CLUSTER-")
            version, latest = candidate_version(registry, candidate_id)
            candidate = build_case_candidate(
                cluster,
                leads,
                result.annotation,
                composition,
                version=version,
                methodology_version=str(pack["methodology_version"]),
                selection_evidence={
                    "protocol": "authoritative-turning-point-crosscheck-v1",
                    "reviewed_pack_id": pack["pack_id"],
                    "expected_event_he": event["expected_event_he"],
                    "lane_id": event["lane_id"],
                    "lane_label_he": event["lane_label_he"],
                    "discovery_providers": list(cluster.provider_ids),
                    "independent_source_domains": len(cluster.source_domains),
                    "observed_languages": list(cluster.languages),
                    "institutional_consequences": event["institutional_consequences"],
                    "authoritative_crosscheck": {
                        "status": "PASSED",
                        "basis": "reviewed public primary, institutional and independent contemporaneous records",
                        "recorded_at": pack["reviewed_at"],
                    },
                    "vertex_canonicality_gate": {
                        "status": "PASSED",
                        "model": result.model_name,
                        "protocol_version": result.protocol_version,
                        "usage": dict(result.usage),
                    },
                    "note_he": pack["note_he"],
                },
            )
            status = "VALIDATED"
            if latest is not None and same_candidate_content(candidate, latest):
                candidate = latest
                status = "ALREADY_REGISTERED"
            elif apply:
                registry.register_candidate(candidate)
                status = "REGISTERED"
            accepted += 1
            results.append({
                "cluster_id": cluster.cluster_id,
                "expected_event_he": event["expected_event_he"],
                "status": status,
                "candidate": candidate,
                "usage": dict(result.usage),
            })
        except Exception as error:
            results.append({
                "cluster_id": event.get("cluster_id"),
                "expected_event_he": event.get("expected_event_he"),
                "status": "REJECTED_OR_FAILED",
                "error": str(error),
            })

    report = {
        "schema_version": "1.0",
        "pack_id": pack["pack_id"],
        "run_at": _utc_now(),
        "apply": apply,
        "minimum_required": minimum,
        "accepted": accepted,
        "failed": len(events) - accepted,
        "peak_rss_mb": round(_peak_rss_mb(), 3),
        "sample_composition_before": {
            "total_cases": composition.total_cases,
            "direct_cases": composition.direct_cases,
            "external_indirect_cases": composition.external_indirect_cases,
            "prewar_formative_cases": composition.prewar_formative_cases,
        },
        "results": results,
    }
    report_path = report_dir / (
        "canonical_expansion_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    write_json(report_path, report)
    report["report_path"] = str(report_path)
    if accepted < minimum:
        raise RuntimeError(
            f"only {accepted} candidates passed; required at least {minimum}; "
            f"see {report_path}"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--minimum", type=int, default=7)
    parser.add_argument("--cluster-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = register_pack(
        pack_path=args.pack.resolve(),
        registry_path=args.registry.resolve(),
        report_dir=args.report_dir.resolve(),
        project=args.project,
        location=args.location,
        model=args.model,
        apply=args.apply,
        minimum=args.minimum,
        cluster_ids=set(args.cluster_id) or None,
    )
    print(json.dumps({
        "accepted": report["accepted"],
        "failed": report["failed"],
        "apply": report["apply"],
        "peak_rss_mb": report["peak_rss_mb"],
        "report_path": report["report_path"],
        "titles": [
            item["candidate"]["title"]
            for item in report["results"]
            if item["status"] in {"REGISTERED", "VALIDATED", "ALREADY_REGISTERED"}
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
