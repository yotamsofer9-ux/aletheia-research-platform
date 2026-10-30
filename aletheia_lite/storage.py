from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CaseConfig, SourceConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredDocument:
    case_id: str
    source_id: str
    url: str
    canonical_url: str
    title: str
    published_at: str | None
    archived_at: str | None
    text: str
    content_hash: str
    fetch_method: str
    http_status: int | None
    truncated: bool
    sample_phase: str | None = None
    snapshot_ref: str | None = None
    snapshot_hash: str | None = None
    snapshot_size_bytes: int | None = None
    snapshot_media_type: str | None = None


class ResearchStore:
    """A single SQLite file with small, explicit research tables."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    case_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    discovery TEXT NOT NULL,
                    publisher_slot TEXT,
                    editorial_profile TEXT,
                    substitute_for TEXT,
                    feed_url TEXT,
                    domain TEXT,
                    PRIMARY KEY (case_id, source_id),
                    FOREIGN KEY (case_id) REFERENCES cases(case_id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    stored INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    FOREIGN KEY (case_id) REFERENCES cases(case_id)
                );

                CREATE TABLE IF NOT EXISTS run_errors (
                    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    source_id TEXT,
                    stage TEXT NOT NULL,
                    url TEXT,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    published_at TEXT,
                    archived_at TEXT,
                    fetched_at TEXT,
                    text TEXT,
                    content_hash TEXT,
                    fetch_method TEXT,
                    http_status INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    duplicate_of TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    sample_phase TEXT,
                    snapshot_ref TEXT,
                    snapshot_hash TEXT,
                    snapshot_size_bytes INTEGER,
                    snapshot_media_type TEXT,
                    run_id TEXT NOT NULL,
                    UNIQUE(case_id, canonical_url),
                    FOREIGN KEY (case_id, source_id) REFERENCES sources(case_id, source_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id),
                    FOREIGN KEY (duplicate_of) REFERENCES documents(document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_documents_case_status
                    ON documents(case_id, status);
                CREATE INDEX IF NOT EXISTS idx_documents_case_hash
                    ON documents(case_id, content_hash);

                CREATE TABLE IF NOT EXISTS measurements (
                    measurement_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, prompt_version, model_name),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id)
                );
                """
            )
            self._ensure_source_columns(connection)
            self._ensure_document_columns(connection)
            connection.commit()

    @staticmethod
    def _ensure_source_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sources)")
        }
        additions = {
            "publisher_slot": "TEXT",
            "editorial_profile": "TEXT",
            "substitute_for": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE sources ADD COLUMN {name} {data_type}")

    @staticmethod
    def _ensure_document_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")
        }
        additions = {
            "sample_phase": "TEXT",
            "snapshot_ref": "TEXT",
            "snapshot_hash": "TEXT",
            "snapshot_size_bytes": "INTEGER",
            "snapshot_media_type": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {data_type}")

    def register_case(self, config: CaseConfig) -> None:
        serializable = {
            "case_id": config.case_id,
            "title": config.title,
            "window_start": config.window_start.isoformat(),
            "window_end": config.window_end.isoformat(),
            "keywords": list(config.keywords),
            "max_documents": config.max_documents,
            "max_documents_per_source": config.max_documents_per_source,
        }
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cases (
                    case_id, title, window_start, window_end,
                    keywords_json, config_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    title=excluded.title,
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    keywords_json=excluded.keywords_json,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (
                    config.case_id,
                    config.title,
                    config.window_start.isoformat(),
                    config.window_end.isoformat(),
                    json.dumps(config.keywords, ensure_ascii=False),
                    json.dumps(serializable, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            for source in config.sources:
                self._upsert_source(connection, config.case_id, source)
            connection.commit()

    @staticmethod
    def _upsert_source(
        connection: sqlite3.Connection, case_id: str, source: SourceConfig
    ) -> None:
        connection.execute(
            """
            INSERT INTO sources (
                case_id, source_id, name, group_name, language,
                discovery, publisher_slot, editorial_profile, substitute_for,
                feed_url, domain
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id, source_id) DO UPDATE SET
                name=excluded.name,
                group_name=excluded.group_name,
                language=excluded.language,
                discovery=excluded.discovery,
                publisher_slot=excluded.publisher_slot,
                editorial_profile=excluded.editorial_profile,
                substitute_for=excluded.substitute_for,
                feed_url=excluded.feed_url,
                domain=excluded.domain
            """,
            (
                case_id,
                source.source_id,
                source.name,
                source.group,
                source.language,
                source.discovery,
                source.publisher_slot,
                source.editorial_profile,
                source.substitute_for,
                source.feed_url,
                source.domain,
            ),
        )

    def start_run(self, case_id: str) -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO runs (run_id, case_id, started_at, status) VALUES (?, ?, ?, ?)",
                (run_id, case_id, utc_now(), "RUNNING"),
            )
            connection.commit()
        return run_id

    def finish_run(self, run_id: str, summary: dict[str, int], error: str | None = None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE runs SET finished_at=?, status=?, discovered=?, stored=?,
                    duplicates=?, skipped=?, failed=?, error=?
                WHERE run_id=?
                """,
                (
                    utc_now(),
                    "FAILED" if error else "COMPLETED",
                    summary.get("discovered", 0),
                    summary.get("stored", 0),
                    summary.get("duplicates", 0),
                    summary.get("skipped", 0),
                    summary.get("failed", 0),
                    error,
                    run_id,
                ),
            )
            connection.commit()

    def record_run_error(
        self,
        run_id: str,
        stage: str,
        message: str,
        source_id: str | None = None,
        url: str | None = None,
    ) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO run_errors (
                    run_id, source_id, stage, url, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, source_id, stage, url, message[:1_000], utc_now()),
            )
            connection.commit()

    def get_existing_status(self, case_id: str, canonical_url: str) -> str | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT status FROM documents WHERE case_id=? AND canonical_url=?",
                (case_id, canonical_url),
            ).fetchone()
        return str(row["status"]) if row else None

    def activate_canonical_urls(
        self, case_id: str, canonical_urls: set[str]
    ) -> dict[str, int]:
        """Make one approved basket the exact active document set.

        Superseded basket members remain in the database as ``RETIRED`` audit
        records.  Re-approving a previously retired URL is also supported, so
        a basket revision never needs to delete research material.
        """

        if not canonical_urls:
            raise ValueError("approved basket cannot activate an empty URL set")
        with closing(self.connect()) as connection:
            placeholders = ",".join("?" for _ in canonical_urls)
            ordered = tuple(sorted(canonical_urls))
            retired = connection.execute(
                f"""
                UPDATE documents SET status='RETIRED'
                WHERE case_id=? AND status='READY'
                  AND canonical_url NOT IN ({placeholders})
                """,
                (case_id, *ordered),
            ).rowcount
            reactivated = connection.execute(
                f"""
                UPDATE documents SET status='READY', error=NULL, duplicate_of=NULL
                WHERE case_id=? AND status='RETIRED'
                  AND canonical_url IN ({placeholders})
                """,
                (case_id, *ordered),
            ).rowcount
            connection.commit()
        return {"retired": int(retired), "reactivated": int(reactivated)}

    def record_failure(
        self,
        *,
        run_id: str,
        case_id: str,
        source_id: str,
        url: str,
        canonical_url: str,
        title: str,
        published_at: str | None,
        sample_phase: str | None,
        error: str,
        http_status: int | None = None,
    ) -> None:
        document_id = f"doc_{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, case_id, source_id, url, canonical_url, title,
                    published_at, sample_phase, fetched_at, http_status, status,
                    error, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FAILED', ?, ?)
                ON CONFLICT(case_id, canonical_url) DO UPDATE SET
                    fetched_at=excluded.fetched_at,
                    http_status=excluded.http_status,
                    status='FAILED',
                    error=excluded.error,
                    run_id=excluded.run_id
                """,
                (
                    document_id,
                    case_id,
                    source_id,
                    url,
                    canonical_url,
                    title,
                    published_at,
                    sample_phase,
                    utc_now(),
                    http_status,
                    error[:1_000],
                    run_id,
                ),
            )
            connection.commit()

    def store_document(self, run_id: str, document: StoredDocument) -> str:
        document_id = f"doc_{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            duplicate = connection.execute(
                """
                SELECT document_id FROM documents
                WHERE case_id=? AND content_hash=? AND status='READY'
                ORDER BY fetched_at LIMIT 1
                """,
                (document.case_id, document.content_hash),
            ).fetchone()
            status = "DUPLICATE" if duplicate else "READY"
            duplicate_of = str(duplicate["document_id"]) if duplicate else None
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, case_id, source_id, url, canonical_url, title,
                    published_at, archived_at, fetched_at, text, content_hash,
                    fetch_method, http_status, status, error, duplicate_of,
                    truncated, sample_phase, snapshot_ref, snapshot_hash,
                    snapshot_size_bytes, snapshot_media_type, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id, canonical_url) DO UPDATE SET
                    source_id=excluded.source_id,
                    title=excluded.title,
                    published_at=excluded.published_at,
                    archived_at=excluded.archived_at,
                    fetched_at=excluded.fetched_at,
                    text=excluded.text,
                    content_hash=excluded.content_hash,
                    fetch_method=excluded.fetch_method,
                    http_status=excluded.http_status,
                    status=excluded.status,
                    error=NULL,
                    duplicate_of=excluded.duplicate_of,
                    truncated=excluded.truncated,
                    sample_phase=excluded.sample_phase,
                    snapshot_ref=excluded.snapshot_ref,
                    snapshot_hash=excluded.snapshot_hash,
                    snapshot_size_bytes=excluded.snapshot_size_bytes,
                    snapshot_media_type=excluded.snapshot_media_type,
                    run_id=excluded.run_id
                """,
                (
                    document_id,
                    document.case_id,
                    document.source_id,
                    document.url,
                    document.canonical_url,
                    document.title,
                    document.published_at,
                    document.archived_at,
                    utc_now(),
                    document.text,
                    document.content_hash,
                    document.fetch_method,
                    document.http_status,
                    status,
                    duplicate_of,
                    int(document.truncated),
                    document.sample_phase,
                    document.snapshot_ref,
                    document.snapshot_hash,
                    document.snapshot_size_bytes,
                    document.snapshot_media_type,
                    run_id,
                ),
            )
            connection.commit()
        return status

    def counts(self, case_id: str) -> dict[str, int]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total FROM documents
                WHERE case_id=? GROUP BY status
                """,
                (case_id,),
            ).fetchall()
        result = {"READY": 0, "DUPLICATE": 0, "FAILED": 0}
        result.update({str(row["status"]): int(row["total"]) for row in rows})
        return result

    def ready_count(self, case_id: str, source_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM documents WHERE case_id=? AND status='READY'"
        params: tuple[str, ...] = (case_id,)
        if source_id is not None:
            query += " AND source_id=?"
            params = (case_id, source_id)
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return int(row[0])

    def export_documents(self, case_id: str, output_path: str | Path) -> int:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT d.document_id, d.source_id, s.name AS source_name,
                    s.group_name, s.language, s.publisher_slot,
                    s.editorial_profile, s.substitute_for,
                    d.url, d.title, d.published_at,
                    d.archived_at, d.sample_phase, d.content_hash,
                    d.fetch_method, d.truncated,
                    d.snapshot_ref, d.snapshot_hash, d.snapshot_size_bytes,
                    d.snapshot_media_type, d.text
                FROM documents d
                JOIN sources s ON s.case_id=d.case_id AND s.source_id=d.source_id
                WHERE d.case_id=? AND d.status='READY'
                ORDER BY d.published_at, d.source_id
                """,
                (case_id,),
            ).fetchall()
        fieldnames = list(rows[0].keys()) if rows else [
            "document_id",
            "source_id",
            "source_name",
            "group_name",
            "language",
            "publisher_slot",
            "editorial_profile",
            "substitute_for",
            "url",
            "title",
            "published_at",
            "archived_at",
            "sample_phase",
            "content_hash",
            "fetch_method",
            "truncated",
            "snapshot_ref",
            "snapshot_hash",
            "snapshot_size_bytes",
            "snapshot_media_type",
            "text",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        return len(rows)
