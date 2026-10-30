from __future__ import annotations

import hashlib
import gzip
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from bs4 import BeautifulSoup, SoupStrainer

from .basket_builder import (
    ArticleLead,
    PHASES,
    PHASE_POLICY_VERSION,
    PublisherRoster,
    SelectedArticle,
    StagedArticle,
    classify_sample_phase,
    is_probable_article_url,
)
from .collector import canonicalize_url, extract_article
from .snapshots import LocalSnapshotStore


ACTIVE_RUN_STATUSES = {"QUEUED", "RUNNING"}
STAGING_POLICY_VERSION = "html-extraction-v2-jsonld"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def case_progress_dir(root: str | Path, case_id: str) -> Path:
    return Path(root) / "analysis" / "case_generator" / case_id.lower()


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def load_json(path: str | Path) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def run_state_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_run_state_v1.json"


def stage_cache_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_stage_cache_v1.json"


def lead_cache_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_leads_cache_v1.json"


def attempt_cache_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_attempts_cache_v1.json"


def snapshot_recovery_marker_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_snapshot_recovery_v1.json"


def lock_path(root: str | Path, case_id: str) -> Path:
    return case_progress_dir(root, case_id) / "basket_discovery.lock"


def load_run_state(root: str | Path, case_id: str) -> dict[str, Any] | None:
    return load_json(run_state_path(root, case_id))


