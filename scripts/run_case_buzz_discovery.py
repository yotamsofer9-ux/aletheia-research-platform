#!/usr/bin/env python3
"""Discover canonical October-war case candidates from normalized attention peaks."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.case_candidate_annotation import VertexClusterAnnotator  # noqa: E402
from aletheia_lite.case_discovery import (  # noqa: E402
    CoveragePeak,
    CoveragePoint,
    DiscoveryQuery,
    EventCluster,
    EventLead,
    GDELTDocLeadProvider,
    GDELTTimelineVolumeProvider,
    GoogleNewsRSSLeadProvider,
    build_case_candidate,
    cluster_event_leads,
    cluster_ids_to_leads,
    detect_coverage_peaks,
)
from aletheia_lite.case_generator_contracts import candidate_content_hash  # noqa: E402
from aletheia_lite.case_registry import CaseRegistry  # noqa: E402
from aletheia_lite.collector import BoundedHTTPClient  # noqa: E402
from aletheia_lite.config import NetworkLimits  # noqa: E402
from scripts.run_case_discovery import (  # noqa: E402
    DEFAULT_CACHE,
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    DEFAULT_PROJECT,
    DEFAULT_REGISTRY,
    bounded_cluster,
    candidate_version,
    discover_provider_with_cache,
    gdelt_seed_clusters,
    same_candidate_content,
    sample_composition,
    verification_phrase,
    write_json,
)


DEFAULT_FRAME = ROOT / "config/case_buzz_discovery_frame_v1.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024 * 1024) if sys.platform == "darwin" else float(value) / 1024


def _timeline_hash(query: DiscoveryQuery) -> str:
    payload = {
        "query_id": query.query_id,
        "window_start": query.window_start.isoformat(),
        "window_end": query.window_end.isoformat(),
        "terms": list(query.terms.get("en", ())),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _timeline_cache_path(
    cache_root: Path, frame_version: str, query: DiscoveryQuery
) -> Path:
    return (
        cache_root
        / frame_version
        / query.query_id
        / f"gdelt-timeline-{_timeline_hash(query)[:12]}.json"
    )


def discover_timeline_with_cache(
    provider: GDELTTimelineVolumeProvider,
    query: DiscoveryQuery,
    *,
    cache_root: Path,
    frame_version: str,
    refresh: bool,
) -> tuple[list[CoveragePoint], dict[str, Any]]:
    path = _timeline_cache_path(cache_root, frame_version, query)
    if path.exists() and not refresh:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload["schema_version"] == "1.0"
                and payload["frame_version"] == frame_version
                and payload["query_hash"] == _timeline_hash(query)
            ):
                points = [
                    CoveragePoint(
                        day=date.fromisoformat(raw["day"]),
                        article_count=int(raw["article_count"]),
                        monitored_article_count=int(raw["monitored_article_count"]),
                        normalized_share=float(raw["normalized_share"]),
                    )
                    for raw in payload["points"]
                ]
                return points, {
                    "mode": "CACHE_HIT",
                    "path": str(path),
                    "retrieved_at": payload.get("retrieved_at"),
                    "point_count": len(points),
                }
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            pass
    points = provider.discover(query)
    retrieved_at = utc_now()
    write_json(
        path,
        {
            "schema_version": "1.0",
            "frame_version": frame_version,
            "query_hash": _timeline_hash(query),
            "retrieved_at": retrieved_at,
            "points": [point.to_dict() for point in points],
        },
    )
    return points, {
        "mode": "NETWORK_FETCH",
        "path": str(path),
        "retrieved_at": retrieved_at,
        "point_count": len(points),
    }


def _peak_key(item: dict[str, Any]) -> tuple[float, int, str]:
    peak: CoveragePeak = item["peak"]
    return (peak.buzz_index, peak.article_count, peak.day.isoformat())


def select_peak_anchors(
    detected: list[dict[str, Any]], *, maximum: int
) -> list[dict[str, Any]]:
    """Round-robin by time window, then fill by buzz; collapse nearby duplicates."""

    selected: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        if any(
            abs((item["peak"].day - existing["peak"].day).days) <= 3
            and item["lane_id"] == existing["lane_id"]
            for existing in selected
        ):
            return
        selected.append(item)

    windows = sorted({item["window_id"] for item in detected})
    for window_id in windows:
        window_items = sorted(
            (item for item in detected if item["window_id"] == window_id),
            key=_peak_key,
            reverse=True,
        )
        if window_items:
            add(window_items[0])
        if len(selected) >= maximum:
            return selected[:maximum]
    for item in sorted(detected, key=_peak_key, reverse=True):
        add(item)
        if len(selected) >= maximum:
            break
    return selected[:maximum]


def _combined_cluster(
    seed: EventCluster,
    all_peak_leads: list[EventLead],
    verified_leads: list[EventLead],
) -> EventCluster | None:
    seed_ids = set(seed.lead_ids)
    combined = list({lead.lead_id: lead for lead in all_peak_leads + verified_leads}.values())
    clusters = cluster_event_leads(
        combined,
        entity_aliases={},
        max_day_gap=2,
        title_similarity_threshold=0.24,
        min_shared_entities=99,
    )
    eligible = [
        cluster
        for cluster in clusters
        if seed_ids & set(cluster.lead_ids)
        and len(cluster.provider_ids) >= 2
        and len(cluster.source_domains) >= 2
        and len(cluster.lead_ids) >= 3
    ]
    if not eligible:
        if (
            len(seed.source_domains) >= 4 and len(seed.languages) >= 2
        ) or len(seed.source_domains) >= 6:
            return seed
        return None
    return max(
        eligible,
        key=lambda cluster: (
            len(cluster.languages),
            len(cluster.source_domains),
            len(cluster.lead_ids),
        ),
    )


def discover_buzz_candidates(
    *,
    frame_path: Path,
    registry_path: Path,
    annotate: bool,
    project: str,
    location: str,
    model: str,
    report_dir: Path | None,
    cache_root: Path,
    refresh_cache: bool,
    window_ids: set[str] | None = None,
    lane_ids: set[str] | None = None,
) -> dict[str, Any]:
    frame = json.loads(frame_path.read_text(encoding="utf-8"))
    registry = CaseRegistry(registry_path)
    composition = sample_composition(registry)
    gdelt_limits = NetworkLimits(
        timeout_seconds=12,
        max_response_bytes=6_000_000,
        requests_per_second_per_host=0.2,
        retries=0,
        min_text_chars=1,
        max_text_chars=30_000,
    )
    google_limits = NetworkLimits(
        timeout_seconds=15,
        max_response_bytes=5_000_000,
        requests_per_second_per_host=0.5,
        retries=1,
        min_text_chars=1,
        max_text_chars=30_000,
    )
    timeline_provider = GDELTTimelineVolumeProvider(BoundedHTTPClient(gdelt_limits))
    gdelt_provider = GDELTDocLeadProvider(BoundedHTTPClient(gdelt_limits))
    google_provider = GoogleNewsRSSLeadProvider(BoundedHTTPClient(google_limits))

    timeline_reports: list[dict[str, Any]] = []
    detected: list[dict[str, Any]] = []
    for raw_window in frame["windows"]:
        if window_ids and raw_window["window_id"] not in window_ids:
            continue
        for lane in frame["lanes"]:
            if lane_ids and lane["lane_id"] not in lane_ids:
                continue
            query = DiscoveryQuery(
                query_id=f"{raw_window['window_id']}-{lane['lane_id']}",
                topic_tag=f"{lane['lane_id']}:{raw_window['window_id']}",
                window_start=date.fromisoformat(raw_window["window_start"]),
                window_end=date.fromisoformat(raw_window["window_end"]),
                terms={
                    "he": (),
                    "en": tuple(lane.get("timeline_terms_en") or lane["terms"]["en"]),
                    "ar": (),
                },
                max_leads_per_provider=int(frame["max_leads_per_provider_per_peak"]),
            )
            try:
                points, cache = discover_timeline_with_cache(
                    timeline_provider,
                    query,
                    cache_root=cache_root,
                    frame_version=frame["frame_version"],
                    refresh=refresh_cache,
                )
                peaks = detect_coverage_peaks(
                    points,
                    maximum=int(frame["max_peaks_per_lane_window"]),
                    baseline_radius_days=int(frame["baseline_radius_days"]),
                    local_maximum_radius_days=int(frame["local_maximum_radius_days"]),
                    minimum_separation_days=int(frame["minimum_peak_separation_days"]),
                    minimum_lift=float(frame["minimum_peak_lift"]),
                    minimum_percentile=float(frame["minimum_window_percentile"]),
                    minimum_article_count=int(frame["minimum_peak_article_count"]),
                )
                for peak in peaks:
                    detected.append(
                        {
                            "window_id": raw_window["window_id"],
                            "lane_id": lane["lane_id"],
                            "lane_label_he": lane["label_he"],
                            "terms": lane["terms"],
                            "peak": peak,
                        }
                    )
                timeline_reports.append(
                    {
                        "window_id": raw_window["window_id"],
                        "lane_id": lane["lane_id"],
                        "lane_label_he": lane["label_he"],
                        "cache": cache,
                        "points": len(points),
                        "peaks": [peak.to_dict() for peak in peaks],
                        "failure": None,
                    }
                )
            except Exception as error:
                timeline_reports.append(
                    {
                        "window_id": raw_window["window_id"],
                        "lane_id": lane["lane_id"],
                        "lane_label_he": lane["label_he"],
                        "cache": None,
                        "points": 0,
                        "peaks": [],
                        "failure": str(error),
                    }
                )

    anchor_limit = max(int(frame["max_total_model_annotations"]), len(frame["windows"]))
    anchors = select_peak_anchors(detected, maximum=anchor_limit)
    cluster_records: list[dict[str, Any]] = []
    all_leads: list[EventLead] = []
    peak_reports: list[dict[str, Any]] = []
    radius = int(frame["article_window_days_each_side"])
    for anchor in anchors:
        peak: CoveragePeak = anchor["peak"]
        peak_query = DiscoveryQuery(
            query_id=f"PEAK-{anchor['lane_id']}-{peak.day.isoformat()}",
            topic_tag=f"peak:{anchor['lane_id']}:{peak.day.isoformat()}",
            window_start=peak.day - timedelta(days=radius),
            window_end=peak.day + timedelta(days=radius),
            terms={
                "he": tuple(anchor["terms"]["he"]),
                "en": tuple(
                    next(
                        lane.get("timeline_terms_en") or lane["terms"]["en"]
                        for lane in frame["lanes"]
                        if lane["lane_id"] == anchor["lane_id"]
                    )
                ),
                "ar": tuple(anchor["terms"]["ar"]),
            },
            max_leads_per_provider=int(frame["max_leads_per_provider_per_peak"]),
        )
        gdelt_leads, gdelt_failure, gdelt_cache = discover_provider_with_cache(
            gdelt_provider,
            peak_query,
            cache_root=cache_root,
            frame_version=frame["frame_version"],
            refresh=refresh_cache,
        )
        seeds = gdelt_seed_clusters(
            peak_query,
            gdelt_leads,
            entity_aliases=frame["entity_aliases"],
            maximum=int(frame["max_seed_clusters_per_peak"]),
        )
        seed_reports: list[dict[str, Any]] = []
        for seed in seeds:
            phrase, language = verification_phrase(seed, gdelt_leads)
            if not phrase:
                continue
            verification_query = DiscoveryQuery(
                query_id=f"{peak_query.query_id}-{seed.cluster_id}",
                topic_tag=peak_query.topic_tag,
                window_start=peak_query.window_start,
                window_end=peak_query.window_end,
                terms={
                    candidate_language: ((phrase,) if candidate_language == language else ())
                    for candidate_language in ("he", "en", "ar")
                },
                max_leads_per_provider=45,
            )
            verified, google_failure, google_cache = discover_provider_with_cache(
                google_provider,
                verification_query,
                cache_root=cache_root,
                frame_version=frame["frame_version"],
                refresh=refresh_cache,
            )
            combined = _combined_cluster(seed, gdelt_leads, verified)
            seed_reports.append(
                {
                    "seed_cluster": asdict(seed),
                    "verification_phrase": phrase,
                    "verification_language": language,
                    "google_leads": len(verified),
                    "google_cache": google_cache,
                    "failure": str(google_failure.message) if google_failure else None,
                    "eligible_cluster": asdict(combined) if combined else None,
                }
            )
            if combined:
                cluster_records.append(
                    {
                        "cluster": combined,
                        "anchor": anchor,
                    }
                )
                all_leads.extend(gdelt_leads)
                all_leads.extend(verified)
        peak_reports.append(
            {
                "window_id": anchor["window_id"],
                "lane_id": anchor["lane_id"],
                "lane_label_he": anchor["lane_label_he"],
                "peak": peak.to_dict(),
                "gdelt_leads": len(gdelt_leads),
                "gdelt_cache": gdelt_cache,
                "failure": gdelt_failure.message if gdelt_failure else None,
                "seeds": seed_reports,
            }
        )

    all_leads = list({lead.lead_id: lead for lead in all_leads}.values())
    deduplicated: list[dict[str, Any]] = []
    for record in sorted(cluster_records, key=lambda item: _peak_key(item["anchor"]), reverse=True):
        candidate_ids = set(record["cluster"].lead_ids)
        if any(
            len(candidate_ids & set(existing["cluster"].lead_ids))
            / min(len(candidate_ids), len(existing["cluster"].lead_ids))
            >= 0.5
            for existing in deduplicated
        ):
            continue
        record["cluster"] = bounded_cluster(
            record["cluster"], all_leads, int(frame["max_leads_per_annotation"])
        )
        deduplicated.append(record)
    selected = deduplicated[: int(frame["max_total_model_annotations"])]

    created: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    total_tokens = 0
    if annotate:
        annotator = VertexClusterAnnotator(
            project=project,
            location=location,
            model=model,
        )
        for record in selected:
            cluster = record["cluster"]
            anchor = record["anchor"]
            peak = anchor["peak"]
            try:
                result = annotator.annotate(cluster, all_leads, composition)
                selection_evidence = {
                    "protocol": "normalized-attention-peaks-v1",
                    "lane_id": anchor["lane_id"],
                    "lane_label_he": anchor["lane_label_he"],
                    "peak": peak.to_dict(),
                    "independent_source_domains": len(cluster.source_domains),
                    "discovery_providers": list(cluster.provider_ids),
                    "observed_languages": list(cluster.languages),
                    "note_he": (
                        "האירוע אותר לאחר חריגה בנפח סיקור מנורמל, ולא לפי "
                        "ייחוס אחריות או תוצאת ניקוד צפויה. מדד הבאזז הוא כלי גילוי בלבד."
                    ),
                }
                candidate = build_case_candidate(
                    cluster,
                    all_leads,
                    result.annotation,
                    composition,
                    methodology_version=frame["methodology_version"],
                    selection_evidence=selection_evidence,
                )
                version, latest = candidate_version(registry, candidate["candidate_id"])
                if latest and latest["registry_state"] in {"APPROVED", "REJECTED"}:
                    rejected.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "reason": "candidate already has a final human decision",
                        }
                    )
                    continue
                candidate["version"] = version
                candidate["candidate_hash"] = candidate_content_hash(candidate)
                if latest and same_candidate_content(latest, candidate):
                    created.append(
                        {
                            "candidate_id": latest["candidate_id"],
                            "version": latest["version"],
                            "candidate_hash": latest["candidate_hash"],
                            "title": latest["title"],
                            "reused_existing_version": True,
                        }
                    )
                else:
                    registry.register_candidate(candidate)
                    created.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "version": candidate["version"],
                            "candidate_hash": candidate["candidate_hash"],
                            "title": candidate["title"],
                            "reused_existing_version": False,
                        }
                    )
                total_tokens += int(result.usage.get("total_tokens") or 0)
            except Exception as error:
                rejected.append(
                    {"cluster_id": cluster.cluster_id, "reason": str(error)}
                )

    output = {
        "schema_version": "1.0",
        "frame_version": frame["frame_version"],
        "methodology_version": frame["methodology_version"],
        "generated_at": utc_now(),
        "mode": "ANNOTATE_AND_REGISTER" if annotate else "METADATA_ONLY_DRY_RUN",
        "timeline_reports": timeline_reports,
        "detected_peaks": [
            {
                "window_id": item["window_id"],
                "lane_id": item["lane_id"],
                "lane_label_he": item["lane_label_he"],
                "peak": item["peak"].to_dict(),
            }
            for item in detected
        ],
        "selected_peak_anchors": [
            {
                "window_id": item["window_id"],
                "lane_id": item["lane_id"],
                "lane_label_he": item["lane_label_he"],
                "peak": item["peak"].to_dict(),
            }
            for item in anchors
        ],
        "peak_reports": peak_reports,
        "selected_clusters": [
            {
                "cluster": asdict(item["cluster"]),
                "lane_id": item["anchor"]["lane_id"],
                "lane_label_he": item["anchor"]["lane_label_he"],
                "peak": item["anchor"]["peak"].to_dict(),
                "leads": [
                    lead.to_dict()
                    for lead in cluster_ids_to_leads(item["cluster"], all_leads)
                ],
            }
            for item in selected
        ],
        "registered_candidates": created,
        "rejected_annotations": rejected,
        "model_calls": len(created) + len(rejected) if annotate else 0,
        "total_model_tokens": total_tokens,
        "peak_rss_mb": peak_rss_mb(),
        "cloud_writes": 0,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_root = report_dir or ROOT / "analysis/case_generator/buzz_runs"
    report_path = report_root / f"buzz_discovery_{stamp}.json"
    write_json(report_path, output)
    try:
        output["report_path"] = str(report_path.relative_to(ROOT))
    except ValueError:
        output["report_path"] = str(report_path)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, default=DEFAULT_FRAME)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--annotate", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--window-id", action="append", dest="window_ids")
    parser.add_argument("--lane-id", action="append", dest="lane_ids")
    args = parser.parse_args()
    result = discover_buzz_candidates(
        frame_path=args.frame,
        registry_path=args.registry,
        annotate=args.annotate,
        project=args.project,
        location=args.location,
        model=args.model,
        report_dir=args.report_dir,
        cache_root=args.cache_root,
        refresh_cache=args.refresh_cache,
        window_ids=set(args.window_ids) if args.window_ids else None,
        lane_ids=set(args.lane_ids) if args.lane_ids else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
