from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .collector import Candidate, CollectionError, SourceCollector, canonicalize_url
from .config import CaseConfig
from .storage import ResearchStore
from .snapshots import LocalSnapshotStore


@dataclass
class CollectionSummary:
    run_id: str
    discovered: int = 0
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0

    def counters(self) -> dict[str, int]:
        values = asdict(self)
        values.pop("run_id")
        return values


class CollectionPipeline:
    def __init__(
        self,
        config: CaseConfig,
        store: ResearchStore,
        collector: SourceCollector | Any | None = None,
        retry_failed: bool = False,
    ):
        self.config = config
        self.store = store
        self.collector = collector or SourceCollector(
            config,
            snapshot_store=LocalSnapshotStore(store.path.parent / "snapshots"),
        )
        self.retry_failed = retry_failed

    def run(self) -> CollectionSummary:
        self.store.register_case(self.config)
        run_id = self.store.start_run(self.config.case_id)
        summary = CollectionSummary(run_id=run_id)
        error: str | None = None
        try:
            for source in self.config.sources:
                if self.store.ready_count(self.config.case_id) >= self.config.max_documents:
                    break
                source_limit = source.max_documents or self.config.max_documents_per_source
                if self.store.ready_count(self.config.case_id, source.source_id) >= source_limit:
                    continue
                try:
                    candidates = self.collector.discover(source)
                except Exception as exc:
                    self.store.record_run_error(
                        run_id,
                        "DISCOVERY",
                        str(exc),
                        source_id=source.source_id,
                    )
                    summary.failed += 1
                    continue
                summary.discovered += len(candidates)
                for candidate in candidates:
                    if self.store.ready_count(self.config.case_id) >= self.config.max_documents:
                        break
                    if self.store.ready_count(self.config.case_id, source.source_id) >= source_limit:
                        break
                    canonical_url = canonicalize_url(candidate.url)
                    existing_status = self.store.get_existing_status(
                        self.config.case_id, canonical_url
                    )
                    if existing_status in {"READY", "DUPLICATE"} or (
                        existing_status == "FAILED" and not self.retry_failed
                    ):
                        summary.skipped += 1
                        continue
                    try:
                        document = self.collector.fetch(source, candidate)
                        status = self.store.store_document(run_id, document)
                        if status == "READY":
                            summary.stored += 1
                        else:
                            summary.duplicates += 1
                    except Exception as exc:
                        http_status = exc.http_status if isinstance(exc, CollectionError) else None
                        self.store.record_run_error(
                            run_id,
                            "FETCH",
                            str(exc),
                            source_id=source.source_id,
                            url=candidate.url,
                        )
                        self.store.record_failure(
                            run_id=run_id,
                            case_id=self.config.case_id,
                            source_id=source.source_id,
                            url=candidate.url,
                            canonical_url=canonical_url,
                            title=candidate.title,
                            published_at=candidate.published_at,
                            sample_phase=candidate.sample_phase,
                            error=str(exc),
                            http_status=http_status,
                        )
                        summary.failed += 1
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self.store.finish_run(run_id, summary.counters(), error=error)
        return summary
