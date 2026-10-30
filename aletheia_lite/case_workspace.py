from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .collector import extract_article
from .config import CaseConfig, SourceConfig
from .storage import ResearchStore, StoredDocument


CASE_ID_PATTERN = re.compile(r"^CASE-[0-9]{3}$")
LOCAL_SNAPSHOT_PREFIX = "local-snapshot://"


class CaseWorkspaceError(ValueError):
    """Raised when an approved case cannot be materialized safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if _canonical_json(existing) != _canonical_json(payload):
            raise CaseWorkspaceError(f"immutable workspace artifact changed: {path.name}")
        return
    _atomic_json(path, payload)


def _snapshot_relative(ref: str) -> Path:
    if not ref.startswith(LOCAL_SNAPSHOT_PREFIX):
        raise CaseWorkspaceError("approved basket contains a non-local staging snapshot")
    relative = Path(ref[len(LOCAL_SNAPSHOT_PREFIX) :])
    if relative.is_absolute() or ".." in relative.parts:
        raise CaseWorkspaceError("approved basket contains an unsafe snapshot path")
    return relative


@dataclass(frozen=True)
class CaseWorkspace:
    project_root: Path
    case_id: str

    def __post_init__(self) -> None:
        if not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise CaseWorkspaceError(f"invalid case id: {self.case_id}")

    @property
    def case_root(self) -> Path:
        return self.project_root / "data" / "cases" / self.case_id.lower()

    @property
    def research_db(self) -> Path:
        return self.case_root / "research.sqlite3"

    @property
    def snapshot_root(self) -> Path:
        return self.case_root / "snapshots"

    @property
    def manifest_path(self) -> Path:
        return self.case_root / "manifest.json"

    @property
    def basket_path(self) -> Path:
        return self.case_root / "approved_basket.json"

    @property
    def receipt_path(self) -> Path:
        return self.case_root / "workspace_receipt.json"

    @property
    def anchors_dir(self) -> Path:
        return self.case_root / "anchors"

    @property
    def analysis_dir(self) -> Path:
        return self.case_root / "analysis"

    @property
    def adjudication_dir(self) -> Path:
        return self.case_root / "adjudication"

    @property
    def results_dir(self) -> Path:
        return self.case_root / "results"

    @property
    def cloud_stage_dir(self) -> Path:
        return self.case_root / "cloud_stage"

    @property
    def revisions_dir(self) -> Path:
        return self.case_root / "revisions"

    def artifact_path(self, stage: str, filename: str) -> Path:
        roots = {
            "anchors": self.anchors_dir,
            "analysis": self.analysis_dir,
            "adjudication": self.adjudication_dir,
            "results": self.results_dir,
            "cloud": self.cloud_stage_dir,
        }
        try:
            base = roots[stage]
        except KeyError as error:
            raise CaseWorkspaceError(f"unknown workspace stage: {stage}") from error
        target = Path(filename)
        if target.is_absolute() or ".." in target.parts:
            raise CaseWorkspaceError("unsafe artifact filename")
        return base / target

    def write_json_artifact(
        self, stage: str, filename: str, payload: Mapping[str, Any]
    ) -> str:
        path = self.artifact_path(stage, filename)
        _atomic_json(path, payload)
        return str(path.relative_to(self.project_root))


def _source_configs(basket: Mapping[str, Any]) -> tuple[SourceConfig, ...]:
    sources: dict[str, SourceConfig] = {}
    for article in basket["articles"]:
        source_id = str(article["source_id"])
        candidate = SourceConfig(
            source_id=source_id,
            name=str(article["source_name"]),
            group=str(article["source_group"]),
            language=str(article["language"]),
            discovery="urls",
            publisher_slot=str(article["publisher_slot"]),
            editorial_profile=str(article["editorial_profile"]),
            substitute_for=article.get("substitute_for_source_id"),
            # The approved basket can legitimately contain mobile and desktop
            # hostnames for the same publisher (for example mobile.mako.co.il
            # and www.mako.co.il).  URL discovery does not use ``domain``;
            # retaining the per-article hostname here would incorrectly make
            # otherwise identical source metadata look inconsistent.
            domain=None,
            max_documents=3,
        )
        existing = sources.get(source_id)
        if existing is not None and existing != candidate:
            raise CaseWorkspaceError(f"source metadata changed within basket: {source_id}")
        sources[source_id] = candidate
    return tuple(sources[key] for key in sorted(sources))


def case_config_from_approved_records(
    manifest: Mapping[str, Any], basket: Mapping[str, Any]
) -> CaseConfig:
    if manifest["case_id"] != basket["case_id"]:
        raise CaseWorkspaceError("manifest and basket case ids do not match")
    keywords = tuple(
        str(term)
        for language in ("he", "en", "ar")
        for term in manifest["keywords"][language]
    )
    return CaseConfig(
        case_id=str(manifest["case_id"]),
        title=str(manifest["title"]),
        window_start=datetime.fromisoformat(
            str(manifest["window_start"]).replace("Z", "+00:00")
        ),
        window_end=datetime.fromisoformat(
            str(manifest["window_end"]).replace("Z", "+00:00")
        ),
        keywords=keywords,
        sources=_source_configs(basket),
        max_documents=int(manifest["target_articles"]),
        max_documents_per_source=3,
        max_discovery_candidates_per_source=75,
    )


def initialize_case_workspace(
    workspace: CaseWorkspace,
    manifest: Mapping[str, Any],
    basket: Mapping[str, Any],
    *,
    staging_root: str | Path,
) -> dict[str, Any]:
    """Promote an approved local basket into an isolated, replay-safe workspace."""

    if manifest["case_id"] != workspace.case_id or basket["case_id"] != workspace.case_id:
        raise CaseWorkspaceError("workspace identity does not match approved records")
    if basket.get("registry_status") != "APPROVED":
        raise CaseWorkspaceError("workspace creation requires the exact approved basket")
    minimum = int(manifest["minimum_articles"])
    articles = list(basket["articles"])
    if len(articles) < minimum:
        raise CaseWorkspaceError("approved basket is below its hard article floor")

    workspace.case_root.mkdir(parents=True, exist_ok=True)
    _write_immutable_json(workspace.manifest_path, dict(manifest))
    previous_basket: dict[str, Any] | None = None
    is_revision = False
    if workspace.basket_path.exists():
        previous_basket = json.loads(workspace.basket_path.read_text(encoding="utf-8"))
        is_revision = previous_basket.get("basket_hash") != basket.get("basket_hash")
        if is_revision:
            previous_hash = str(previous_basket.get("basket_hash") or "unknown")
            archive = workspace.revisions_dir / previous_hash[:16]
            archive.mkdir(parents=True, exist_ok=True)
            _write_immutable_json(archive / "approved_basket.json", previous_basket)
            if workspace.receipt_path.exists():
                previous_receipt = json.loads(
                    workspace.receipt_path.read_text(encoding="utf-8")
                )
                _write_immutable_json(archive / "workspace_receipt.json", previous_receipt)
            for relative in (
                "analysis/analysis_roster.json",
                "analysis/corpus.csv",
                "analysis/article_measurements_v1.jsonl",
                "analysis/article_measurements_v1_summary.json",
                "analysis/frame_intensity_full_first_pass_v1.jsonl",
                "analysis/frame_intensity_full_first_pass_v1_summary.json",
                "analysis/frame_intensity_full_second_pass_v1.jsonl",
                "analysis/frame_intensity_full_second_pass_v1_summary.json",
                "adjudication/dilemmas_v1.json",
                "adjudication/evidence_translations_he_v1.json",
                "results/run_receipt_v1.json",
            ):
                source = workspace.case_root / relative
                destination = archive / relative
                if source.exists() and not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    config = case_config_from_approved_records(manifest, basket)
    store = ResearchStore(workspace.research_db)
    store.register_case(config)
    run_id = store.start_run(workspace.case_id)
    staging_base = Path(staging_root) / workspace.case_id / "snapshots"
    promoted = 0
    reused = 0
    receipts: list[dict[str, Any]] = []

    try:
        for article in articles:
            relative = _snapshot_relative(str(article["snapshot_ref"]))
            source = staging_base / relative
            if not source.exists():
                raise CaseWorkspaceError(
                    f"approved staging snapshot is missing: {article['article_id']}"
                )
            compressed = source.read_bytes()
            try:
                raw_html = gzip.decompress(compressed)
            except (OSError, EOFError) as error:
                raise CaseWorkspaceError(
                    f"approved snapshot is not valid gzip: {article['article_id']}"
                ) from error
            raw_hash = hashlib.sha256(raw_html).hexdigest()
            if raw_hash != article["snapshot_hash"]:
                raise CaseWorkspaceError(
                    f"approved snapshot hash mismatch: {article['article_id']}"
                )
            destination = workspace.snapshot_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != compressed:
                    raise CaseWorkspaceError(
                        f"canonical snapshot collision: {article['article_id']}"
                    )
                reused += 1
            else:
                temporary = destination.with_name(
                    f".{destination.name}.{uuid.uuid4().hex}.tmp"
                )
                try:
                    temporary.write_bytes(compressed)
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                promoted += 1

            extracted = extract_article(raw_html, fallback_title=str(article["title"]))
            if not extracted.text:
                raise CaseWorkspaceError(
                    f"approved snapshot has no extractable article text: {article['article_id']}"
                )
            content_hash = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
            canonical_ref = LOCAL_SNAPSHOT_PREFIX + relative.as_posix()
            status = store.get_existing_status(
                workspace.case_id, str(article["canonical_url"])
            )
            if status != "READY":
                stored_status = store.store_document(
                    run_id,
                    StoredDocument(
                        case_id=workspace.case_id,
                        source_id=str(article["source_id"]),
                        url=str(article["url"]),
                        canonical_url=str(article["canonical_url"]),
                        title=extracted.title or str(article["title"]),
                        published_at=str(article["published_at"]),
                        archived_at=None,
                        text=extracted.text,
                        content_hash=content_hash,
                        fetch_method="PROMOTED_APPROVED_STAGING",
                        http_status=200,
                        truncated=False,
                        sample_phase=str(article["sample_phase"]),
                        snapshot_ref=canonical_ref,
                        snapshot_hash=raw_hash,
                        snapshot_size_bytes=len(raw_html),
                        snapshot_media_type="text/html",
                    ),
                )
                if stored_status != "READY":
                    raise CaseWorkspaceError(
                        f"approved article became a content duplicate: {article['article_id']}"
                    )
            receipts.append(
                {
                    "article_id": article["article_id"],
                    "snapshot_ref": canonical_ref,
                    "snapshot_hash": raw_hash,
                    "content_hash": content_hash,
                }
            )
        activation = store.activate_canonical_urls(
            workspace.case_id,
            {str(article["canonical_url"]) for article in articles},
        )
        counts = store.counts(workspace.case_id)
        if counts["READY"] != len(articles):
            raise CaseWorkspaceError(
                f"workspace contains {counts['READY']} ready documents, expected {len(articles)}"
            )
        store.finish_run(
            run_id,
            {
                "discovered": len(articles),
                "stored": promoted,
                "duplicates": 0,
                "skipped": reused,
                "failed": 0,
            },
        )
    except Exception as error:
        store.finish_run(
            run_id,
            {
                "discovered": len(articles),
                "stored": promoted,
                "duplicates": 0,
                "skipped": reused,
                "failed": 1,
            },
            error=str(error),
        )
        raise

    receipt = {
        "schema_version": "1.0",
        "case_id": workspace.case_id,
        "basket_hash": basket["basket_hash"],
        "article_count": len(articles),
        "promoted_snapshots": promoted,
        "reused_snapshots": reused,
        "retired_documents": int(activation["retired"]),
        "reactivated_documents": int(activation["reactivated"]),
        "cloud_writes": 0,
        "created_at": _utc_now(),
        "documents": sorted(receipts, key=lambda item: item["article_id"]),
    }
    if workspace.receipt_path.exists() and not is_revision:
        previous = json.loads(workspace.receipt_path.read_text(encoding="utf-8"))
        comparable_previous = dict(previous)
        comparable_current = dict(receipt)
        comparable_previous.pop("created_at", None)
        comparable_current.pop("created_at", None)
        comparable_previous["promoted_snapshots"] = 0
        comparable_previous["reused_snapshots"] = len(articles)
        comparable_previous["retired_documents"] = 0
        comparable_previous["reactivated_documents"] = 0
        if _canonical_json(comparable_previous) != _canonical_json(comparable_current):
            raise CaseWorkspaceError("workspace receipt changed across an idempotent replay")
    if is_revision:
        _atomic_json(workspace.basket_path, dict(basket))
    else:
        _write_immutable_json(workspace.basket_path, dict(basket))
    _atomic_json(workspace.receipt_path, receipt)
    return receipt


def materialize_analysis_inputs(workspace: CaseWorkspace) -> dict[str, Any]:
    """Export the frozen corpus and its exact allow-list for bounded scoring.

    This is intentionally derived from the isolated case database instead of
    from the staging cache.  It therefore cannot silently include a lead that
    was not part of the Gate-2-approved basket.
    """

    if not workspace.receipt_path.exists() or not workspace.basket_path.exists():
        raise CaseWorkspaceError("workspace must be initialized before analysis inputs")
    receipt = json.loads(workspace.receipt_path.read_text(encoding="utf-8"))
    basket = json.loads(workspace.basket_path.read_text(encoding="utf-8"))
    if receipt.get("basket_hash") != basket.get("basket_hash"):
        raise CaseWorkspaceError("workspace receipt is not bound to the approved basket")

    corpus_path = workspace.analysis_dir / "corpus.csv"
    store = ResearchStore(workspace.research_db)
    exported = store.export_documents(workspace.case_id, corpus_path)
    if exported != int(receipt.get("article_count") or 0):
        raise CaseWorkspaceError(
            f"analysis corpus has {exported} documents, expected {receipt.get('article_count')}"
        )

    import csv

    with corpus_path.open(encoding="utf-8-sig", newline="") as handle:
        document_ids = [str(row["document_id"]) for row in csv.DictReader(handle)]
    if len(document_ids) != exported or len(set(document_ids)) != exported:
        raise CaseWorkspaceError("analysis corpus document allow-list is not unique")

    roster = {
        "schema_version": "1.0",
        "case_id": workspace.case_id,
        "basket_hash": basket["basket_hash"],
        "document_ids": document_ids,
    }
    roster_path = workspace.analysis_dir / "analysis_roster.json"
    _atomic_json(roster_path, roster)
    return {
        "case_id": workspace.case_id,
        "basket_hash": basket["basket_hash"],
        "documents": exported,
        "corpus_path": str(corpus_path.relative_to(workspace.project_root)),
        "roster_path": str(roster_path.relative_to(workspace.project_root)),
    }
