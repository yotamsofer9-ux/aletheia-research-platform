from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlencode, urlparse

import feedparser

from .case_generator_contracts import basket_content_hash, validate_basket_contract
from .case_discovery import GDELT_DOC_API
from .collector import (
    BoundedHTTPClient,
    Candidate,
    CollectionError,
    SourceCollector,
    canonicalize_url,
)
from .config import CaseConfig, NetworkLimits, SourceConfig
from .snapshots import LocalSnapshotStore


PHASES = ("INITIAL_REPORT", "EARLY_UPDATE", "LATER_INVESTIGATION")
PHASE_POLICY_VERSION = "event-relative-date-v2-d0-d1-d2-d3-d4plus"
GROUP_BY_LANGUAGE = {
    "he": "DOMESTIC_ISRAELI",
    "en": "WESTERN_INTERNATIONAL",
    "ar": "REGIONAL_ARABIC",
}
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
GOOGLE_NEWS_ARTICLE = "https://news.google.com/rss/articles/{article_id}"
GOOGLE_NEWS_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
GOOGLE_LOCALE_BY_LANGUAGE = {
    "he": ("he", "IL", "IL:he"),
    "en": ("en-US", "US", "US:en"),
    "ar": ("ar", "EG", "EG:ar"),
}
_GOOGLE_SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GOOGLE_TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_GOOGLE_REQUEST_CONTEXT = [
    [
        "X",
        "X",
        ["X", "X"],
        None,
        None,
        1,
        1,
        "US:en",
        None,
        1,
        None,
        None,
        None,
        None,
        None,
        0,
        1,
    ],
    "X",
    "X",
    1,
    [1, 1, 1],
    1,
    1,
    None,
    0,
    0,
    None,
    0,
]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class OutletSpec:
    source_id: str
    name: str
    domain: str
    slot_id: str
    group: str
    language: str
    editorial_profile: str
    primary_source_id: str

    @property
    def is_primary(self) -> bool:
        return self.source_id == self.primary_source_id


@dataclass(frozen=True)
class PublisherSlot:
    slot_id: str
    group: str
    language: str
    editorial_profile: str
    primary: OutletSpec
    substitutes: tuple[OutletSpec, ...]

    @property
    def outlets(self) -> tuple[OutletSpec, ...]:
        return (self.primary, *self.substitutes)


@dataclass(frozen=True)
class PublisherRoster:
    roster_version: str
    methodology_version: str
    slots: tuple[PublisherSlot, ...]


@dataclass(frozen=True)
class ArticleLead:
    source_id: str
    url: str
    title: str
    published_at: str
    provider_id: str
    sample_phase: str


@dataclass(frozen=True)
class SelectedArticle:
    slot: PublisherSlot
    outlet: OutletSpec
    lead: ArticleLead


@dataclass(frozen=True)
class StagedArticle:
    selected: SelectedArticle
    canonical_url: str
    title: str
    published_at: str
    snapshot_ref: str
    snapshot_hash: str
    content_hash: str
    fetch_method: str


@dataclass(frozen=True)
class BasketFailure:
    slot_id: str
    source_id: str
    sample_phase: str
    url: str
    message: str


@dataclass(frozen=True)
class BasketStagingReport:
    staged: tuple[StagedArticle, ...]
    failures: tuple[BasketFailure, ...]


class BasketLeadProvider(Protocol):
    provider_id: str

    def discover(
        self, manifest: Mapping[str, Any], outlet: OutletSpec
    ) -> list[ArticleLead]: ...


class BasketBuildError(ValueError):
    pass


def is_probable_article_url(url: str) -> bool:
    """Reject obvious listing/search pages before they occupy a basket cell."""

    parsed = urlparse(url)
    path = parsed.path.casefold().rstrip("/")
    if not path:
        return False
    listing_markers = (
        "/tagit/",
        "/tag/",
        "/tags/",
        "/search/",
        "/topic/",
        "/topics/",
        "/category/",
    )
    return not any(marker in f"{path}/" for marker in listing_markers)


