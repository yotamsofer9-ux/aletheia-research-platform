#!/usr/bin/env python3
"""Run the two-pass pre-Gate-2 primary-topic relevance audit."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_workspace import CaseWorkspace
from aletheia_lite.topic_relevance import (
    adjudicate_existing_audit_with_vertex,
    audit_basket_with_vertex,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--resume-audit",
        type=Path,
        help="Adjudicate only HUMAN_REVIEW rows from an existing exact-basket audit.",
    )
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument(
        "--workspace-current",
        action="store_true",
        help="Use current same-article repaired snapshots from the case workspace.",
    )
    parser.add_argument(
        "--article-id",
        action="append",
        help="Audit only the exact listed basket article IDs (repeatable).",
    )
    args = parser.parse_args()
    registry = CaseRegistry(ROOT / "data/case_registry.sqlite3")
    case = registry.get_case(args.case_id)
    basket = registry.get_basket(args.case_id)
    snapshot_base = None
    if args.workspace_current:
        workspace = CaseWorkspace(ROOT, args.case_id)
        connection = sqlite3.connect(workspace.research_db)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT canonical_url, snapshot_ref, snapshot_hash
                FROM documents
                WHERE case_id = ? AND status = 'READY'
                """,
                (args.case_id,),
            ).fetchall()
        finally:
            connection.close()
        current = {str(row["canonical_url"]): row for row in rows}
        basket = copy.deepcopy(basket)
        for article in basket["articles"]:
            row = current.get(str(article["canonical_url"]))
            if row is None:
                raise ValueError(
                    f"workspace lacks current basket article: {article['article_id']}"
                )
            article["snapshot_ref"] = str(row["snapshot_ref"])
            article["snapshot_hash"] = str(row["snapshot_hash"])
        snapshot_base = workspace.snapshot_root
    if args.article_id:
        requested = list(dict.fromkeys(args.article_id))
        by_id = {
            str(article["article_id"]): article for article in basket["articles"]
        }
        missing = [article_id for article_id in requested if article_id not in by_id]
        if missing:
            raise ValueError(f"unknown basket article IDs: {', '.join(missing)}")
        basket = copy.deepcopy(basket)
        basket["articles"] = [by_id[article_id] for article_id in requested]
    output = args.output or (
        ROOT
        / "analysis/case_generator"
        / args.case_id.lower()
        / "topic_relevance_audit_v2.json"
    )
    if not output.is_absolute():
        output = ROOT / output
    if args.resume_audit:
        resume_path = (
            args.resume_audit
            if args.resume_audit.is_absolute()
            else ROOT / args.resume_audit
        )
        prior_audit = json.loads(resume_path.read_text(encoding="utf-8"))
        audit = adjudicate_existing_audit_with_vertex(
            case["manifest"],
            basket,
            prior_audit,
            staging_root=ROOT / "data/case_staging",
            batch_size=args.batch_size,
            snapshot_base=snapshot_base,
        )
    else:
        audit = audit_basket_with_vertex(
            case["manifest"],
            basket,
            staging_root=ROOT / "data/case_staging",
            batch_size=args.batch_size,
            snapshot_base=snapshot_base,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "case_id": args.case_id,
                "status": audit["status"],
                "summary": audit["summary"],
                "model_calls": audit["model_calls"],
                "output": str(output.relative_to(ROOT)),
            },
            ensure_ascii=False,
        )
    )
    return 2 if args.enforce and audit["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
