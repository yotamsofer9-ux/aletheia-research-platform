#!/usr/bin/env python3
"""Consolidate exact-basket topicality audits and verified same-article repairs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_registry import CaseRegistry  # noqa: E402
from aletheia_lite.topic_relevance import (  # noqa: E402
    AUDIT_VERSION,
    _cost_estimate,
    _summary_status,
    topic_scope_from_manifest,
)


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    registry = CaseRegistry(ROOT / "data/case_registry.sqlite3")
    case = registry.get_case(args.case_id)
    basket = registry.get_basket(args.case_id)
    current_ids = [str(article["article_id"]) for article in basket["articles"]]
    current_set = set(current_ids)
    rows_by_id: dict[str, dict] = {}
    sources: list[dict] = []
    usage = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    model_calls = 0
    model = None
    for source_arg in args.source:
        source = _resolve(source_arg)
        raw = source.read_bytes()
        audit = json.loads(raw)
        if audit.get("case_id") != args.case_id:
            raise ValueError(f"audit targets another case: {source}")
        for row in audit.get("articles") or []:
            article_id = str(row["article_id"])
            if article_id in current_set:
                rows_by_id[article_id] = row
        for key in usage:
            usage[key] += int((audit.get("usage") or {}).get(key) or 0)
        model_calls += int(audit.get("model_calls") or 0)
        model = audit.get("model") or model
        sources.append(
            {
                "path": str(source.relative_to(ROOT)),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "basket_hash": audit.get("basket_hash"),
            }
        )

    missing = [article_id for article_id in current_ids if article_id not in rows_by_id]
    if missing:
        raise ValueError(f"current basket lacks topicality results: {', '.join(missing)}")
    rows = [rows_by_id[article_id] for article_id in current_ids]
    counts, status = _summary_status(rows)
    snapshot_set_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "article_id": article["article_id"],
                    "snapshot_hash": article["snapshot_hash"],
                }
                for article in basket["articles"]
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    output_payload = {
        "schema_version": "1.0",
        "audit_version": AUDIT_VERSION,
        "case_id": args.case_id,
        "basket_id": basket["basket_id"],
        "basket_hash": basket["basket_hash"],
        "snapshot_set_hash": snapshot_set_hash,
        "status": status,
        "scope": topic_scope_from_manifest(case["manifest"]),
        "articles": rows,
        "summary": {"articles": len(rows), **counts},
        "model": model,
        "model_calls": model_calls,
        "usage": usage,
        "cost_estimate": _cost_estimate(usage),
        "consolidation": {
            "kind": "EXACT_CURRENT_BASKET_WITH_VERIFIED_SAME_ARTICLE_REPAIRS",
            "sources": sources,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    output = _resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "case_id": args.case_id,
                "basket_hash": basket["basket_hash"],
                "status": status,
                "summary": output_payload["summary"],
                "output": str(output.relative_to(ROOT)),
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