def load_publisher_roster(path: str | Path) -> PublisherRoster:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    slots: list[PublisherSlot] = []
    for raw_slot in payload["slots"]:
        base = {
            "slot_id": raw_slot["slot_id"],
            "group": raw_slot["group"],
            "language": raw_slot["language"],
            "editorial_profile": raw_slot["editorial_profile"],
            "primary_source_id": raw_slot["primary"]["source_id"],
        }
        primary = OutletSpec(**base, **raw_slot["primary"])
        substitutes = tuple(
            OutletSpec(**base, **raw_outlet)
            for raw_outlet in raw_slot.get("substitutes", [])
        )
        slots.append(
            PublisherSlot(
                slot_id=raw_slot["slot_id"],
                group=raw_slot["group"],
                language=raw_slot["language"],
                editorial_profile=raw_slot["editorial_profile"],
                primary=primary,
                substitutes=substitutes,
            )
        )
    roster = PublisherRoster(
        roster_version=payload["roster_version"],
        methodology_version=payload["methodology_version"],
        slots=tuple(slots),
    )
    validate_publisher_roster(roster)
    return roster


def publisher_roster_path(root: str | Path, case_id: str) -> Path:
    """Return a case-pinned roster when one exists, otherwise the global roster."""
    base = Path(root)
    pinned = base / "config" / "case_rosters" / f"{case_id.lower()}.json"
    if pinned.exists():
        return pinned
    return base / "config" / "multicase_publisher_roster_v1.json"


def load_reviewed_leads(
    path: str | Path,
    manifest: Mapping[str, Any],
    roster: PublisherRoster,
) -> list[ArticleLead]:
    """Load public-search recovery leads after strict roster/domain validation."""
    source_map = {
        outlet.source_id: outlet
        for slot in roster.slots
        for outlet in slot.outlets
    }
    source = Path(path)
    if not source.exists():
        return []
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("case_id") != manifest.get("case_id"):
        raise BasketBuildError("reviewed leads target a different case")
    window_start = _parse_datetime(str(manifest["window_start"]))
    window_end = _parse_datetime(str(manifest["window_end"]))
    leads: list[ArticleLead] = []
    for raw in payload.get("leads") or []:
        source_id = str(raw["source_id"])
        outlet = source_map.get(source_id)
        if outlet is None:
            raise BasketBuildError(f"reviewed lead has unknown source {source_id}")
        url = str(raw["url"])
        host = urlparse(url).netloc.lower().split(":", 1)[0]
        if host != outlet.domain and not host.endswith("." + outlet.domain):
            raise BasketBuildError(
                f"reviewed lead domain does not match source {source_id}"
            )
        published_at = str(raw["published_at"])
        published = _parse_datetime(published_at)
        if not window_start.date() <= published.date() <= window_end.date():
            raise BasketBuildError("reviewed lead is outside the case window")
        leads.append(
            ArticleLead(
                source_id=source_id,
                url=url,
                title=str(raw["title"]),
                published_at=published_at,
                provider_id="reviewed-public-search",
                sample_phase=classify_sample_phase(
                    published_at, str(manifest["window_start"])
                ),
            )
        )
    return leads


