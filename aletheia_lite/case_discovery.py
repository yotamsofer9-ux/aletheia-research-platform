from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import feedparser

from .case_generator_contracts import candidate_content_hash
from .collector import BoundedHTTPClient, CollectionError, canonicalize_url


GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
SUPPORTED_LANGUAGES = frozenset({"he", "en", "ar"})
DIRECT_SCOPE_RELATIONS = frozenset({"CORE", "DIRECT_SPILLOVER"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = re.sub(r"[^\w\u0590-\u05ff\u0600-\u06ff]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _iso_date_or_datetime(value: str | None) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _rss_entry_datetime(entry: Mapping[str, Any]) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        parts = entry.get(key)
        if parts:
            return datetime(*parts[:6], tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
    for key in ("published", "updated"):
        parsed = _parse_datetime(str(entry.get(key) or ""))
        if parsed:
            return parsed.isoformat().replace("+00:00", "Z")
    return None


@dataclass(frozen=True)
class DiscoveryQuery:
    query_id: str
    topic_tag: str
    window_start: date
    window_end: date
    terms: Mapping[str, tuple[str, ...]]
    max_leads_per_provider: int = 75

    def __post_init__(self) -> None:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        if not 1 <= self.max_leads_per_provider <= 250:
            raise ValueError("max_leads_per_provider must be between 1 and 250")
        if not any(self.terms.get(language) for language in SUPPORTED_LANGUAGES):
            raise ValueError("at least one Hebrew, English or Arabic term is required")


@dataclass(frozen=True)
class EventLead:
    lead_id: str
    provider_id: str
    provider_record_id: str
    source_name: str
    source_domain: str
    url: str
    canonical_url: str
    title: str
    summary: str
    published_at: str | None
    language: str
    topic_tag: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryFailure:
    provider_id: str
    message: str


@dataclass(frozen=True)
class DiscoveryReport:
    query_id: str
    leads: tuple[EventLead, ...]
    failures: tuple[DiscoveryFailure, ...]


class EventLeadProvider(Protocol):
    provider_id: str

    def discover(self, query: DiscoveryQuery) -> list[EventLead]: ...


class RSSLeadProvider:
    """One bounded public RSS feed; stores metadata only, never article bodies."""

    def __init__(
        self,
        *,
        provider_id: str,
        source_name: str,
        feed_url: str,
        language: str,
        client: BoundedHTTPClient | Any,
    ):
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported RSS language: {language}")
        self.provider_id = provider_id
        self.source_name = source_name
        self.feed_url = feed_url
        self.language = language
        self.client = client

    def discover(self, query: DiscoveryQuery) -> list[EventLead]:
        result = self.client.get(self.feed_url)
        feed = feedparser.parse(result.content)
        terms = tuple(_normalize(term) for term in query.terms.get(self.language, ()))
        leads: list[EventLead] = []
        for index, entry in enumerate(feed.entries):
            url = str(entry.get("link") or "").strip()
            title = str(entry.get("title") or "").strip()
            summary = str(entry.get("summary") or "").strip()
            if not url or not title:
                continue
            searchable = _normalize(f"{title} {summary}")
            if terms and not any(term in searchable for term in terms):
                continue
            published_at = _rss_entry_datetime(entry)
            parsed = _parse_datetime(published_at)
            if parsed and not query.window_start <= parsed.date() <= query.window_end:
                continue
            canonical = canonicalize_url(url)
            record_id = str(entry.get("id") or entry.get("guid") or canonical)
            leads.append(
                EventLead(
                    lead_id=_sha256(f"{self.provider_id}|{record_id}|{canonical}"),
                    provider_id=self.provider_id,
                    provider_record_id=record_id,
                    source_name=self.source_name,
                    source_domain=urlparse(canonical).hostname or "",
                    url=url,
                    canonical_url=canonical,
                    title=title,
                    summary=summary,
                    published_at=published_at,
                    language=self.language,
                    topic_tag=query.topic_tag,
                )
            )
            if len(leads) >= query.max_leads_per_provider:
                break
        return _unique_leads(leads)


class GDELTDocLeadProvider:
    """Bounded adapter for GDELT DOC 2.0 ArticleList JSON metadata."""

    provider_id = "gdelt-doc-2"

    LANGUAGE_MAP = {
        "arabic": "ar",
        "ar": "ar",
        "english": "en",
        "en": "en",
        "hebrew": "he",
        "he": "he",
    }

    def __init__(self, client: BoundedHTTPClient | Any):
        self.client = client

    def discover(self, query: DiscoveryQuery) -> list[EventLead]:
        english_terms = tuple(term.strip() for term in query.terms.get("en", ()) if term.strip())
        if not english_terms:
            return []
        expression = "(" + " OR ".join(_quote_gdelt(term) for term in english_terms) + ")"
        params = {
            "query": expression,
            "mode": "artlist",
            "format": "json",
            "sort": "hybridrel",
            # Broad six-month discovery queries become needlessly expensive at
            # the API maximum. A smaller cap is enough to seed events and is
            # materially less likely to trigger GDELT's overload protection.
            "maxrecords": str(min(query.max_leads_per_provider, 75)),
            "startdatetime": query.window_start.strftime("%Y%m%d000000"),
            "enddatetime": query.window_end.strftime("%Y%m%d235959"),
        }
        result = self.client.get(GDELT_DOC_API, params=params)
        try:
            payload = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CollectionError("GDELT returned invalid JSON") from error

        leads: list[EventLead] = []
        for raw in payload.get("articles") or []:
            url = str(raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            language = self.LANGUAGE_MAP.get(str(raw.get("language") or "").lower())
            if not url or not title or language not in SUPPORTED_LANGUAGES:
                continue
            canonical = canonicalize_url(url)
            domain = str(raw.get("domain") or urlparse(canonical).hostname or "")
            published_at = _iso_date_or_datetime(str(raw.get("seendate") or ""))
            record_id = str(raw.get("url") or canonical)
            leads.append(
                EventLead(
                    lead_id=_sha256(f"{self.provider_id}|{record_id}|{canonical}"),
                    provider_id=self.provider_id,
                    provider_record_id=record_id,
                    source_name=domain,
                    source_domain=domain,
                    url=url,
                    canonical_url=canonical,
                    title=title,
                    summary="",
                    published_at=published_at,
                    language=language,
                    topic_tag=query.topic_tag,
                )
            )
        return _unique_leads(leads)[: query.max_leads_per_provider]


@dataclass(frozen=True)
class CoveragePoint:
    day: date
    article_count: int
    monitored_article_count: int
    normalized_share: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["day"] = self.day.isoformat()
        return payload


@dataclass(frozen=True)
class CoveragePeak:
    day: date
    article_count: int
    monitored_article_count: int
    normalized_share: float
    local_baseline_share: float
    lift_over_local_baseline: float
    window_percentile: float
    buzz_index: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["day"] = self.day.isoformat()
        return payload


class GDELTTimelineVolumeProvider:
    """Read-only GDELT daily counts used to find attention spikes before stories."""

    provider_id = "gdelt-timelinevolraw-2"

    def __init__(self, client: BoundedHTTPClient | Any):
        self.client = client

    def discover(self, query: DiscoveryQuery) -> list[CoveragePoint]:
        english_terms = tuple(
            term.strip() for term in query.terms.get("en", ()) if term.strip()
        )
        if not english_terms:
            return []
        expression = "(" + " OR ".join(_quote_gdelt(term) for term in english_terms) + ")"
        params = {
            "query": expression,
            "mode": "timelinevolraw",
            "format": "json",
            "startdatetime": query.window_start.strftime("%Y%m%d000000"),
            "enddatetime": query.window_end.strftime("%Y%m%d235959"),
        }
        result = self.client.get(GDELT_DOC_API, params=params)
        try:
            payload = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CollectionError("GDELT timeline returned invalid JSON") from error

        series = next(
            (
                item
                for item in payload.get("timeline") or []
                if str(item.get("series") or "").lower() == "article count"
            ),
            None,
        )
        if not series:
            raise CollectionError("GDELT timeline omitted the Article Count series")
        points: list[CoveragePoint] = []
        for raw in series.get("data") or []:
            parsed = _parse_datetime(str(raw.get("date") or ""))
            if not parsed:
                continue
            article_count = max(0, int(raw.get("value") or 0))
            monitored_count = max(0, int(raw.get("norm") or 0))
            if monitored_count <= 0:
                continue
            points.append(
                CoveragePoint(
                    day=parsed.date(),
                    article_count=article_count,
                    monitored_article_count=monitored_count,
                    normalized_share=article_count / monitored_count,
                )
            )
        return sorted(points, key=lambda point: point.day)


def detect_coverage_peaks(
    points: Sequence[CoveragePoint],
    *,
    maximum: int = 2,
    baseline_radius_days: int = 14,
    local_maximum_radius_days: int = 2,
    minimum_separation_days: int = 14,
    minimum_lift: float = 1.25,
    minimum_percentile: float = 0.75,
    minimum_article_count: int = 50,
) -> list[CoveragePeak]:
    """Detect locally exceptional attention without using event responsibility.

    The index is an operational discovery aid, never a media-performance or
    research score. It compares normalized daily share with a symmetric local
    baseline and then enforces time separation between selected peaks.
    """

    if maximum < 1:
        raise ValueError("maximum must be positive")
    if baseline_radius_days < 3:
        raise ValueError("baseline_radius_days must be at least three")
    if local_maximum_radius_days < 1:
        raise ValueError("local_maximum_radius_days must be positive")
    if minimum_separation_days < 1:
        raise ValueError("minimum_separation_days must be positive")
    ordered = sorted(points, key=lambda point: point.day)
    if len(ordered) < 3:
        return []
    shares = sorted(point.normalized_share for point in ordered)

    def percentile(value: float) -> float:
        if len(shares) == 1:
            return 1.0
        rank = sum(candidate <= value for candidate in shares) - 1
        return max(0.0, rank / (len(shares) - 1))

    candidates: list[CoveragePeak] = []
    for point in ordered:
        neighbors = [
            other.normalized_share
            for other in ordered
            if 2 < abs((other.day - point.day).days) <= baseline_radius_days
        ]
        if len(neighbors) < 4:
            continue
        local_neighbors = [
            other.normalized_share
            for other in ordered
            if 0 < abs((other.day - point.day).days) <= local_maximum_radius_days
        ]
        if local_neighbors and point.normalized_share < max(local_neighbors):
            continue
        if point.article_count < minimum_article_count:
            continue
        baseline = statistics.median(neighbors)
        if baseline <= 0:
            continue
        lift = point.normalized_share / baseline
        position = percentile(point.normalized_share)
        if lift < minimum_lift or position < minimum_percentile:
            continue
        # Log volume prevents a tiny specialist-news burst from outranking a
        # broad agenda-setting event while keeping normalized lift dominant.
        buzz_index = lift * (0.5 + position) * math.log10(point.article_count + 10)
        candidates.append(
            CoveragePeak(
                day=point.day,
                article_count=point.article_count,
                monitored_article_count=point.monitored_article_count,
                normalized_share=point.normalized_share,
                local_baseline_share=baseline,
                lift_over_local_baseline=lift,
                window_percentile=position,
                buzz_index=buzz_index,
            )
        )

    candidates.sort(
        key=lambda peak: (peak.buzz_index, peak.article_count, peak.day),
        reverse=True,
    )
    selected: list[CoveragePeak] = []
    for candidate in candidates:
        if any(
            abs((candidate.day - existing.day).days) < minimum_separation_days
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= maximum:
            break
    return selected


class GoogleNewsRSSLeadProvider:
    """Public RSS search adapter used as a second metadata-only discovery source."""

    provider_id = "google-news-rss"
    LOCALES = {
        "he": {"hl": "he", "gl": "IL", "ceid": "IL:he"},
        "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
        "ar": {"hl": "ar", "gl": "EG", "ceid": "EG:ar"},
    }

    def __init__(self, client: BoundedHTTPClient | Any):
        self.client = client

    def discover(self, query: DiscoveryQuery) -> list[EventLead]:
        leads: list[EventLead] = []
        per_language_limit = max(1, query.max_leads_per_provider // 3)
        for language in ("he", "en", "ar"):
            terms = [term.strip() for term in query.terms.get(language, ()) if term.strip()]
            if not terms:
                continue
            expression = " OR ".join(
                f'"{term.replace(chr(34), "")}"' if " " in term else term
                for term in terms[:8]
            )
            expression += (
                f" after:{query.window_start.isoformat()}"
                f" before:{(query.window_end + timedelta(days=1)).isoformat()}"
            )
            params = {"q": expression, **self.LOCALES[language]}
            result = self.client.get(GOOGLE_NEWS_RSS, params=params)
            feed = feedparser.parse(result.content)
            language_count = 0
            for entry in feed.entries:
                url = str(entry.get("link") or "").strip()
                title = str(entry.get("title") or "").strip()
                if not url or not title:
                    continue
                published_at = _rss_entry_datetime(entry)
                published = _parse_datetime(published_at)
                if published and not (
                    query.window_start <= published.date() <= query.window_end
                ):
                    continue
                source = entry.get("source") or {}
                source_name = str(source.get("title") or "Google News source")
                source_href = str(source.get("href") or "")
                source_domain = urlparse(source_href).hostname or source_name
                canonical = canonicalize_url(url)
                record_id = str(entry.get("id") or entry.get("guid") or canonical)
                leads.append(
                    EventLead(
                        lead_id=_sha256(
                            f"{self.provider_id}|{record_id}|{canonical}|{language}"
                        ),
                        provider_id=self.provider_id,
                        provider_record_id=record_id,
                        source_name=source_name,
                        source_domain=source_domain,
                        url=url,
                        canonical_url=canonical,
                        title=title,
                        summary=str(entry.get("summary") or ""),
                        published_at=published_at,
                        language=language,
                        topic_tag=query.topic_tag,
                    )
                )
                language_count += 1
                if language_count >= per_language_limit:
                    break
        return _unique_leads(leads)[: query.max_leads_per_provider]


def discover_from_providers(
    query: DiscoveryQuery, providers: Sequence[EventLeadProvider]
) -> DiscoveryReport:
    """Run providers sequentially to keep laptop memory and request volume bounded."""

    all_leads: list[EventLead] = []
    failures: list[DiscoveryFailure] = []
    for provider in providers:
        try:
            all_leads.extend(provider.discover(query))
        except Exception as error:  # provider failures are audit data, not global crashes
            failures.append(
                DiscoveryFailure(provider_id=provider.provider_id, message=str(error))
            )
    return DiscoveryReport(
        query_id=query.query_id,
        leads=tuple(_unique_leads(all_leads)),
        failures=tuple(failures),
    )


@dataclass(frozen=True)
class EventCluster:
    cluster_id: str
    topic_tag: str
    lead_ids: tuple[str, ...]
    provider_ids: tuple[str, ...]
    source_domains: tuple[str, ...]
    languages: tuple[str, ...]
    window_start: str
    window_end: str


def cluster_event_leads(
    leads: Sequence[EventLead],
    *,
    entity_aliases: Mapping[str, Sequence[str]],
    max_day_gap: int = 3,
    title_similarity_threshold: float = 0.28,
    min_shared_entities: int = 1,
) -> list[EventCluster]:
    """Deterministic clustering by topic, time, canonical entities and title overlap."""

    if max_day_gap < 0:
        raise ValueError("max_day_gap must be non-negative")
    if min_shared_entities < 1:
        raise ValueError("min_shared_entities must be positive")
    ordered = sorted(leads, key=lambda lead: (lead.published_at or "", lead.lead_id))
    parents = list(range(len(ordered)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    entities = [_canonical_entities(lead, entity_aliases) for lead in ordered]
    tokens = [_title_tokens(lead.title) for lead in ordered]
    dates = [_parse_datetime(lead.published_at) for lead in ordered]
    for left in range(len(ordered)):
        for right in range(left + 1, len(ordered)):
            if ordered[left].topic_tag != ordered[right].topic_tag:
                continue
            if dates[left] and dates[right]:
                if abs((dates[left].date() - dates[right].date()).days) > max_day_gap:
                    continue
            shared_entities = entities[left] & entities[right]
            similarity = _jaccard(tokens[left], tokens[right])
            if (
                len(shared_entities) >= min_shared_entities
                or similarity >= title_similarity_threshold
            ):
                union(left, right)

    groups: dict[int, list[EventLead]] = {}
    for index, lead in enumerate(ordered):
        groups.setdefault(find(index), []).append(lead)

    clusters: list[EventCluster] = []
    for members in groups.values():
        member_dates = [
            parsed.date().isoformat()
            for parsed in (_parse_datetime(item.published_at) for item in members)
            if parsed
        ]
        lead_ids = tuple(sorted(item.lead_id for item in members))
        clusters.append(
            EventCluster(
                cluster_id="CLUSTER-" + _sha256("|".join(lead_ids))[:16].upper(),
                topic_tag=members[0].topic_tag,
                lead_ids=lead_ids,
                provider_ids=tuple(sorted({item.provider_id for item in members})),
                source_domains=tuple(sorted({item.source_domain for item in members})),
                languages=tuple(sorted({item.language for item in members})),
                window_start=min(member_dates) if member_dates else "",
                window_end=max(member_dates) if member_dates else "",
            )
        )
    return sorted(clusters, key=lambda cluster: (cluster.window_start, cluster.cluster_id))


def build_temporal_entity_bundles(
    leads: Sequence[EventLead],
    *,
    entity_aliases: Mapping[str, Sequence[str]],
    generic_entities: frozenset[str] = frozenset({"israel", "gaza"}),
) -> list[EventCluster]:
    """Build coarse same-day actor bundles for bounded multilingual model clustering."""

    indexed: dict[tuple[str, str], list[tuple[date, EventLead]]] = {}
    for lead in leads:
        parsed = _parse_datetime(lead.published_at)
        if parsed is None:
            continue
        entities = _canonical_entities(lead, entity_aliases) - generic_entities
        for entity in entities:
            indexed.setdefault((lead.topic_tag, entity), []).append(
                (parsed.date(), lead)
            )

    bundles: list[EventCluster] = []
    seen_memberships: set[tuple[str, ...]] = set()
    for (topic_tag, entity), dated_members in indexed.items():
        for anchor_day in sorted({item[0] for item in dated_members}):
            members = [
                member
                for published_day, member in dated_members
                if abs((published_day - anchor_day).days) <= 1
            ]
            lead_ids = tuple(sorted({member.lead_id for member in members}))
            if lead_ids in seen_memberships:
                continue
            seen_memberships.add(lead_ids)
            providers = tuple(sorted({member.provider_id for member in members}))
            domains = tuple(sorted({member.source_domain for member in members}))
            languages = tuple(sorted({member.language for member in members}))
            if len(providers) < 2 or len(domains) < 2 or len(lead_ids) < 3:
                continue
            dates = sorted(
                _parse_datetime(member.published_at).date().isoformat()
                for member in members
                if _parse_datetime(member.published_at)
            )
            bundles.append(
                EventCluster(
                    cluster_id=(
                        "BUNDLE-"
                        + entity.upper().replace("_", "-")
                        + "-"
                        + _sha256("|".join(lead_ids))[:12].upper()
                    ),
                    topic_tag=topic_tag,
                    lead_ids=lead_ids,
                    provider_ids=providers,
                    source_domains=domains,
                    languages=languages,
                    window_start=dates[0],
                    window_end=dates[-1],
                )
            )
    return sorted(bundles, key=lambda bundle: (bundle.window_start, bundle.cluster_id))


@dataclass(frozen=True)
class ClusterAnnotation:
    title: str
    neutral_summary: str
    scope_relation: str
    responsibility_class: str
    evidence_clarity: str
    war_nexus_rationale: str
    material_impact_on_israel: bool
    observed_lead_ids: tuple[str, ...]
    proposed_keywords: Mapping[str, tuple[str, ...]]
    unresolved_hypotheses: tuple[str, ...] = ()
    turning_point_rationale: str = ""
    transformative_dimensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SampleComposition:
    total_cases: int
    direct_cases: int
    external_indirect_cases: int
    prewar_formative_cases: int


class CandidateEligibilityError(ValueError):
    pass


def build_case_candidate(
    cluster: EventCluster,
    leads: Sequence[EventLead],
    annotation: ClusterAnnotation,
    composition: SampleComposition,
    *,
    version: int = 1,
    methodology_version: str = "case-generator-contract-v1.0",
    selection_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lead_by_id = {lead.lead_id: lead for lead in leads}
    cluster_ids = set(cluster.lead_ids)
    observed_ids = set(annotation.observed_lead_ids)
    if not observed_ids or not observed_ids <= cluster_ids:
        raise CandidateEligibilityError(
            "annotation must cite only observed leads from its cluster"
        )
    selected = [lead_by_id[lead_id] for lead_id in sorted(observed_ids) if lead_id in lead_by_id]
    if len(selected) != len(observed_ids):
        raise CandidateEligibilityError("annotation cites a lead missing from discovery output")
    selected_domains = {lead.source_domain for lead in selected}
    selected_providers = {lead.provider_id for lead in selected}
    selected_languages = {lead.language for lead in selected}
    if len(selected_domains) < 2:
        raise CandidateEligibilityError("candidate needs at least two independent source domains")
    if len(selected_providers) < 2 and not (
        (len(selected_domains) >= 4 and len(selected_languages) >= 2)
        or len(selected_domains) >= 6
    ):
        raise CandidateEligibilityError(
            "candidate needs two providers, four domains across two languages, or six domains"
        )
    if annotation.scope_relation == "EXTERNAL_INDIRECT" and not annotation.material_impact_on_israel:
        raise CandidateEligibilityError(
            "external event has local coverage but no documented material impact on Israel"
        )
    if annotation.evidence_clarity == "UNRESOLVED":
        hypotheses = {item.strip() for item in annotation.unresolved_hypotheses if item.strip()}
        if len(hypotheses) < 2:
            raise CandidateEligibilityError(
                "unresolved case needs at least two evidence-backed plausible hypotheses"
            )
    if any(not annotation.proposed_keywords.get(language) for language in SUPPORTED_LANGUAGES):
        raise CandidateEligibilityError(
            "candidate needs proposed search terms in Hebrew, English and Arabic"
        )

    balance = _balance_impact(annotation.scope_relation, composition)
    occurred_after = bool(cluster.window_start and cluster.window_start >= "2023-10-07")
    evidence_leads = [
        {
            "provider_id": lead.provider_id,
            "source_name": lead.source_name,
            "url": lead.canonical_url,
            "title": lead.title,
            "published_at": lead.published_at or cluster.window_start,
            "language": lead.language,
        }
        for lead in selected
    ]
    coverage = Counter(lead.language for lead in cluster_ids_to_leads(cluster, leads))
    candidate: dict[str, Any] = {
        "candidate_id": "CAND-" + cluster.cluster_id.removeprefix("CLUSTER-"),
        "version": version,
        "candidate_hash": "",
        "title": annotation.title,
        "neutral_summary": annotation.neutral_summary,
        "window_start": cluster.window_start,
        "window_end": cluster.window_end,
        "scope_relation": annotation.scope_relation,
        "occurred_after_2023_10_07": occurred_after,
        "responsibility_class": annotation.responsibility_class,
        "evidence_clarity": annotation.evidence_clarity,
        "unresolved_hypotheses": list(annotation.unresolved_hypotheses),
        "war_nexus_rationale": annotation.war_nexus_rationale,
        "turning_point_rationale": annotation.turning_point_rationale,
        "transformative_dimensions": list(annotation.transformative_dimensions),
        "evidence_leads": evidence_leads,
        "proposed_keywords": {
            language: list(annotation.proposed_keywords[language])
            for language in ("he", "en", "ar")
        },
        "coverage_estimate": {
            language: int(coverage.get(language, 0)) for language in ("he", "en", "ar")
        },
        "sample_balance_impact": balance,
        "selection_evidence": dict(selection_evidence or {}),
        "state": "PROPOSED",
        "methodology_version": methodology_version,
    }
    candidate["candidate_hash"] = candidate_content_hash(candidate)
    return candidate


def cluster_ids_to_leads(
    cluster: EventCluster, leads: Sequence[EventLead]
) -> list[EventLead]:
    member_ids = set(cluster.lead_ids)
    return [lead for lead in leads if lead.lead_id in member_ids]


def _balance_impact(
    scope_relation: str, composition: SampleComposition
) -> dict[str, str]:
    if composition.total_cases >= 8:
        raise CandidateEligibilityError("the approved eight-case sample is already full")
    external = composition.external_indirect_cases + int(
        scope_relation == "EXTERNAL_INDIRECT"
    )
    prewar = composition.prewar_formative_cases + int(
        scope_relation == "PREWAR_FORMATIVE"
    )
    if external > 1:
        raise CandidateEligibilityError("sample already contains its external-indirect case")
    if prewar > 1:
        raise CandidateEligibilityError("sample already contains its formative pre-war case")
    direct = composition.direct_cases + int(scope_relation in DIRECT_SCOPE_RELATIONS)
    remaining = 8 - (composition.total_cases + 1)
    if direct + remaining < 6:
        raise CandidateEligibilityError(
            "candidate would make the six-direct-case minimum impossible"
        )
    return {
        "fills_slot": scope_relation.lower().replace("_", "-"),
        "rationale": (
            f"לאחר הכללה: {composition.total_cases + 1}/8 קייסים, "
            f"{direct} קייסי ליבה/השלכה ישירה, {external} חיצוני עקיף "
            f"ו-{prewar} מכונן טרום-מלחמה; עדיין ניתן לעמוד בחוזה המדגם."
        ),
    }


def _quote_gdelt(term: str) -> str:
    escaped = term.replace('"', "").strip()
    return f'"{escaped}"' if " " in escaped else escaped


def _unique_leads(leads: Iterable[EventLead]) -> list[EventLead]:
    by_url: dict[str, EventLead] = {}
    for lead in leads:
        by_url.setdefault(lead.canonical_url, lead)
    return sorted(
        by_url.values(), key=lambda lead: (lead.published_at or "", lead.lead_id)
    )


def _canonical_entities(
    lead: EventLead, aliases: Mapping[str, Sequence[str]]
) -> set[str]:
    text = _normalize(f"{lead.title} {lead.summary}")
    found: set[str] = set()
    for canonical, variants in aliases.items():
        if any(_normalize(variant) in text for variant in variants if _normalize(variant)):
            found.add(canonical)
    return found


def _title_tokens(title: str) -> set[str]:
    return {token for token in _normalize(title).split() if len(token) >= 3}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
