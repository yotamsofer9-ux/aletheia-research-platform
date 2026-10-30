from __future__ import annotations

import gzip
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SnapshotReceipt:
    ref: str
    sha256: str
    size_bytes: int
    media_type: str


class SnapshotStore(Protocol):
    def put(
        self,
        *,
        case_id: str,
        source_id: str,
        content: bytes,
        media_type: str,
    ) -> SnapshotReceipt: ...


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


class LocalSnapshotStore:
    """Content-addressed gzip snapshots; replaceable later by a GCS backend."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(
        self,
        *,
        case_id: str,
        source_id: str,
        content: bytes,
        media_type: str,
    ) -> SnapshotReceipt:
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(_safe_segment(case_id)) / _safe_segment(source_id) / f"{digest}.gz"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            compressed = gzip.compress(content, compresslevel=6, mtime=0)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(compressed)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return SnapshotReceipt(
            ref=f"local-snapshot://{relative.as_posix()}",
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
        )

    def read(self, ref: str) -> bytes:
        prefix = "local-snapshot://"
        if not ref.startswith(prefix):
            raise ValueError("Not a local snapshot reference")
        relative = Path(ref[len(prefix) :])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe snapshot reference")
        return gzip.decompress((self.root / relative).read_bytes())