def validate_publisher_roster(roster: PublisherRoster) -> None:
    if len(roster.slots) != 9:
        raise BasketBuildError("publisher roster must contain exactly nine slots")
    slot_ids = [slot.slot_id for slot in roster.slots]
    if len(set(slot_ids)) != len(slot_ids):
        raise BasketBuildError("publisher roster contains duplicate slots")
    counts = Counter(slot.language for slot in roster.slots)
    if counts != {"he": 3, "en": 3, "ar": 3}:
        raise BasketBuildError("publisher roster must contain three slots per language")
    for slot in roster.slots:
        if slot.group != GROUP_BY_LANGUAGE[slot.language]:
            raise BasketBuildError(f"slot {slot.slot_id} has incompatible group/language")
        # Keep the substitute pool deliberately small, while allowing the
        # case-specific discovery layer to switch providers when a public
        # article cannot be fetched or a complete three-phase run is absent.
        # All substitutes still inherit the slot's language and editorial
        # profile and the exact basket remains subject to Gate 2 approval.
        if len(slot.substitutes) > 3:
            raise BasketBuildError(f"slot {slot.slot_id} declares more than three substitutes")
        outlet_ids = [outlet.source_id for outlet in slot.outlets]
        outlet_domains = [outlet.domain for outlet in slot.outlets]
        if len(set(outlet_ids)) != len(outlet_ids):
            raise BasketBuildError(f"slot {slot.slot_id} contains duplicate source ids")
        if len(set(outlet_domains)) != len(outlet_domains):
            raise BasketBuildError(f"slot {slot.slot_id} contains duplicate domains")
        for outlet in slot.outlets:
            if (
                outlet.slot_id != slot.slot_id
                or outlet.group != slot.group
                or outlet.language != slot.language
                or outlet.editorial_profile != slot.editorial_profile
            ):
                raise BasketBuildError(
                    f"slot {slot.slot_id} has a substitute outside its approved profile"
                )


def classify_sample_phase(published_at: str, window_start: str) -> str:
    # The approved phase policy is calendar-date based.  Preserve the date as
    # published by the outlet instead of converting near-midnight local times
    # to the preceding UTC date.
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    start = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    day_delta = (published.date() - start.date()).days
    if day_delta <= 1:
        return "INITIAL_REPORT"
    if day_delta <= 3:
        return "EARLY_UPDATE"
    return "LATER_INVESTIGATION"


def _language_terms(
    manifest: Mapping[str, Any], language: str, *, limit: int = 3
) -> list[str]:
    terms: list[str] = []
    for raw in manifest.get("keywords", {}).get(language, []):
        term = str(raw).strip()
        if term and term.casefold() not in {value.casefold() for value in terms}:
            terms.append(term)
    # Candidate manifests order terms from event-specific names to broader context.
    # Callers choose how much of that prefix they need: discovery may stay broad,
    # while the post-fetch staging gate deliberately uses only the two strongest
    # event expressions so generic actor/location terms cannot admit a different
    # event involving the same parties.
    return terms[:limit]


def _quote_search_term(term: str) -> str:
    clean = term.replace('"', "").strip()
    return f'"{clean}"' if " " in clean else clean


def _parse_feed_date(entry: Any) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _strip_source_suffix(title: str, entry: Any) -> str:
    source = entry.get("source") or {}
    source_title = str(source.get("title") or "").strip()
    suffix = f" - {source_title}"
    if source_title and title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def _host_matches(url: str, expected_domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    expected = expected_domain.lower()
    return host == expected or host.endswith("." + expected)


def _google_article_id(url: str) -> str | None:
    parsed = urlparse(url)
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.hostname == "news.google.com"
        and len(parts) >= 2
        and parts[-2] in {"articles", "read"}
    ):
        return parts[-1] or None
    return None


