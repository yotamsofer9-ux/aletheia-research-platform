from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import CaseConfig, NetworkLimits, SeedURL, SourceConfig
from .storage import StoredDocument
from .snapshots import SnapshotStore


USER_AGENT = (
    "Mozilla/5.0 (compatible; AletheiaAcademicResearch/0.1; "
    "read-only student research)"
)
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH_PREFIX = "https://web.archive.org/web"
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
SKIPPED_SUFFIXES = {
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".xml",
}


class CollectionError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class HTTPResult:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes


@dataclass(frozen=True)
class Candidate:
    source_id: str
    url: str
    title: str = ""
    published_at: str | None = None
    snapshot_url: str | None = None
    archived_at: str | None = None
    sample_phase: str | None = None


@dataclass(frozen=True)
class ExtractedArticle:
    title: str
    text: str
    published_at: str | None


class BoundedHTTPClient:
    """Small HTTP client with hard response, retry, timeout and rate limits."""

    def __init__(self, limits: NetworkLimits, session: requests.Session | None = None):
        self.limits = limits
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5"})
        self._last_request_by_host: dict[str, float] = {}

    def _rate_limit(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        interval = 1.0 / self.limits.requests_per_second_per_host
        previous = self._last_request_by_host.get(host)
        if previous is not None:
            remaining = interval - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_by_host[host] = time.monotonic()

    def get(self, url: str, params: Any = None) -> HTTPResult:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CollectionError("Refusing non-http(s) URL")

        last_error: Exception | None = None
        for attempt in range(self.limits.retries + 1):
            self._rate_limit(url)
            try:
                with self.session.get(
                    url,
                    params=params,
                    timeout=self.limits.timeout_seconds,
                    stream=True,
                    allow_redirects=True,
                ) as response:
                    if response.status_code >= 400:
                        raise CollectionError(
                            f"HTTP {response.status_code} for {response.url}",
                            response.status_code,
                        )
                    declared_size = response.headers.get("Content-Length")
                    if declared_size and int(declared_size) > self.limits.max_response_bytes:
                        raise CollectionError("Response exceeds configured byte limit")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.limits.max_response_bytes:
                            raise CollectionError("Response exceeds configured byte limit")
                        chunks.append(chunk)
                    return HTTPResult(
                        url=response.url,
                        status_code=response.status_code,
                        headers={key: value for key, value in response.headers.items()},
                        content=b"".join(chunks),
                    )
            except CollectionError as exc:
                last_error = exc
                if exc.http_status is not None and exc.http_status < 500 and exc.http_status != 429:
                    break
            except requests.RequestException as exc:
                last_error = exc
            if attempt < self.limits.retries:
                time.sleep(min(2**attempt, 4))
        if isinstance(last_error, CollectionError):
            raise last_error
        raise CollectionError(f"Request failed: {last_error}")

    def post_form(self, url: str, data: Mapping[str, Any]) -> HTTPResult:
        """POST a small form while preserving the same network guardrails as GET."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CollectionError("Refusing non-http(s) URL")

        last_error: Exception | None = None
        for attempt in range(self.limits.retries + 1):
            self._rate_limit(url)
            try:
                with self.session.post(
                    url,
                    data=data,
                    timeout=self.limits.timeout_seconds,
                    stream=True,
                    allow_redirects=True,
                ) as response:
                    if response.status_code >= 400:
                        raise CollectionError(
                            f"HTTP {response.status_code} for {response.url}",
                            response.status_code,
                        )
                    declared_size = response.headers.get("Content-Length")
                    if declared_size and int(declared_size) > self.limits.max_response_bytes:
                        raise CollectionError("Response exceeds configured byte limit")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.limits.max_response_bytes:
                            raise CollectionError("Response exceeds configured byte limit")
                        chunks.append(chunk)
                    return HTTPResult(
                        url=response.url,
                        status_code=response.status_code,
                        headers={key: value for key, value in response.headers.items()},
                        content=b"".join(chunks),
                    )
            except CollectionError as exc:
                last_error = exc
                if exc.http_status is not None and exc.http_status < 500 and exc.http_status != 429:
                    break
            except requests.RequestException as exc:
                last_error = exc
            if attempt < self.limits.retries:
                time.sleep(min(2**attempt, 4))
        if isinstance(last_error, CollectionError):
            raise last_error
        raise CollectionError(f"Request failed: {last_error}")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    filtered_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        filtered_query.append((key, value))
    query = urlencode(sorted(filtered_query))
    return urlunparse((scheme, host, path, "", query, ""))


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u00a0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in normalized.splitlines()]
    return "\n".join(line for line in lines if line)


def _first_meta_content(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") or node.get("datetime")
            if value:
                return str(value).strip()
    return None


def extract_article(html: bytes, fallback_title: str = "") -> ExtractedArticle:
    soup = BeautifulSoup(html, "html.parser")
    structured_articles: list[Mapping[str, Any]] = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                structured_articles.extend(
                    item for item in graph if isinstance(item, Mapping)
                )
            structured_articles.append(candidate)
    structured = next(
        (
            item
            for item in structured_articles
            if isinstance(item.get("articleBody"), str)
            and str(item.get("articleBody")).strip()
        ),
        None,
    )
    title = _first_meta_content(
        soup,
        (
            'meta[property="og:title"]',
            'meta[name="twitter:title"]',
        ),
    )
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title and structured:
        title = str(structured.get("headline") or "")
    title = normalize_text(title or fallback_title)

    published_at = _first_meta_content(
        soup,
        (
            'meta[property="article:published_time"]',
            'meta[name="date"]',
            'meta[name="pubdate"]',
            "time[datetime]",
        ),
    )
    if not published_at and structured:
        published_at = str(structured.get("datePublished") or "") or None
    for selector in ("script", "style", "noscript", "svg", "nav", "footer", "form", "aside"):
        for node in soup.select(selector):
            node.decompose()

    article = soup.find("article")
    paragraphs = (article or soup).find_all("p")
    text = normalize_text("\n".join(node.get_text(" ", strip=True) for node in paragraphs))
    if not text and structured:
        body = str(structured.get("articleBody") or "")
        text = normalize_text(BeautifulSoup(body, "html.parser").get_text("\n"))
    return ExtractedArticle(title=title, text=text, published_at=published_at)


def _entry_datetime(entry: Any) -> str | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    for key in ("published_parsed", "updated_parsed"):
        raw = entry.get(key)
        if raw:
            return datetime(*raw[:6], tzinfo=timezone.utc).isoformat()
    return None


def _is_within_window(value: str | None, config: CaseConfig) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    except ValueError:
        return True
    return config.window_start <= parsed <= config.window_end


def _matches_keywords(value: str, keywords: tuple[str, ...]) -> bool:
    folded = value.casefold()
    # Publisher prose may split or join the same proper name (for example the
    # Arabic spellings "نصر الله" and "نصرالله").  A compact Unicode form makes
    # that typography irrelevant without broadening the semantic match.
    compact = re.sub(r"[\W_]+", "", folded, flags=re.UNICODE)
    for keyword in keywords:
        phrase = keyword.casefold().strip()
        if not phrase:
            continue
        compact_phrase = re.sub(r"[\W_]+", "", phrase, flags=re.UNICODE)
        if phrase in folded or (compact_phrase and compact_phrase in compact):
            return True
        # Case-generator keywords are often compact search phrases (for
        # example "Iran attack Israel").  Requiring that exact word order in
        # publisher prose rejects relevant articles.  Two informative tokens
        # from the same phrase are still a deliberately narrow relevance gate;
        # dates and one-word generic terms do not qualify through this path.
        tokens = [
            token
            for token in re.findall(r"[^\W\d_]+", phrase, flags=re.UNICODE)
            if len(token) >= 3
        ]
        if len(tokens) >= 2 and sum(token in folded for token in set(tokens)) >= 2:
            return True
    return False


def _looks_like_access_interstitial(article: ExtractedArticle) -> bool:
    value = f"{article.title}\n{article.text}".casefold()
    markers = (
        "radware block page",
        "please solve this captcha",
        "verify you are human",
        "access denied",
    )
    return any(marker in value for marker in markers)


def _meaningful_title_tokens(value: str) -> set[str]:
    stopwords = {
        "ישראל",
        "היום",
        "חדשות",
        "דיווח",
        "דיווחים",
        "לאחר",
        "של",
        "על",
        "את",
        "עם",
        "the",
        "and",
        "for",
        "from",
        "after",
    }
    return {
        token.casefold()
        for token in re.findall(r"[^\W\d_]+", value, flags=re.UNICODE)
        if len(token) >= 3 and token.casefold() not in stopwords
    }


def title_body_coherent(article: ExtractedArticle) -> bool:
    """Conservative guard against a headline paired with promo-card text."""
    title_tokens = _meaningful_title_tokens(article.title)
    if not title_tokens:
        return True
    body = article.text.casefold()
    return any(token in body for token in title_tokens)


def amp_recovery_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "israelhayom.co.il":
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["amp"] = "1"
    return urlunparse(parsed._replace(query=urlencode(query)))


class SourceCollector:
    def __init__(
        self,
        config: CaseConfig,
        client: BoundedHTTPClient | Any | None = None,
        snapshot_store: SnapshotStore | None = None,
    ):
        self.config = config
        self.client = client or BoundedHTTPClient(config.network)
        self.snapshot_store = snapshot_store

    def discover(self, source: SourceConfig) -> list[Candidate]:
        if source.discovery == "urls":
            return [self._from_seed(source, seed) for seed in source.article_urls]
        if source.discovery == "rss":
            return self._discover_rss(source)
        if source.discovery == "wayback":
            return self._discover_wayback(source)
        raise CollectionError(f"Unsupported discovery mode: {source.discovery}")

    @staticmethod
    def _from_seed(source: SourceConfig, seed: SeedURL) -> Candidate:
        return Candidate(
            source_id=source.source_id,
            url=seed.url,
            title=seed.title,
            published_at=seed.published_at,
            sample_phase=seed.sample_phase,
        )

    def _discover_rss(self, source: SourceConfig) -> list[Candidate]:
        result = self.client.get(source.feed_url)
        feed = feedparser.parse(result.content)
        candidates: list[Candidate] = []
        for entry in feed.entries:
            url = str(entry.get("link", "")).strip()
            if not url:
                continue
            title = normalize_text(str(entry.get("title", "")))
            summary = normalize_text(str(entry.get("summary", "")))
            published_at = _entry_datetime(entry)
            if not _is_within_window(published_at, self.config):
                continue
            if not _matches_keywords(f"{title}\n{summary}\n{url}", self.config.keywords):
                continue
            candidates.append(
                Candidate(
                    source_id=source.source_id,
                    url=url,
                    title=title,
                    published_at=published_at,
                )
            )
        return self._unique_candidates(candidates)

    def _discover_wayback(self, source: SourceConfig) -> list[Candidate]:
        params = [
            ("url", f"{source.domain}/*"),
            ("from", self.config.window_start.strftime("%Y%m%d")),
            ("to", self.config.window_end.strftime("%Y%m%d")),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,mimetype,digest"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "urlkey"),
            ("limit", str(self.config.max_discovery_candidates_per_source)),
        ]
        result = self.client.get(WAYBACK_CDX_URL, params=params)
        try:
            rows = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectionError("Wayback returned invalid JSON") from exc
        if not rows or len(rows) < 2:
            return []
        headers = rows[0]
        candidates: list[Candidate] = []
        for raw_row in rows[1:]:
            row = dict(zip(headers, raw_row))
            original = str(row.get("original", ""))
            if not original or self._skip_url(original):
                continue
            if not _matches_keywords(original, self.config.keywords):
                continue
            timestamp = str(row.get("timestamp", ""))
            if len(timestamp) < 14:
                continue
            archived_at = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            ).isoformat()
            snapshot_url = f"{WAYBACK_FETCH_PREFIX}/{timestamp}id_/{original}"
            candidates.append(
                Candidate(
                    source_id=source.source_id,
                    url=original,
                    snapshot_url=snapshot_url,
                    archived_at=archived_at,
                )
            )
        return self._unique_candidates(candidates)

    @staticmethod
    def _skip_url(url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(suffix) for suffix in SKIPPED_SUFFIXES)

    @staticmethod
    def _unique_candidates(candidates: list[Candidate]) -> list[Candidate]:
        unique: dict[str, Candidate] = {}
        for candidate in candidates:
            unique.setdefault(canonicalize_url(candidate.url), candidate)
        return list(unique.values())

    def _resolve_wayback_snapshot(self, candidate: Candidate) -> Candidate | None:
        params = [
            ("url", candidate.url),
            ("from", self.config.window_start.strftime("%Y%m%d")),
            ("to", self.config.window_end.strftime("%Y%m%d")),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,mimetype,digest"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "digest"),
            ("limit", "1"),
        ]
        result = self.client.get(WAYBACK_CDX_URL, params=params)
        try:
            rows = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not rows or len(rows) < 2:
            return None
        row = dict(zip(rows[0], rows[1]))
        timestamp = str(row.get("timestamp", ""))
        if len(timestamp) < 14:
            return None
        archived_at = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ).isoformat()
        return Candidate(
            source_id=candidate.source_id,
            url=candidate.url,
            title=candidate.title,
            published_at=candidate.published_at,
            snapshot_url=f"{WAYBACK_FETCH_PREFIX}/{timestamp}id_/{candidate.url}",
            archived_at=archived_at,
            sample_phase=candidate.sample_phase,
        )

    def _guessed_wayback_snapshot(self, candidate: Candidate) -> Candidate:
        target = self.config.window_end
        if candidate.published_at:
            try:
                target = datetime.fromisoformat(
                    candidate.published_at.replace("Z", "+00:00")
                ) + timedelta(days=1)
            except ValueError:
                pass
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        target = target.astimezone(timezone.utc)
        timestamp = target.strftime("%Y%m%d%H%M%S")
        return Candidate(
            source_id=candidate.source_id,
            url=candidate.url,
            title=candidate.title,
            published_at=candidate.published_at,
            snapshot_url=f"{WAYBACK_FETCH_PREFIX}/{timestamp}id_/{candidate.url}",
            archived_at=target.isoformat(),
            sample_phase=candidate.sample_phase,
        )

    @staticmethod
    def _actual_archive_time(result_url: str, fallback: str | None) -> str | None:
        match = re.search(r"/web/(\d{14})", result_url)
        if not match:
            return fallback
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            ).isoformat()
        except ValueError:
            return fallback

    def fetch(self, source: SourceConfig, candidate: Candidate) -> StoredDocument:
        attempts: list[tuple[str, Candidate]] = []
        if candidate.snapshot_url:
            attempts.append(("WAYBACK", candidate))
        else:
            attempts.append(("DIRECT", candidate))
            if source.archive_fallback:
                attempts.append(("WAYBACK_GUESS", self._guessed_wayback_snapshot(candidate)))

        errors: list[str] = []
        for fetch_method, attempt in attempts:
            fetch_url = attempt.snapshot_url or attempt.url
            try:
                result = self.client.get(fetch_url)
                article = extract_article(result.content, attempt.title)
                if fetch_method == "DIRECT" and not title_body_coherent(article):
                    recovery_url = amp_recovery_url(attempt.url)
                    if recovery_url:
                        recovered = self.client.get(recovery_url)
                        recovered_article = extract_article(
                            recovered.content, attempt.title
                        )
                        if title_body_coherent(recovered_article):
                            result = recovered
                            article = recovered_article
                            fetch_method = "DIRECT_AMP_RECOVERY"
                text = article.text
                if _looks_like_access_interstitial(article):
                    raise CollectionError(
                        "publisher returned an access-check page instead of an article",
                        result.status_code,
                    )
                if len(text) < self.config.network.min_text_chars:
                    raise CollectionError(
                        f"extracted text is too short ({len(text)} characters)",
                        result.status_code,
                    )
                relevance_text = "\n".join(
                    value
                    for value in (article.title, text)
                    if value
                )
                if not _matches_keywords(relevance_text, self.config.keywords):
                    raise CollectionError(
                        "extracted text does not contain a configured case keyword",
                        result.status_code,
                    )
                truncated = len(text) > self.config.network.max_text_chars
                if truncated:
                    text = text[: self.config.network.max_text_chars].rstrip()
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                published_at = attempt.published_at or article.published_at
                snapshot = None
                if self.snapshot_store is not None:
                    snapshot = self.snapshot_store.put(
                        case_id=self.config.case_id,
                        source_id=source.source_id,
                        content=result.content,
                        media_type=result.headers.get("Content-Type", "text/html"),
                    )
                return StoredDocument(
                    case_id=self.config.case_id,
                    source_id=source.source_id,
                    url=attempt.url,
                    canonical_url=canonicalize_url(attempt.url),
                    title=article.title or attempt.title,
                    published_at=published_at,
                    archived_at=self._actual_archive_time(
                        result.url, attempt.archived_at
                    ),
                    text=text,
                    content_hash=content_hash,
                    fetch_method=fetch_method,
                    http_status=result.status_code,
                    truncated=truncated,
                    sample_phase=attempt.sample_phase,
                    snapshot_ref=snapshot.ref if snapshot else None,
                    snapshot_hash=snapshot.sha256 if snapshot else None,
                    snapshot_size_bytes=snapshot.size_bytes if snapshot else None,
                    snapshot_media_type=snapshot.media_type if snapshot else None,
                )
            except Exception as exc:
                errors.append(f"{fetch_method}: {exc}")
                if fetch_method == "WAYBACK_GUESS":
                    try:
                        resolved = self._resolve_wayback_snapshot(candidate)
                        if resolved is not None:
                            attempts.append(("WAYBACK_CDX", resolved))
                    except Exception as resolver_exc:
                        errors.append(f"WAYBACK_CDX_LOOKUP: {resolver_exc}")
        raise CollectionError(" | ".join(errors))
