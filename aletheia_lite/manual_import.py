from __future__ import annotations

import hashlib
from pathlib import Path

from .collector import canonicalize_url, extract_article, normalize_text
from .config import CaseConfig, SourceConfig
from .storage import ResearchStore, StoredDocument
from .snapshots import LocalSnapshotStore


def import_local_file(
    *,
    config: CaseConfig,
    store: ResearchStore,
    source: SourceConfig,
    url: str,
    path: str | Path,
    title: str = "",
    published_at: str | None = None,
    sample_phase: str | None = None,
    file_format: str = "auto",
) -> tuple[str, str]:
    """Import a human-reviewed HTML or text file without bypassing a publisher."""
    input_path = Path(path)
    size = input_path.stat().st_size
    if size > config.network.max_response_bytes:
        raise ValueError("Input file exceeds configured byte limit")
    raw = input_path.read_bytes()
    selected_format = file_format
    if selected_format == "auto":
        selected_format = "html" if input_path.suffix.lower() in {".htm", ".html"} else "text"
    if selected_format == "html":
        article = extract_article(raw, title)
        text = article.text
        final_title = article.title or title
        final_published_at = published_at or article.published_at
    elif selected_format == "text":
        text = normalize_text(raw.decode("utf-8"))
        final_title = title
        final_published_at = published_at
    else:
        raise ValueError("file_format must be auto, html, or text")
    if len(text) < config.network.min_text_chars:
        raise ValueError(f"Imported text is too short ({len(text)} characters)")
    truncated = len(text) > config.network.max_text_chars
    if truncated:
        text = text[: config.network.max_text_chars].rstrip()

    snapshot = LocalSnapshotStore(store.path.parent / "snapshots").put(
        case_id=config.case_id,
        source_id=source.source_id,
        content=raw,
        media_type="text/html" if selected_format == "html" else "text/plain",
    )

    store.register_case(config)
    run_id = store.start_run(config.case_id)
    try:
        status = store.store_document(
            run_id,
            StoredDocument(
                case_id=config.case_id,
                source_id=source.source_id,
                url=url,
                canonical_url=canonicalize_url(url),
                title=final_title,
                published_at=final_published_at,
                archived_at=None,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                fetch_method="MANUAL_IMPORT",
                http_status=None,
                truncated=truncated,
                sample_phase=sample_phase,
                snapshot_ref=snapshot.ref,
                snapshot_hash=snapshot.sha256,
                snapshot_size_bytes=snapshot.size_bytes,
                snapshot_media_type=snapshot.media_type,
            ),
        )
        summary = {
            "discovered": 1,
            "stored": int(status == "READY"),
            "duplicates": int(status == "DUPLICATE"),
            "skipped": 0,
            "failed": 0,
        }
        store.finish_run(run_id, summary)
        return run_id, status
    except Exception as exc:
        store.record_run_error(
            run_id,
            "MANUAL_IMPORT",
            str(exc),
            source_id=source.source_id,
            url=url,
        )
        store.finish_run(
            run_id,
            {"discovered": 1, "stored": 0, "duplicates": 0, "skipped": 0, "failed": 1},
            error=str(exc),
        )
        raise