def _decode_google_news_urls(
    client: BoundedHTTPClient | Any,
    urls: Sequence[str],
    *,
    locale: tuple[str, str, str],
) -> list[str | None]:
    """Resolve Google News RSS links in one bounded batch request."""
    hl, gl, ceid = locale
    results: list[str | None] = [None] * len(urls)
    pending: list[tuple[int, str, str, str]] = []
    for index, url in enumerate(urls):
        article_id = _google_article_id(url)
        if not article_id:
            continue
        page_url = GOOGLE_NEWS_ARTICLE.format(article_id=article_id)
        page_url += "?" + urlencode({"hl": hl, "gl": gl, "ceid": ceid})
        page = client.get(page_url)
        text = page.content.decode("utf-8", errors="replace")
        signature = _GOOGLE_SIGNATURE_RE.search(text)
        timestamp = _GOOGLE_TIMESTAMP_RE.search(text)
        if signature and timestamp:
            pending.append(
                (index, article_id, timestamp.group(1), signature.group(1))
            )
    if not pending:
        return results

    envelopes = []
    for index, article_id, timestamp, signature in pending:
        inner = [
            "garturlreq",
            _GOOGLE_REQUEST_CONTEXT,
            article_id,
            int(timestamp) if timestamp.isdigit() else timestamp,
            signature,
        ]
        envelopes.append(
            ["Fbv4je", json.dumps(inner, separators=(",", ":")), None, str(index)]
        )
    response = client.post_form(
        GOOGLE_NEWS_BATCH,
        {"f.req": json.dumps([envelopes], separators=(",", ":"))},
    )
    text = response.content.decode("utf-8", errors="replace")
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    body = body.lstrip()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1] if "\n" in body else body[4:]
    try:
        rows = json.loads(body.lstrip())
    except json.JSONDecodeError as error:
        raise CollectionError("Google News URL resolver returned invalid JSON") from error
    ordered_urls: list[str] = []
    urls_by_request_id: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        payload = row[2]
        try:
            if isinstance(payload, str):
                payload = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list) or len(payload) < 2 or payload[0] != "garturlres":
            continue
        decoded_url = str(payload[1])
        ordered_urls.append(decoded_url)
        for cell in reversed(row[3:]):
            if cell is not None:
                urls_by_request_id[str(cell)] = decoded_url
                break
    for offset, (index, *_rest) in enumerate(pending):
        decoded_url = urls_by_request_id.get(str(index))
        if decoded_url is None and not urls_by_request_id and offset < len(ordered_urls):
            decoded_url = ordered_urls[offset]
        results[index] = decoded_url
    return results


