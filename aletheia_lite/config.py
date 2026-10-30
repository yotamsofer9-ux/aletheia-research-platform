from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_GROUPS = {
    "DOMESTIC_ISRAELI",
    "WESTERN_INTERNATIONAL",
    "REGIONAL_ARABIC",
}
ALLOWED_DISCOVERY_MODES = {"rss", "urls", "wayback"}
ALLOWED_SAMPLE_PHASES = {
    "INITIAL_REPORT",
    "EARLY_UPDATE",
    "LATER_INVESTIGATION",
}


def parse_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_http_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an http(s) URL")
    return value


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


@dataclass(frozen=True)
class NetworkLimits:
    timeout_seconds: float = 15.0
    max_response_bytes: int = 5_000_000
    requests_per_second_per_host: float = 1.0
    retries: int = 1
    max_text_chars: int = 30_000
    min_text_chars: int = 250

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "NetworkLimits":
        raw = raw or {}
        limits = cls(
            timeout_seconds=float(raw.get("timeout_seconds", 15.0)),
            max_response_bytes=int(raw.get("max_response_bytes", 5_000_000)),
            requests_per_second_per_host=float(
                raw.get("requests_per_second_per_host", 1.0)
            ),
            retries=int(raw.get("retries", 1)),
            max_text_chars=int(raw.get("max_text_chars", 30_000)),
            min_text_chars=int(raw.get("min_text_chars", 250)),
        )
        if not 1 <= limits.timeout_seconds <= 60:
            raise ValueError("network.timeout_seconds must be between 1 and 60")
        if not 10_000 <= limits.max_response_bytes <= 20_000_000:
            raise ValueError(
                "network.max_response_bytes must be between 10 KB and 20 MB"
            )
        if not 0.1 <= limits.requests_per_second_per_host <= 5:
            raise ValueError(
                "network.requests_per_second_per_host must be between 0.1 and 5"
            )
        if not 0 <= limits.retries <= 3:
            raise ValueError("network.retries must be between 0 and 3")
        if not 1_000 <= limits.max_text_chars <= 100_000:
            raise ValueError("network.max_text_chars must be between 1,000 and 100,000")
        if not 50 <= limits.min_text_chars <= limits.max_text_chars:
            raise ValueError("network.min_text_chars is outside the permitted range")
        return limits


@dataclass(frozen=True)
class SeedURL:
    url: str
    title: str = ""
    published_at: str | None = None
    sample_phase: str | None = None

    @classmethod
    def from_value(cls, raw: str | dict[str, Any]) -> "SeedURL":
        if isinstance(raw, str):
            return cls(url=validate_http_url(raw, "article_urls[]"))
        if not isinstance(raw, dict):
            raise ValueError("article_urls entries must be URLs or objects")
        url = validate_http_url(str(raw.get("url", "")), "article_urls[].url")
        published_at = raw.get("published_at")
        if published_at:
            parse_datetime(str(published_at), "article_urls[].published_at")
        sample_phase = raw.get("sample_phase")
        if sample_phase is not None:
            sample_phase = str(sample_phase).strip().upper()
            if sample_phase not in ALLOWED_SAMPLE_PHASES:
                raise ValueError(
                    "article_urls[].sample_phase must be INITIAL_REPORT, "
                    "EARLY_UPDATE, or LATER_INVESTIGATION"
                )
        return cls(
            url=url,
            title=str(raw.get("title", "")),
            published_at=str(published_at) if published_at else None,
            sample_phase=sample_phase,
        )


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    name: str
    group: str
    language: str
    discovery: str
    publisher_slot: str | None = None
    editorial_profile: str | None = None
    substitute_for: str | None = None
    feed_url: str | None = None
    domain: str | None = None
    article_urls: tuple[SeedURL, ...] = ()
    archive_fallback: bool = True
    max_documents: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceConfig":
        source_id = str(raw.get("source_id", "")).strip()
        name = str(raw.get("name", "")).strip()
        group = str(raw.get("group", "")).strip()
        discovery = str(raw.get("discovery", "")).strip().lower()
        if not source_id or not name:
            raise ValueError("Every source needs source_id and name")
        if group not in ALLOWED_GROUPS:
            raise ValueError(f"Unknown source group for {source_id}: {group}")
        if discovery not in ALLOWED_DISCOVERY_MODES:
            raise ValueError(f"Unknown discovery mode for {source_id}: {discovery}")

        feed_url = raw.get("feed_url")
        if feed_url:
            feed_url = validate_http_url(str(feed_url), f"{source_id}.feed_url")
        domain = str(raw.get("domain", "")).strip().lower() or None
        if domain and "://" in domain:
            raise ValueError(f"{source_id}.domain must be a hostname, not a URL")
        article_urls = tuple(
            SeedURL.from_value(item) for item in raw.get("article_urls", [])
        )
        if discovery == "rss" and not feed_url:
            raise ValueError(f"RSS source {source_id} needs feed_url")
        if discovery == "urls" and not article_urls:
            raise ValueError(f"URL source {source_id} needs article_urls")
        if discovery == "wayback" and not domain:
            raise ValueError(f"Wayback source {source_id} needs domain")

        max_documents = raw.get("max_documents")
        if max_documents is not None and int(max_documents) < 1:
            raise ValueError(f"{source_id}.max_documents must be positive")
        return cls(
            source_id=source_id,
            name=name,
            group=group,
            language=str(raw.get("language", "und")),
            discovery=discovery,
            publisher_slot=optional_text(raw.get("publisher_slot")),
            editorial_profile=optional_text(raw.get("editorial_profile")),
            substitute_for=optional_text(raw.get("substitute_for")),
            feed_url=feed_url,
            domain=domain,
            article_urls=article_urls,
            archive_fallback=bool(raw.get("archive_fallback", True)),
            max_documents=int(max_documents) if max_documents is not None else None,
        )