def save_run_state(
    root: str | Path,
    case_id: str,
    run_id: str,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    previous = load_run_state(root, case_id) or {}
    if previous.get("run_id") not in {None, run_id}:
        previous = {}
    payload = {
        **previous,
        "schema_version": "1.0",
        "case_id": case_id,
        "run_id": run_id,
        "status": status,
        "updated_at": utc_now(),
        **fields,
    }
    atomic_write_json(run_state_path(root, case_id), payload)
    return payload


def process_is_alive(pid: Any) -> bool:
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    try:
        os.kill(numeric_pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    # A detached child may remain briefly as a zombie until Streamlit reaps it.
    # ``kill(pid, 0)`` still succeeds for that state, so it must not keep the
    # per-case lock alive and block the next resumable run.
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(numeric_pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = completed.stdout.strip().upper()
    if completed.returncode == 0 and state.startswith("Z"):
        return False
    return True


def run_is_active(state: Mapping[str, Any] | None) -> bool:
    if not state or state.get("status") not in ACTIVE_RUN_STATUSES:
        return False
    pid = state.get("pid")
    return pid is None or process_is_alive(pid)


@contextmanager
def case_run_lock(
    root: str | Path, case_id: str, run_id: str
) -> Iterator[None]:
    path = lock_path(root, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = load_json(path)
        if existing and process_is_alive(existing.get("pid")):
            raise RuntimeError("basket discovery is already running")
        path.unlink(missing_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            json.dumps(
                {"run_id": run_id, "pid": os.getpid(), "started_at": utc_now()}
            ).encode("utf-8"),
        )
        os.close(descriptor)
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        current = load_json(path)
        if not current or current.get("run_id") == run_id:
            path.unlink(missing_ok=True)


def input_signature(manifest: Mapping[str, Any], roster: PublisherRoster) -> str:
    encoded = json.dumps(
        {
            "manifest": manifest,
            "roster_version": roster.roster_version,
            "phase_policy_version": PHASE_POLICY_VERSION,
            "staging_policy_version": STAGING_POLICY_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def article_lead_to_dict(lead: ArticleLead) -> dict[str, Any]:
    return {
        "source_id": lead.source_id,
        "url": lead.url,
        "title": lead.title,
        "published_at": lead.published_at,
        "provider_id": lead.provider_id,
        "sample_phase": lead.sample_phase,
    }


def article_lead_from_dict(payload: Mapping[str, Any]) -> ArticleLead:
    return ArticleLead(**{key: str(payload[key]) for key in (
        "source_id",
        "url",
        "title",
        "published_at",
        "provider_id",
        "sample_phase",
    )})


def previous_basket_leads(
    basket: Mapping[str, Any] | None,
    roster: PublisherRoster,
) -> list[ArticleLead]:
    """Rehydrate approved staging inputs when the roster signature changes."""
    if not basket:
        return []
    known_sources = {
        outlet.source_id
        for slot in roster.slots
        for outlet in slot.outlets
    }
    restored: list[ArticleLead] = []
    for article in basket.get("articles") or []:
        try:
            source_id = str(article["source_id"])
            phase = str(article["sample_phase"])
            if source_id not in known_sources or phase not in PHASES:
                continue
            restored.append(
                ArticleLead(
                    source_id=source_id,
                    url=str(article["url"]),
                    title=str(article["title"]),
                    published_at=str(article["published_at"]),
                    provider_id="previous-basket",
                    sample_phase=phase,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return restored


def staged_article_to_dict(item: StagedArticle) -> dict[str, Any]:
    return {
        "publisher_slot": item.selected.slot.slot_id,
        "source_id": item.selected.outlet.source_id,
        "source_name": item.selected.outlet.name,
        "language": item.selected.outlet.language,
        "editorial_profile": item.selected.outlet.editorial_profile,
        "sample_phase": item.selected.lead.sample_phase,
        "provider_id": item.selected.lead.provider_id,
        "url": item.selected.lead.url,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "published_at": item.published_at,
        "snapshot_ref": item.snapshot_ref,
        "snapshot_hash": item.snapshot_hash,
        "content_hash": item.content_hash,
        "fetch_method": item.fetch_method,
    }


def staged_article_from_dict(
    payload: Mapping[str, Any], roster: PublisherRoster
) -> StagedArticle:
    source_id = str(payload["source_id"])
    match = next(
        (slot, outlet)
        for slot in roster.slots
        for outlet in slot.outlets
        if outlet.source_id == source_id
    )
    slot, outlet = match
    lead = ArticleLead(
        source_id=source_id,
        url=str(payload["url"]),
        title=str(payload["title"]),
        published_at=str(payload["published_at"]),
        provider_id=str(payload.get("provider_id") or "cached"),
        sample_phase=str(payload["sample_phase"]),
    )
    return StagedArticle(
        selected=SelectedArticle(slot=slot, outlet=outlet, lead=lead),
        canonical_url=str(payload["canonical_url"]),
        title=str(payload["title"]),
        published_at=str(payload["published_at"]),
        snapshot_ref=str(payload["snapshot_ref"]),
        snapshot_hash=str(payload["snapshot_hash"]),
        content_hash=str(payload["content_hash"]),
        fetch_method=str(payload["fetch_method"]),
    )


def save_lead_cache(
    root: str | Path,
    case_id: str,
    signature: str,
    leads: Sequence[ArticleLead],
    provider_counts: Mapping[str, int],
) -> None:
    atomic_write_json(
        lead_cache_path(root, case_id),
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "input_signature": signature,
            "updated_at": utc_now(),
            "provider_counts": dict(provider_counts),
            "leads": [article_lead_to_dict(lead) for lead in leads],
        },
    )


def load_lead_cache(
    root: str | Path, case_id: str, signature: str
) -> tuple[list[ArticleLead], dict[str, int]] | None:
    payload = load_json(lead_cache_path(root, case_id))
    if not payload or payload.get("input_signature") != signature:
        return None
    try:
        leads = [article_lead_from_dict(item) for item in payload.get("leads", [])]
        counts = {
            str(key): int(value)
            for key, value in (payload.get("provider_counts") or {}).items()
        }
    except (KeyError, TypeError, ValueError):
        return None
    # An empty discovery result is not a durable cache entry.  In particular,
    # provider/network failures used to leave a valid-looking empty cache that
    # made the next healthy run skip discovery and incorrectly request a human
    # source-substitution decision.  Re-run the bounded providers until at
    # least one lead (or a complete reviewed seed set) exists.
    if not leads:
        return None
    return leads, counts


def save_attempt_cache(
    root: str | Path,
    case_id: str,
    signature: str,
    attempts: Mapping[str, int],
) -> None:
    atomic_write_json(
        attempt_cache_path(root, case_id),
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "input_signature": signature,
            "updated_at": utc_now(),
            "attempts": {
                str(url): int(count)
                for url, count in attempts.items()
                if int(count) > 0
            },
        },
    )


def load_attempt_cache(
    root: str | Path, case_id: str, signature: str
) -> dict[str, int]:
    payload = load_json(attempt_cache_path(root, case_id))
    if not payload or payload.get("input_signature") != signature:
        return {}
    try:
        return {
            str(url): int(count)
            for url, count in (payload.get("attempts") or {}).items()
            if int(count) > 0
        }
    except (TypeError, ValueError):
        return {}


def snapshot_recovery_is_complete(
    root: str | Path, case_id: str, signature: str
) -> bool:
    payload = load_json(snapshot_recovery_marker_path(root, case_id))
    return bool(payload and payload.get("input_signature") == signature)


def mark_snapshot_recovery_complete(
    root: str | Path, case_id: str, signature: str
) -> None:
    atomic_write_json(
        snapshot_recovery_marker_path(root, case_id),
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "input_signature": signature,
            "completed_at": utc_now(),
        },
    )


def save_stage_cache(
    root: str | Path,
    case_id: str,
    signature: str,
    staged: Sequence[StagedArticle],
) -> None:
    atomic_write_json(
        stage_cache_path(root, case_id),
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "input_signature": signature,
            "updated_at": utc_now(),
            "staged_articles": [staged_article_to_dict(item) for item in staged],
        },
    )


def load_stage_cache(
    root: str | Path,
    case_id: str,
    signature: str,
    roster: PublisherRoster,
) -> list[StagedArticle]:
    payload = load_json(stage_cache_path(root, case_id))
    if not payload or payload.get("input_signature") != signature:
        return []
    restored: list[StagedArticle] = []
    for item in payload.get("staged_articles", []):
        try:
            if not is_probable_article_url(str(item["url"])):
                continue
            restored.append(staged_article_from_dict(item, roster))
        except (KeyError, StopIteration, TypeError, ValueError):
            continue
    return restored


def recover_compatible_stage_cache(
    root: str | Path,
    case_id: str,
    roster: PublisherRoster,
    manifest: Mapping[str, Any],
    staging_root: str | Path,
) -> list[StagedArticle]:
    """Recover verified staged pages after a case roster is explicitly pinned.

    This path deliberately ignores only the old input signature.  It still
    requires the same case, a source present in the new roster, the same
    calendar phase, a unique slot-phase cell, and a snapshot whose bytes match
    the recorded hash.  That keeps an in-progress case reusable when the global
    roster evolves without silently importing incompatible material.
    """
    payload = load_json(stage_cache_path(root, case_id))
    if not payload or payload.get("case_id") != case_id:
        return []
    recovered: list[StagedArticle] = []
    cells: set[tuple[str, str]] = set()
    content_hashes: set[str] = set()
    snapshot_prefix = f"local-snapshot://{case_id}/"
    for item in payload.get("staged_articles", []):
        try:
            article = staged_article_from_dict(item, roster)
            phase = article.selected.lead.sample_phase
            expected_phase = classify_sample_phase(
                article.published_at, str(manifest["window_start"])
            )
            if phase != expected_phase:
                continue
            cell = (article.selected.slot.slot_id, phase)
            if cell in cells or article.content_hash in content_hashes:
                continue
            ref = article.snapshot_ref
            if not ref.startswith(snapshot_prefix):
                continue
            relative = ref.split("local-snapshot://", 1)[1]
            snapshot = Path(staging_root) / case_id / "snapshots" / relative
            if not snapshot.exists():
                continue
            raw = gzip.decompress(snapshot.read_bytes())
            if hashlib.sha256(raw).hexdigest() != article.snapshot_hash:
                continue
        except (KeyError, OSError, StopIteration, TypeError, ValueError, gzip.BadGzipFile):
            continue
        recovered.append(article)
        cells.add(cell)
        content_hashes.add(article.content_hash)
    return recovered


def recover_staged_from_snapshot_cache(
    manifest: Mapping[str, Any],
    leads: Sequence[ArticleLead],
    roster: PublisherRoster,
    staging_root: str | Path,
    staged: Sequence[StagedArticle],
) -> list[StagedArticle]:
    """Recover previously verified pages when an older run lacked metadata cache."""
    restored = list(staged)
    completed = {
        (item.selected.slot.slot_id, item.selected.lead.sample_phase)
        for item in restored
    }
    content_hashes = {item.content_hash for item in restored}
    source_lookup = {
        outlet.source_id: (slot, outlet)
        for slot in roster.slots
        for outlet in slot.outlets
    }
    lead_lookup = {
        canonicalize_url(lead.url): lead
        for lead in leads
        if lead.source_id in source_lookup
    }
    snapshot_root = (
        Path(staging_root)
        / str(manifest["case_id"])
        / "snapshots"
        / str(manifest["case_id"])
    )
    if not snapshot_root.exists() or not lead_lookup:
        return restored
    store = LocalSnapshotStore(Path(staging_root) / str(manifest["case_id"]) / "snapshots")
    for path in sorted(snapshot_root.glob("*/*.gz")):
        try:
            raw_html = gzip.decompress(path.read_bytes())
            soup = BeautifulSoup(
                raw_html,
                "html.parser",
                parse_only=SoupStrainer(["meta", "link"]),
            )
            candidate_urls: list[str] = []
            og_url = soup.find("meta", attrs={"property": "og:url"})
            if og_url and og_url.get("content"):
                candidate_urls.append(str(og_url["content"]))
            canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
            if canonical and canonical.get("href"):
                candidate_urls.append(str(canonical["href"]))
            lead = next(
                (
                    lead_lookup[canonicalize_url(url)]
                    for url in candidate_urls
                    if canonicalize_url(url) in lead_lookup
                ),
                None,
            )
            if lead is None:
                continue
            slot, outlet = source_lookup[lead.source_id]
            cell = (slot.slot_id, lead.sample_phase)
            if cell in completed:
                continue
            article = extract_article(raw_html, lead.title)
            text = article.text[:30_000].rstrip()
            if len(text) < 250:
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if content_hash in content_hashes:
                continue
            receipt = store.put(
                case_id=str(manifest["case_id"]),
                source_id=lead.source_id,
                content=raw_html,
                media_type="text/html",
            )
            restored.append(
                StagedArticle(
                    selected=SelectedArticle(slot=slot, outlet=outlet, lead=lead),
                    canonical_url=canonicalize_url(lead.url),
                    title=article.title or lead.title,
                    published_at=lead.published_at,
                    snapshot_ref=receipt.ref,
                    snapshot_hash=receipt.sha256,
                    content_hash=content_hash,
                    fetch_method="LOCAL_CACHE",
                )
            )
            completed.add(cell)
            content_hashes.add(content_hash)
        except (OSError, EOFError, ValueError, TypeError):
            continue
    return restored


def restore_staged_from_previous_basket(
    basket: Mapping[str, Any] | None,
    roster: PublisherRoster,
    staging_root: str | Path,
    staged: Sequence[StagedArticle],
) -> list[StagedArticle]:
    """Restore immutable verified pages directly from the last basket version."""
    if not basket:
        return list(staged)
    restored = list(staged)
    completed = {
        (item.selected.slot.slot_id, item.selected.lead.sample_phase)
        for item in restored
    }
    content_hashes = {item.content_hash for item in restored}
    source_lookup = {
        outlet.source_id: (slot, outlet)
        for slot in roster.slots
        for outlet in slot.outlets
    }
    store = LocalSnapshotStore(
        Path(staging_root) / str(basket["case_id"]) / "snapshots"
    )
    for article in basket.get("articles") or []:
        try:
            if not is_probable_article_url(str(article["url"])):
                continue
            source_id = str(article["source_id"])
            phase = str(article["sample_phase"])
            slot, outlet = source_lookup[source_id]
            cell = (slot.slot_id, phase)
            if cell in completed:
                continue
            raw_html = store.read(str(article["snapshot_ref"]))
            snapshot_hash = hashlib.sha256(raw_html).hexdigest()
            if snapshot_hash != str(article["snapshot_hash"]):
                continue
            extracted = extract_article(raw_html, str(article["title"]))
            text = extracted.text[:30_000].rstrip()
            if len(text) < 250:
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if content_hash in content_hashes:
                continue
            lead = ArticleLead(
                source_id=source_id,
                url=str(article["url"]),
                title=str(article["title"]),
                published_at=str(article["published_at"]),
                provider_id="previous-basket",
                sample_phase=phase,
            )
            restored.append(
                StagedArticle(
                    selected=SelectedArticle(slot=slot, outlet=outlet, lead=lead),
                    canonical_url=str(article["canonical_url"]),
                    title=extracted.title or str(article["title"]),
                    published_at=str(article["published_at"]),
                    snapshot_ref=str(article["snapshot_ref"]),
                    snapshot_hash=snapshot_hash,
                    content_hash=content_hash,
                    fetch_method="LOCAL_CACHE",
                )
            )
            completed.add(cell)
            content_hashes.add(content_hash)
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return restored