class GDELTBasketLeadProvider:
    """Metadata-only domain search; HTML is fetched later by SourceCollector."""

    provider_id = "gdelt-doc-2"

    def __init__(self, client: BoundedHTTPClient | Any, *, max_records: int = 75):
        if not 1 <= max_records <= 250:
            raise ValueError("max_records must be between 1 and 250")
        self.client = client
        self.max_records = max_records

    def discover(
        self, manifest: Mapping[str, Any], outlet: OutletSpec
    ) -> list[ArticleLead]:
        terms = [
            term.strip()
            for term in manifest["keywords"].get(outlet.language, [])
            if term.strip()
        ]
        expression = "(" + " OR ".join(
            f'"{term.replace(chr(34), "")}"' if " " in term else term
            for term in terms[:18]
        ) + f") domain:{outlet.domain}"
        start = _parse_datetime(manifest["window_start"])
        end = _parse_datetime(manifest["window_end"])
        params = {
            "query": expression,
            "mode": "artlist",
            "format": "json",
            "sort": "dateasc",
            "maxrecords": str(self.max_records),
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }
        result = self.client.get(GDELT_DOC_API, params=params)
        try:
            payload = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CollectionError("GDELT basket search returned invalid JSON") from error
        leads: list[ArticleLead] = []
        seen_urls: set[str] = set()
        for raw in payload.get("articles") or []:
            url = str(raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            date_text = str(raw.get("seendate") or "").strip()
            published = _parse_gdelt_date(date_text)
            if not url or not title or not published:
                continue
            host = (urlparse(url).hostname or "").lower()
            expected = outlet.domain.lower()
            if host != expected and not host.endswith("." + expected):
                continue
            canonical = canonicalize_url(url)
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            published_at = published.isoformat().replace("+00:00", "Z")
            leads.append(
                ArticleLead(
                    source_id=outlet.source_id,
                    url=url,
                    title=title,
                    published_at=published_at,
                    provider_id=self.provider_id,
                    sample_phase=classify_sample_phase(
                        published_at, manifest["window_start"]
                    ),
                )
            )
        return leads


class GoogleNewsBasketLeadProvider:
    """Public RSS discovery with bounded resolution to the publisher URL."""

    provider_id = "google-news-rss"

    def __init__(
        self,
        client: BoundedHTTPClient | Any,
        *,
        max_candidates_per_phase: int = 4,
    ):
        if not 1 <= max_candidates_per_phase <= 10:
            raise ValueError("max_candidates_per_phase must be between 1 and 10")
        self.client = client
        self.max_candidates_per_phase = max_candidates_per_phase

    def discover(
        self, manifest: Mapping[str, Any], outlet: OutletSpec
    ) -> list[ArticleLead]:
        terms = _language_terms(manifest, outlet.language)
        if not terms:
            return []
        start = _parse_datetime(manifest["window_start"])
        end = _parse_datetime(manifest["window_end"])
        after = (start.date() - timedelta(days=1)).isoformat()
        before = (end.date() + timedelta(days=1)).isoformat()
        expression = "(" + " OR ".join(_quote_search_term(term) for term in terms) + ")"
        expression += f" site:{outlet.domain} after:{after} before:{before}"
        hl, gl, ceid = GOOGLE_LOCALE_BY_LANGUAGE[outlet.language]
        result = self.client.get(
            GOOGLE_NEWS_RSS,
            params={"q": expression, "hl": hl, "gl": gl, "ceid": ceid},
        )
        feed = feedparser.parse(result.content)

        candidates_by_phase: dict[str, list[tuple[str, str, datetime]]] = defaultdict(list)
        for entry in feed.entries:
            link = str(entry.get("link") or "").strip()
            title = str(entry.get("title") or "").strip()
            published = _parse_feed_date(entry)
            if not link or not title or published is None:
                continue
            if not start.date() <= published.date() <= end.date():
                continue
            published_at = published.isoformat().replace("+00:00", "Z")
            phase = classify_sample_phase(published_at, manifest["window_start"])
            values = candidates_by_phase[phase]
            if len(values) < self.max_candidates_per_phase:
                values.append((link, _strip_source_suffix(title, entry), published))

        candidates = [
            item
            for phase in PHASES
            for item in candidates_by_phase.get(phase, [])
        ]
        if not candidates:
            return []
        decoded_urls = _decode_google_news_urls(
            self.client,
            [item[0] for item in candidates],
            locale=(hl, gl, ceid),
        )
        leads: list[ArticleLead] = []
        seen_urls: set[str] = set()
        for (_, title, published), decoded_url in zip(candidates, decoded_urls):
            if not decoded_url or not _host_matches(decoded_url, outlet.domain):
                continue
            canonical = canonicalize_url(decoded_url)
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            published_at = published.isoformat().replace("+00:00", "Z")
            leads.append(
                ArticleLead(
                    source_id=outlet.source_id,
                    url=decoded_url,
                    title=title,
                    published_at=published_at,
                    provider_id=self.provider_id,
                    sample_phase=classify_sample_phase(
                        published_at, manifest["window_start"]
                    ),
                )
            )
        if not leads:
            raise CollectionError(
                "Google News returned candidates but no publisher URL could be verified"
            )
        return leads


def discover_basket_leads(
    manifest: Mapping[str, Any],
    roster: PublisherRoster,
    providers: Sequence[BasketLeadProvider],
    *,
    source_ids: set[str] | None = None,
) -> tuple[list[ArticleLead], list[BasketFailure]]:
    leads: list[ArticleLead] = []
    failures: list[BasketFailure] = []
    for slot in roster.slots:
        for outlet in slot.outlets:
            if source_ids is not None and outlet.source_id not in source_ids:
                continue
            for provider in providers:
                try:
                    leads.extend(provider.discover(manifest, outlet))
                except Exception as error:
                    failures.append(
                        BasketFailure(
                            slot_id=slot.slot_id,
                            source_id=outlet.source_id,
                            sample_phase="DISCOVERY",
                            url="",
                            message=f"{provider.provider_id}: {error}",
                        )
                    )
    unique: dict[tuple[str, str], ArticleLead] = {}
    for lead in leads:
        unique.setdefault((lead.source_id, canonicalize_url(lead.url)), lead)
    return list(unique.values()), failures


def missing_roster_source_ids(
    leads: Sequence[ArticleLead], roster: PublisherRoster
) -> set[str]:
    """Return outlets belonging to slots that still miss at least one phase."""
    source_to_slot = {
        outlet.source_id: slot.slot_id
        for slot in roster.slots
        for outlet in slot.outlets
    }
    covered = {
        (source_to_slot[lead.source_id], lead.sample_phase)
        for lead in leads
        if lead.source_id in source_to_slot
    }
    missing_slots = {
        slot.slot_id
        for slot in roster.slots
        if any((slot.slot_id, phase) not in covered for phase in PHASES)
    }
    return {
        outlet.source_id
        for slot in roster.slots
        if slot.slot_id in missing_slots
        for outlet in slot.outlets
    }


def select_roster_articles(
    leads: Sequence[ArticleLead], roster: PublisherRoster
) -> list[SelectedArticle]:
    by_source_phase: dict[tuple[str, str], list[ArticleLead]] = defaultdict(list)
    for lead in leads:
        if lead.sample_phase not in PHASES:
            raise BasketBuildError(f"unknown sample phase: {lead.sample_phase}")
        by_source_phase[(lead.source_id, lead.sample_phase)].append(lead)
    for values in by_source_phase.values():
        values.sort(key=lambda lead: (lead.published_at, canonicalize_url(lead.url)))

    selected: list[SelectedArticle] = []
    used_urls: set[str] = set()
    for slot in roster.slots:
        for phase in PHASES:
            choice: tuple[OutletSpec, ArticleLead] | None = None
            for outlet in slot.outlets:
                for lead in by_source_phase.get((outlet.source_id, phase), []):
                    canonical = canonicalize_url(lead.url)
                    if canonical not in used_urls:
                        choice = (outlet, lead)
                        break
                if choice:
                    break
            if choice:
                outlet, lead = choice
                used_urls.add(canonicalize_url(lead.url))
                selected.append(SelectedArticle(slot=slot, outlet=outlet, lead=lead))
    return selected


def stage_selected_articles(
    manifest: Mapping[str, Any],
    selected: Sequence[SelectedArticle],
    staging_root: str | Path,
    *,
    client: BoundedHTTPClient | Any | None = None,
) -> BasketStagingReport:
    flat_keywords = tuple(
        term
        for language in ("he", "en", "ar")
        for term in _language_terms(manifest, language, limit=2)
    )
    source_configs = {
        item.outlet.source_id: SourceConfig(
            source_id=item.outlet.source_id,
            name=item.outlet.name,
            group=item.outlet.group,
            language=item.outlet.language,
            discovery="urls",
            publisher_slot=item.slot.slot_id,
            editorial_profile=item.slot.editorial_profile,
            substitute_for=(
                None if item.outlet.is_primary else item.slot.primary.source_id
            ),
            domain=item.outlet.domain,
            archive_fallback=True,
            max_documents=3,
        )
        for item in selected
    }
    config = CaseConfig(
        case_id=manifest["case_id"],
        title=manifest["title"],
        window_start=_parse_datetime(manifest["window_start"]),
        window_end=_parse_datetime(manifest["window_end"]),
        keywords=flat_keywords,
        sources=tuple(source_configs.values()),
        max_documents=27,
        max_documents_per_source=3,
        max_discovery_candidates_per_source=75,
        network=NetworkLimits(
            timeout_seconds=8,
            max_response_bytes=5_000_000,
            requests_per_second_per_host=0.5,
            retries=0,
            min_text_chars=250,
            max_text_chars=30_000,
        ),
    )
    snapshot_store = LocalSnapshotStore(
        Path(staging_root) / manifest["case_id"] / "snapshots"
    )
    collector = SourceCollector(config, client=client, snapshot_store=snapshot_store)
    reviewed_keywords = tuple(
        term
        for language in ("he", "en", "ar")
        # Reviewed recovery leads may legitimately focus on a named hostage or
        # another event-specific entity rather than repeat the case label. The
        # later Vertex topic audit remains the substantive relevance gate.
        for term in _language_terms(manifest, language, limit=7)
    )
    reviewed_collector = SourceCollector(
        replace(config, keywords=reviewed_keywords),
        client=collector.client,
        snapshot_store=snapshot_store,
    )
    staged: list[StagedArticle] = []
    failures: list[BasketFailure] = []
    content_hashes: set[str] = set()
    for item in selected:
        if not is_probable_article_url(item.lead.url):
            failures.append(
                BasketFailure(
                    slot_id=item.slot.slot_id,
                    source_id=item.outlet.source_id,
                    sample_phase=item.lead.sample_phase,
                    url=item.lead.url,
                    message="URL is a listing, tag, search, topic, or category page",
                )
            )
            continue
        candidate = Candidate(
            source_id=item.outlet.source_id,
            url=item.lead.url,
            title=item.lead.title,
            published_at=item.lead.published_at,
            sample_phase=item.lead.sample_phase,
        )
        try:
            active_collector = (
                reviewed_collector
                if item.lead.provider_id == "reviewed-public-search"
                else collector
            )
            document = active_collector.fetch(
                source_configs[item.outlet.source_id], candidate
            )
            if not document.snapshot_ref or not document.snapshot_hash:
                raise CollectionError("staging fetch did not create an HTML snapshot")
            if document.content_hash in content_hashes:
                raise CollectionError("duplicate article content in proposed basket")
            content_hashes.add(document.content_hash)
            staged.append(
                StagedArticle(
                    selected=item,
                    canonical_url=document.canonical_url,
                    title=document.title,
                    published_at=document.published_at or item.lead.published_at,
                    snapshot_ref=document.snapshot_ref,
                    snapshot_hash=document.snapshot_hash,
                    content_hash=document.content_hash,
                    fetch_method=document.fetch_method,
                )
            )
        except Exception as error:
            failures.append(
                BasketFailure(
                    slot_id=item.slot.slot_id,
                    source_id=item.outlet.source_id,
                    sample_phase=item.lead.sample_phase,
                    url=item.lead.url,
                    message=str(error),
                )
            )
    return BasketStagingReport(staged=tuple(staged), failures=tuple(failures))


def build_basket_payload(
    manifest: Mapping[str, Any],
    staged: Sequence[StagedArticle],
    *,
    version: int,
    created_at: str,
    change_reason: str,
) -> dict[str, Any]:
    articles: list[dict[str, Any]] = []
    for item in sorted(
        staged,
        key=lambda value: (
            value.selected.slot.language,
            value.selected.slot.slot_id,
            PHASES.index(value.selected.lead.sample_phase),
        ),
    ):
        selected = item.selected
        is_substitute = not selected.outlet.is_primary
        replacement_kind = "SIMILAR_OUTLET" if is_substitute else "NONE"
        article_id = (
            f"ARTICLE-{manifest['case_id']}-"
            + item.snapshot_hash[:16].upper()
        )
        articles.append(
            {
                "article_id": article_id,
                "publisher_slot": selected.slot.slot_id,
                "source_id": selected.outlet.source_id,
                "source_name": selected.outlet.name,
                "source_group": selected.outlet.group,
                "language": selected.outlet.language,
                "editorial_profile": selected.outlet.editorial_profile,
                "sample_phase": selected.lead.sample_phase,
                "url": selected.lead.url,
                "canonical_url": item.canonical_url,
                "title": item.title,
                "published_at": item.published_at,
                "snapshot_ref": item.snapshot_ref,
                "snapshot_hash": item.snapshot_hash,
                "replacement_kind": replacement_kind,
                "substitute_for_source_id": (
                    selected.slot.primary.source_id if is_substitute else None
                ),
                "requires_human_approval": bool(is_substitute),
            }
        )
    payload: dict[str, Any] = {
        "basket_id": f"BASKET-{manifest['case_id']}-V{version}",
        "case_id": manifest["case_id"],
        "version": version,
        "basket_hash": "",
        "status": "AWAITING_APPROVAL",
        "created_at": created_at,
        "change_reason": change_reason,
        "articles": articles,
    }
    payload["basket_hash"] = basket_content_hash(payload)
    validate_basket_contract(payload, manifest)
    return payload


def _parse_gdelt_date(value: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None