@dataclass(frozen=True)
class CaseConfig:
    case_id: str
    title: str
    window_start: datetime
    window_end: datetime
    keywords: tuple[str, ...]
    sources: tuple[SourceConfig, ...]
    max_documents: int = 30
    max_documents_per_source: int = 10
    max_discovery_candidates_per_source: int = 300
    network: NetworkLimits = field(default_factory=NetworkLimits)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CaseConfig":
        case_id = str(raw.get("case_id", "")).strip()
        title = str(raw.get("title", "")).strip()
        if not case_id or not title:
            raise ValueError("case_id and title are required")
        window_start = parse_datetime(str(raw.get("window_start", "")), "window_start")
        window_end = parse_datetime(str(raw.get("window_end", "")), "window_end")
        if window_end <= window_start:
            raise ValueError("window_end must be later than window_start")
        keywords = tuple(
            value.strip().lower()
            for value in raw.get("keywords", [])
            if str(value).strip()
        )
        if not keywords:
            raise ValueError("At least one keyword is required")
        sources = tuple(SourceConfig.from_dict(item) for item in raw.get("sources", []))
        if not sources:
            raise ValueError("At least one source is required")
        source_ids = [source.source_id for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")
        cls._validate_publisher_slots(sources)

        max_documents = int(raw.get("max_documents", 30))
        max_per_source = int(raw.get("max_documents_per_source", 10))
        max_candidates = int(raw.get("max_discovery_candidates_per_source", 300))
        if not 1 <= max_documents <= 2_000:
            raise ValueError("max_documents must be between 1 and 2,000")
        if not 1 <= max_per_source <= max_documents:
            raise ValueError("max_documents_per_source must be positive and within max_documents")
        if not 1 <= max_candidates <= 2_000:
            raise ValueError(
                "max_discovery_candidates_per_source must be between 1 and 2,000"
            )
        return cls(
            case_id=case_id,
            title=title,
            window_start=window_start,
            window_end=window_end,
            keywords=keywords,
            sources=sources,
            max_documents=max_documents,
            max_documents_per_source=max_per_source,
            max_discovery_candidates_per_source=max_candidates,
            network=NetworkLimits.from_dict(raw.get("network")),
        )

    @staticmethod
    def _validate_publisher_slots(sources: tuple[SourceConfig, ...]) -> None:
        slots: dict[str, list[SourceConfig]] = {}
        for source in sources:
            if source.substitute_for and not source.publisher_slot:
                raise ValueError(
                    f"{source.source_id}.substitute_for requires publisher_slot"
                )
            if source.publisher_slot:
                if not source.editorial_profile:
                    raise ValueError(
                        f"{source.source_id}.publisher_slot requires editorial_profile"
                    )
                slots.setdefault(source.publisher_slot, []).append(source)

        for slot, members in slots.items():
            if len(members) > 2:
                raise ValueError(
                    f"publisher_slot {slot} cannot mix more than two publishers"
                )
            if len(members) == 1:
                if members[0].substitute_for:
                    raise ValueError(
                        f"publisher_slot {slot} has a substitute without its primary"
                    )
                continue

            primary = [member for member in members if not member.substitute_for]
            substitute = [member for member in members if member.substitute_for]
            if len(primary) != 1 or len(substitute) != 1:
                raise ValueError(
                    f"publisher_slot {slot} needs one primary and one substitute"
                )
            original = primary[0]
            replacement = substitute[0]
            if replacement.substitute_for != original.source_id:
                raise ValueError(
                    f"{replacement.source_id}.substitute_for must name {original.source_id}"
                )
            if replacement.group != original.group or replacement.language != original.language:
                raise ValueError(
                    f"publisher_slot {slot} must keep group and language"
                )
            if replacement.editorial_profile != original.editorial_profile:
                raise ValueError(
                    f"publisher_slot {slot} must keep the editorial profile"
                )


def load_case_config(path: str | Path) -> CaseConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("Case configuration must be a JSON object")
    return CaseConfig.from_dict(raw)
