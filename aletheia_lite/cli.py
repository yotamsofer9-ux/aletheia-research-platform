from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

from .config import ALLOWED_SAMPLE_PHASES, load_case_config
from .manual_import import import_local_file
from .pipeline import CollectionPipeline
from .storage import ResearchStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aletheia Lite research pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect documents for a case")
    collect.add_argument("--config", required=True)
    collect.add_argument("--db", default="data/aletheia_lite.db")
    collect.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry URLs previously recorded as failed",
    )
    collect.add_argument(
        "--source",
        action="append",
        dest="source_ids",
        help="Limit the run to a source_id; may be repeated",
    )

    stats = subparsers.add_parser("stats", help="Show collection counts")
    stats.add_argument("--config", required=True)
    stats.add_argument("--db", default="data/aletheia_lite.db")

    export = subparsers.add_parser("export", help="Export ready documents to CSV")
    export.add_argument("--config", required=True)
    export.add_argument("--db", default="data/aletheia_lite.db")
    export.add_argument("--output", required=True)

    import_file = subparsers.add_parser(
        "import-file", help="Import a human-reviewed local HTML or text file"
    )
    import_file.add_argument("--config", required=True)
    import_file.add_argument("--db", default="data/aletheia_lite.db")
    import_file.add_argument("--source", required=True, dest="source_id")
    import_file.add_argument("--url", required=True)
    import_file.add_argument("--file", required=True)
    import_file.add_argument("--title", default="")
    import_file.add_argument("--published-at")
    import_file.add_argument(
        "--sample-phase",
        choices=sorted(ALLOWED_SAMPLE_PHASES),
    )
    import_file.add_argument("--format", choices=("auto", "html", "text"), default="auto")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_case_config(args.config)
    store = ResearchStore(args.db)
    if args.command == "collect":
        if args.source_ids:
            selected = tuple(
                source for source in config.sources if source.source_id in args.source_ids
            )
            missing = sorted(set(args.source_ids) - {source.source_id for source in selected})
            if missing:
                raise SystemExit(f"Unknown source_id: {', '.join(missing)}")
            config = replace(config, sources=selected)
        summary = CollectionPipeline(
            config,
            store,
            retry_failed=args.retry_failed,
        ).run()
        print(json.dumps(asdict(summary), indent=2, sort_keys=True))
        return 0
    if args.command == "stats":
        print(json.dumps(store.counts(config.case_id), indent=2, sort_keys=True))
        return 0
    if args.command == "export":
        count = store.export_documents(config.case_id, args.output)
        print(json.dumps({"exported": count, "output": args.output}, indent=2))
        return 0
    if args.command == "import-file":
        source = next(
            (item for item in config.sources if item.source_id == args.source_id),
            None,
        )
        if source is None:
            raise SystemExit(f"Unknown source_id: {args.source_id}")
        run_id, status = import_local_file(
            config=config,
            store=store,
            source=source,
            url=args.url,
            path=args.file,
            title=args.title,
            published_at=args.published_at,
            sample_phase=args.sample_phase,
            file_format=args.format,
        )
        print(json.dumps({"run_id": run_id, "status": status}, indent=2))
        return 0
    return 2
