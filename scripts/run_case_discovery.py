#!/usr/bin/env python3
"""Discover traceable case candidates from a fixed public metadata frame."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aletheia_lite.case_candidate_annotation import VertexClusterAnnotator  # noqa: E402
from aletheia_lite.case_discovery import (  # noqa: E402
    DiscoveryFailure,
    DiscoveryQuery,
    EventCluster,
    EventLead,
    GDELTDocLeadProvider,
    GoogleNewsRSSLeadProvider,
    SampleComposition,
    build_case_candidate,
    build_temporal_entity_bundles,
    cluster_event_leads,
    cluster_ids_to_leads,
)
from aletheia_lite.case_registry import CaseRegistry, CaseRegistryError  # noqa: E402
from aletheia_lite.collector import BoundedHTTPClient  # noqa: E402
from aletheia_lite.config import NetworkLimits  # noqa: E402


DEFAULT_FRAME = ROOT / "config/case_discovery_frame_v1.json"
DEFAULT_REGISTRY = ROOT / "data/case_registry.sqlite3"
DEFAULT_PROJECT = os.getenv("GCP_PROJECT_ID")
DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_CACHE = ROOT / "data/case_discovery_cache"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024 * 1024) if sys.platform == "darwin" else float(value) / 1024


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized or "unnamed"


def _query_payload(query: DiscoveryQuery) -> dict[str, Any]:
    return {
        "query_id": query.query_id,
        "topic_tag": query.topic_tag,
        "window_start": query.window_start.isoformat(),
        "window_end": query.window_end.isoformat(),
        "terms": {
            language: list(query.terms.get(language, ()))
            for language in ("he", "en", "ar")
        },
        "max_leads_per_provider": query.max_leads_per_provider,
    }


def _query_hash(query: DiscoveryQuery) -> str:
    encoded = json.dumps(
        _query_payload(query), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_cache_path(
    cache_root: Path,
    *,
    frame_version: str,
    query: DiscoveryQuery,
    provider_id: str,
) -> Path:
    return (
        cache_root
        / _safe_component(frame_version)
        / _safe_component(query.query_id)
        / f"{_safe_component(provider_id)}-{_query_hash(query)[:12]}.json"
    )


def _load_provider_cache(
    path: Path,
    *,
    frame_version: str,
    query: DiscoveryQuery,
    provider_id: str,
) -> tuple[list[EventLead] | None, str | None, str | None]:
    if not path.exists():
        return None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "1.0":
            raise ValueError("unsupported cache schema")
        if payload.get("frame_version") != frame_version:
            raise ValueError("frame version mismatch")
        if payload.get("provider_id") != provider_id:
            raise ValueError("provider mismatch")
        if payload.get("query_hash") != _query_hash(query):
            raise ValueError("query hash mismatch")
        leads = [EventLead(**raw) for raw in payload.get("leads", [])]
        if any(
            lead.provider_id != provider_id or lead.topic_tag != query.topic_tag
            for lead in leads
        ):
            raise ValueError("cached lead provenance mismatch")
        retrieved_at = str(payload.get("retrieved_at") or "") or None
        return leads, retrieved_at, None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, None, f"invalid local cache ignored: {error}"


def discover_provider_with_cache(
    provider: Any,
    query: DiscoveryQuery,
    *,
    cache_root: Path,
    frame_version: str,
    refresh: bool = False,
) -> tuple[list[EventLead], DiscoveryFailure | None, dict[str, Any]]:
    """Fetch one provider once, then replay its bounded metadata from local cache."""

    path = _provider_cache_path(
        cache_root,
        frame_version=frame_version,
        query=query,
        provider_id=provider.provider_id,
    )
    cached, retrieved_at, cache_note = _load_provider_cache(
        path,
        frame_version=frame_version,
        query=query,
        provider_id=provider.provider_id,
    )
    if cached is not None and not refresh:
        return cached, None, {
            "mode": "CACHE_HIT",
            "path": str(path),
            "retrieved_at": retrieved_at,
            "lead_count": len(cached),
        }
    try:
        leads = list(provider.discover(query))
        retrieved_at = utc_now()
        write_json(
            path,
            {
                "schema_version": "1.0",
                "frame_version": frame_version,
                "provider_id": provider.provider_id,
                "query_hash": _query_hash(query),
                "query": _query_payload(query),
                "retrieved_at": retrieved_at,
                "leads": [lead.to_dict() for lead in leads],
            },
        )
        return leads, None, {
            "mode": "REFRESHED" if refresh and cached is not None else "NETWORK_FETCH",
            "path": str(path),
            "retrieved_at": retrieved_at,
            "lead_count": len(leads),
            "cache_note": cache_note,
        }
    except Exception as error:  # provider failures are audit data, not global crashes
        failure = DiscoveryFailure(
            provider_id=provider.provider_id,
            message=str(error),
        )
        if cached is not None:
            return cached, failure, {
                "mode": "STALE_CACHE_AFTER_REFRESH_FAILURE",
                "path": str(path),
                "retrieved_at": retrieved_at,
                "lead_count": len(cached),
            }
        return [], failure, {
            "mode": "NETWORK_FAILURE",
            "path": str(path),
            "retrieved_at": None,
            "lead_count": 0,
            "cache_note": cache_note,
        }


def sample_composition(registry: CaseRegistry) -> SampleComposition:
    active = [
        item for item in registry.list_cases()
        if item["current_state"] != "REJECTED"
    ]
    scopes = [item["manifest"]["scope_relation"] for item in active if item["manifest"]]
    return SampleComposition(
        total_cases=1 + len(active),
        direct_cases=1 + sum(scope in {"CORE", "DIRECT_SPILLOVER"} for scope in scopes),
        external_indirect_cases=sum(scope == "EXTERNAL_INDIRECT" for scope in scopes),
        prewar_formative_cases=sum(scope == "PREWAR_FORMATIVE" for scope in scopes),
    )


def bounded_cluster(
    cluster: EventCluster, leads: list[EventLead], maximum: int
) -> EventCluster:
    members = cluster_ids_to_leads(cluster, leads)
    ordered = sorted(
        members,
        key=lambda item: (item.published_at or "", item.provider_id, item.lead_id),
    )
    selected: list[EventLead] = []
    selected_ids: set[str] = set()
    used_provider_language: set[tuple[str, str]] = set()
    title_counts: dict[str, int] = {}

    def title_key(member: EventLead) -> str:
        title = re.sub(r"\s+", " ", member.title).strip().lower()
        for separator in (" – ", " | ", " - "):
            if separator in title:
                head, tail = title.rsplit(separator, 1)
                if len(head.split()) >= 5 and len(tail.split()) <= 5:
                    title = head
        return re.sub(r"[^\w\u0590-\u05ff\u0600-\u06ff]+", "", title)

    def add(member: EventLead) -> None:
        selected.append(member)
        selected_ids.add(member.lead_id)
        key = title_key(member)
        title_counts[key] = title_counts.get(key, 0) + 1

    for member in ordered:
        key = (member.provider_id, member.language)
        if key not in used_provider_language:
            add(member)
            used_provider_language.add(key)
        if len(selected) >= maximum:
            break
    for member in ordered:
        if len(selected) >= maximum:
            break
        key = title_key(member)
        if member.lead_id not in selected_ids and key not in title_counts:
            add(member)
    for member in ordered:
        if len(selected) >= maximum:
            break
        key = title_key(member)
        if member.lead_id not in selected_ids and title_counts.get(key, 0) < 2:
            add(member)
    bounded_ids = tuple(sorted(item.lead_id for item in selected))
    dates = sorted(
        item.published_at[:10] for item in selected if item.published_at
    )
    return replace(
        cluster,
        lead_ids=bounded_ids,
        provider_ids=tuple(sorted({item.provider_id for item in selected})),
        source_domains=tuple(sorted({item.source_domain for item in selected})),
        languages=tuple(sorted({item.language for item in selected})),
        window_start=dates[0] if dates else cluster.window_start,
        window_end=dates[-1] if dates else cluster.window_end,
    )


def cluster_rank(cluster: EventCluster) -> tuple[int, int, int, int, str]:
    return (
        len(cluster.languages),
        len(cluster.provider_ids),
        min(len(cluster.source_domains), 12),
        len(cluster.lead_ids),
        cluster.window_start,
    )


def verification_phrase(
    cluster: EventCluster, leads: list[EventLead]
) -> tuple[str, str]:
    """Derive a narrow observed-title query without asking a model to invent an event."""

    members = cluster_ids_to_leads(cluster, leads)
    if not members:
        return "", "en"
    title_counts: dict[str, int] = {}
    original_by_normalized: dict[str, str] = {}
    language_by_normalized: dict[str, str] = {}
    for member in members:
        title = re.sub(r"\s+", " ", member.title).strip()
        for separator in (" – ", " | "):
            if separator in title:
                title = title.split(separator, 1)[0].strip()
        normalized = re.sub(r"[^\w\u0590-\u05ff\u0600-\u06ff]+", " ", title.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized:
            title_counts[normalized] = title_counts.get(normalized, 0) + 1
            original_by_normalized.setdefault(normalized, title)
            language_by_normalized.setdefault(normalized, member.language)
    if not title_counts:
        return "", "en"
    selected = max(
        title_counts,
        key=lambda value: (title_counts[value], len(value.split()), value),
    )
    words = original_by_normalized[selected].split()
    return " ".join(words[:14]), language_by_normalized[selected]


def gdelt_seed_clusters(
    query: DiscoveryQuery,
    leads: list[EventLead],
    *,
    entity_aliases: dict[str, list[str]],
    maximum: int,
) -> list[EventCluster]:
    """Find dense same-event seeds without using responsibility or media scores."""

    gdelt_leads = [lead for lead in leads if lead.provider_id == "gdelt-doc-2"]
    clusters = cluster_event_leads(
        gdelt_leads,
        entity_aliases=entity_aliases,
        max_day_gap=1,
        title_similarity_threshold=0.55,
        min_shared_entities=2,
    )
    eligible = [
        cluster
        for cluster in clusters
        if cluster.topic_tag == query.topic_tag
        and len(cluster.source_domains) >= 2
        and len(cluster.lead_ids) >= 2
    ]
    eligible.sort(
        key=lambda cluster: (
            min(len(cluster.source_domains), 12),
            len(cluster.lead_ids),
            cluster.window_start,
        ),
        reverse=True,
    )
    return eligible[:maximum]


def candidate_version(
    registry: CaseRegistry, candidate_id: str
) -> tuple[int, dict[str, Any] | None]:
    version = 1
    latest = None
    while True:
        try:
            existing = registry.get_candidate(candidate_id, version)
        except CaseRegistryError:
            return version, latest
        latest = existing
        version += 1


def same_candidate_content(left: dict[str, Any], right: dict[str, Any]) -> bool:
    ignored = {"candidate_hash", "version", "state", "registry_state"}
    return {
        key: value for key, value in left.items() if key not in ignored
    } == {
        key: value for key, value in right.items() if key not in ignored
    }


def discover_candidates(
    *,
    frame_path: Path,
    registry_path: Path,
    annotate: bool,
    project: str,
    location: str,
    model: str,
    report_dir: Path | None = None,
    query_ids: set[str] | None = None,
    cache_root: Path | None = None,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    frame = json.loads(frame_path.read_text(encoding="utf-8"))
    registry = CaseRegistry(registry_path)
    composition = sample_composition(registry)
    google_limits = NetworkLimits(
        timeout_seconds=12,
        max_response_bytes=5_000_000,
        requests_per_second_per_host=0.5,
        retries=1,
        min_text_chars=1,
        max_text_chars=30_000,
    )
    gdelt_limits = NetworkLimits(
        timeout_seconds=15,
        max_response_bytes=5_000_000,
        requests_per_second_per_host=0.1,
        retries=2,
        min_text_chars=1,
        max_text_chars=30_000,
    )
    providers = [
        GDELTDocLeadProvider(BoundedHTTPClient(gdelt_limits)),
        GoogleNewsRSSLeadProvider(BoundedHTTPClient(google_limits)),
    ]
    provider_by_id = {provider.provider_id: provider for provider in providers}
    all_leads: list[EventLead] = []
    query_reports: list[dict[str, Any]] = []
    ranked_by_window: list[list[EventCluster]] = []
    for raw_window in frame["windows"]:
        if query_ids and raw_window["query_id"] not in query_ids:
            continue
        query = DiscoveryQuery(
            query_id=raw_window["query_id"],
            topic_tag=raw_window["topic_tag"],
            window_start=date.fromisoformat(raw_window["window_start"]),
            window_end=date.fromisoformat(raw_window["window_end"]),
            terms={
                language: tuple(frame["terms"][language])
                for language in ("he", "en", "ar")
            },
            max_leads_per_provider=int(frame["max_leads_per_provider_per_window"]),
        )
        leads: list[EventLead] = []
        failures: list[DiscoveryFailure] = []
        cache_diagnostics: dict[str, dict[str, Any]] = {}
        for provider in providers:
            provider_leads, failure, cache_status = discover_provider_with_cache(
                provider,
                query,
                cache_root=cache_root or DEFAULT_CACHE,
                frame_version=frame["frame_version"],
                refresh=refresh_cache,
            )
            leads.extend(provider_leads)
            if failure:
                failures.append(failure)
            cache_diagnostics[provider.provider_id] = cache_status
        leads = list({lead.lead_id: lead for lead in leads}.values())
        leads.sort(key=lambda lead: (lead.published_at or "", lead.lead_id))
        verification_reports: list[dict[str, Any]] = []
        verified_lead_ids: set[str] = set()
        seed_limit = int(frame.get("max_seed_verification_queries_per_window", 0))
        if seed_limit:
            seeds = gdelt_seed_clusters(
                query,
                leads,
                entity_aliases=frame["entity_aliases"],
                maximum=seed_limit,
            )
            for seed in seeds:
                phrase, language = verification_phrase(seed, leads)
                if not phrase or language not in {"he", "en", "ar"}:
                    continue
                seed_start = date.fromisoformat(seed.window_start) - timedelta(days=1)
                seed_end = date.fromisoformat(seed.window_end) + timedelta(days=1)
                verification_query = DiscoveryQuery(
                    query_id=f"{query.query_id}-{seed.cluster_id}",
                    topic_tag=query.topic_tag,
                    window_start=seed_start,
                    window_end=seed_end,
                    terms={
                        candidate_language: ((phrase,) if candidate_language == language else ())
                        for candidate_language in ("he", "en", "ar")
                    },
                    max_leads_per_provider=min(30, query.max_leads_per_provider),
                )
                verified_leads, failure, cache_status = discover_provider_with_cache(
                    provider_by_id["google-news-rss"],
                    verification_query,
                    cache_root=cache_root or DEFAULT_CACHE,
                    frame_version=frame["frame_version"],
                    refresh=refresh_cache,
                )
                leads.extend(verified_leads)
                verified_lead_ids.update(lead.lead_id for lead in verified_leads)
                if failure:
                    failures.append(failure)
                verification_reports.append(
                    {
                        "seed_cluster": asdict(seed),
                        "query_phrase": phrase,
                        "query_language": language,
                        "returned_leads": len(verified_leads),
                        "cache": cache_status,
                        "failure": asdict(failure) if failure else None,
                    }
                )
            leads = list({lead.lead_id: lead for lead in leads}.values())
            leads.sort(key=lambda lead: (lead.published_at or "", lead.lead_id))
        all_leads.extend(leads)
        clusters = cluster_event_leads(
            leads,
            entity_aliases=frame["entity_aliases"],
            max_day_gap=1,
            title_similarity_threshold=0.35,
            min_shared_entities=2,
        )
        eligible = [
            cluster for cluster in clusters
            if len(cluster.provider_ids) >= 2
            and len(cluster.source_domains) >= 2
            and len(cluster.lead_ids) >= 3
            and (
                not seed_limit
                or bool(set(cluster.lead_ids) & verified_lead_ids)
            )
        ]
        fallback_bundles = build_temporal_entity_bundles(
            leads,
            entity_aliases=frame["entity_aliases"],
        )
        known_memberships = {cluster.lead_ids for cluster in eligible}
        eligible.extend(
            bundle
            for bundle in fallback_bundles
            if bundle.lead_ids not in known_memberships
            and (
                not seed_limit
                or bool(set(bundle.lead_ids) & verified_lead_ids)
            )
        )
        eligible.sort(key=cluster_rank, reverse=True)
        deduplicated: list[EventCluster] = []
        for candidate_cluster in eligible:
            candidate_ids = set(candidate_cluster.lead_ids)
            if any(
                len(candidate_ids & set(existing.lead_ids))
                / min(len(candidate_ids), len(existing.lead_ids))
                >= 0.5
                for existing in deduplicated
            ):
                continue
            deduplicated.append(candidate_cluster)
        eligible = deduplicated
        ranked_by_window.append(eligible)
        cross_provider = [
            cluster for cluster in clusters if len(cluster.provider_ids) >= 2
        ]
        diagnostic_clusters = sorted(
            cross_provider, key=cluster_rank, reverse=True
        )[:5]
        lead_by_id = {lead.lead_id: lead for lead in leads}
        provider_diagnostics = {}
        for provider_id in sorted({lead.provider_id for lead in leads}):
            provider_leads = [
                lead for lead in leads if lead.provider_id == provider_id
            ]
            dates = sorted(
                lead.published_at for lead in provider_leads if lead.published_at
            )
            provider_diagnostics[provider_id] = {
                "count": len(provider_leads),
                "languages": {
                    language: sum(
                        lead.language == language for lead in provider_leads
                    )
                    for language in ("he", "en", "ar")
                },
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "sample": [
                    {
                        "published_at": lead.published_at,
                        "language": lead.language,
                        "title": lead.title,
                    }
                    for lead in sorted(
                        provider_leads,
                        key=lambda item: (item.published_at or "", item.lead_id),
                    )[:5]
                ],
            }
        query_reports.append(
            {
                "query": _query_payload(query),
                "leads": len(leads),
                "clusters": len(clusters),
                "eligible_clusters": len(eligible),
                "cross_provider_clusters": len(cross_provider),
                "cross_provider_clusters_with_three_leads": sum(
                    len(cluster.lead_ids) >= 3 for cluster in cross_provider
                ),
                "cross_provider_language_counts": {
                    str(count): sum(
                        len(cluster.languages) == count for cluster in cross_provider
                    )
                    for count in (1, 2, 3)
                },
                "diagnostic_clusters": [
                    {
                        "cluster": asdict(cluster),
                        "leads": [
                            {
                                "provider_id": lead_by_id[lead_id].provider_id,
                                "language": lead_by_id[lead_id].language,
                                "source_domain": lead_by_id[lead_id].source_domain,
                                "published_at": lead_by_id[lead_id].published_at,
                                "title": lead_by_id[lead_id].title,
                            }
                            for lead_id in cluster.lead_ids[:8]
                        ],
                    }
                    for cluster in diagnostic_clusters
                ],
                "provider_diagnostics": provider_diagnostics,
                "provider_cache": cache_diagnostics,
                "seed_verification": verification_reports,
                "failures": [asdict(item) for item in failures],
            }
        )

    selected_clusters: list[EventCluster] = []
    per_window = int(frame["max_clusters_per_window_for_annotation"])
    for rank in range(per_window):
        for eligible in ranked_by_window:
            if rank < len(eligible):
                selected_clusters.append(eligible[rank])
    selected_clusters = selected_clusters[: int(frame["max_total_model_annotations"])]
    selected_clusters = [
        bounded_cluster(
            cluster, all_leads, int(frame["max_leads_per_annotation"])
        )
        for cluster in selected_clusters
    ]

    created: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    total_tokens = 0
    if annotate:
        annotator = VertexClusterAnnotator(
            project=project, location=location, model=model
        )
        for cluster in selected_clusters:
            try:
                result = annotator.annotate(cluster, all_leads, composition)
                candidate = build_case_candidate(
                    cluster,
                    all_leads,
                    result.annotation,
                    composition,
                )
                version, latest = candidate_version(
                    registry, candidate["candidate_id"]
                )
                if latest and latest["registry_state"] in {"APPROVED", "REJECTED"}:
                    rejected.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "reason": (
                                "candidate already has a final human decision: "
                                f"{latest['registry_state']}"
                            ),
                        }
                    )
                    continue
                if latest and same_candidate_content(latest, candidate):
                    created.append(
                        {
                            "candidate_id": latest["candidate_id"],
                            "version": latest["version"],
                            "candidate_hash": latest["candidate_hash"],
                            "title": latest["title"],
                            "cluster_id": cluster.cluster_id,
                            "usage": dict(result.usage),
                            "reused_existing_version": True,
                        }
                    )
                    total_tokens += int(result.usage.get("total_tokens") or 0)
                    continue
                candidate["version"] = version
                from aletheia_lite.case_generator_contracts import candidate_content_hash

                candidate["candidate_hash"] = candidate_content_hash(candidate)
                registry.register_candidate(candidate)
                usage = dict(result.usage)
                total_tokens += int(usage.get("total_tokens") or 0)
                created.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "version": candidate["version"],
                        "candidate_hash": candidate["candidate_hash"],
                        "title": candidate["title"],
                        "cluster_id": cluster.cluster_id,
                        "usage": usage,
                    }
                )
            except Exception as error:
                rejected.append(
                    {"cluster_id": cluster.cluster_id, "reason": str(error)}
                )

    generated_at = utc_now()
    output = {
        "schema_version": "1.0",
        "frame_version": frame["frame_version"],
        "generated_at": generated_at,
        "mode": "ANNOTATE_AND_REGISTER" if annotate else "METADATA_ONLY_DRY_RUN",
        "providers": [provider.provider_id for provider in providers],
        "query_reports": query_reports,
        "selected_clusters": [asdict(cluster) for cluster in selected_clusters],
        "selected_cluster_evidence": [
            {
                "cluster": asdict(cluster),
                "leads": [
                    {
                        "lead_id": lead.lead_id,
                        "provider_id": lead.provider_id,
                        "language": lead.language,
                        "source_name": lead.source_name,
                        "source_domain": lead.source_domain,
                        "published_at": lead.published_at,
                        "title": lead.title,
                        "url": lead.canonical_url,
                    }
                    for lead in cluster_ids_to_leads(cluster, all_leads)
                ],
            }
            for cluster in selected_clusters
        ],
        "registered_candidates": created,
        "rejected_annotations": rejected,
        "model_calls": len(created) + len(rejected) if annotate else 0,
        "total_model_tokens": total_tokens,
        "peak_rss_mb": peak_rss_mb(),
        "cloud_writes": 0,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_root = report_dir or (ROOT / "analysis/case_generator/discovery_runs")
    report_path = report_root / f"discovery_{stamp}.json"
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
    parser.add_argument("--annotate", action="store_true")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--query-id", action="append", default=[])
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Retry providers while retaining a valid previous cache on failure.",
    )
    args = parser.parse_args()
    result = discover_candidates(
        frame_path=args.frame,
        registry_path=args.registry,
        annotate=args.annotate,
        project=args.project,
        location=args.location,
        model=args.model,
        query_ids=set(args.query_id) or None,
        cache_root=args.cache_root,
        refresh_cache=args.refresh_cache,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
