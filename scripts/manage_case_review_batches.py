#!/usr/bin/env python3
"""Split and merge bounded methodological-review batches for one case."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _summary(documents: list[dict]) -> dict:
    routed = sum(len(row.get("routed_components", [])) for row in documents)
    return {
        "documents": len(documents),
        "focused_review_documents": sum(bool(row.get("routed_components")) for row in documents),
        "batch_signoff_only_documents": 0,
        "quality_audit_documents": 0,
        "routed_components": routed,
        "provisionally_accepted_components": len(documents) * 8 - routed,
    }


def split(queue_path: Path, output_dir: Path, max_dilemmas: int, case_id: str) -> None:
    queue = _read(queue_path)
    routed = [row for row in queue["documents"] if row.get("routed_components")]
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_count = 0
    for document in routed:
        count = len(document["routed_components"])
        if current and current_count + count > max_dilemmas:
            batches.append(current)
            current, current_count = [], 0
        current.append(document)
        current_count += count
    if current:
        batches.append(current)

    manifest_rows = []
    for index, documents in enumerate(batches, 1):
        prefix = f"batch_{index:02d}"
        queue_output = output_dir / f"{prefix}_queue.json"
        payload = {
            **{key: value for key, value in queue.items() if key not in {"documents", "summary"}},
            "batch_id": prefix,
            "documents": documents,
            "summary": _summary(documents),
        }
        _write(queue_output, payload)
        manifest_rows.append(
            {
                "batch_id": prefix,
                "queue": str(queue_output),
                "candidate": str(output_dir / f"{prefix}_candidate.json"),
                "dilemmas": str(output_dir / f"{prefix}_dilemmas.json"),
                "translations": str(output_dir / f"{prefix}_translations.json"),
                "documents": len(documents),
                "routed_components": payload["summary"]["routed_components"],
            }
        )
    _write(
        output_dir / "manifest.json",
        {
            "schema_version": "1.0",
            "case_id": case_id,
            "source_queue": str(queue_path),
            "max_dilemmas_per_batch": max_dilemmas,
            "batches": manifest_rows,
        },
    )
    print(json.dumps({"batches": len(batches), "routed_components": sum(row["routed_components"] for row in manifest_rows)}))


def merge(
    manifest_path: Path,
    candidate_output: Path,
    dilemmas_output: Path,
    translations_output: Path,
) -> None:
    manifest = _read(manifest_path)
    dilemmas: list[dict] = []
    resolved: list[dict] = []
    translations: list[dict] = []
    usage = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    batch_candidates = []
    packet_template = None
    translation_template = None
    for batch in manifest["batches"]:
        candidate = _read(Path(batch["candidate"]))
        packet = _read(Path(batch["dilemmas"]))
        translated = _read(Path(batch["translations"]))
        packet_template = packet_template or packet
        translation_template = translation_template or translated
        dilemmas.extend(packet["dilemmas"])
        resolved.extend(packet.get("resolved_without_dispute", []))
        translations.extend(translated["records"])
        for key in usage:
            usage[key] += int((candidate.get("usage") or {}).get(key) or 0)
        batch_candidates.append(batch["candidate"])
    ids = [row["dilemma_id"] for row in dilemmas]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate merged dilemma IDs")
    translation_ids = [row["translation_id"] for row in translations]
    if len(translation_ids) != len(set(translation_ids)):
        raise ValueError("duplicate merged translation IDs")
    if packet_template is None or translation_template is None:
        raise ValueError("no completed review batches")
    _write(
        candidate_output,
        {
            "schema_version": "1.0",
            "case_id": manifest.get("case_id"),
            "generation_mode": "BOUNDED_BATCHES",
            "batch_candidates": batch_candidates,
            "usage": usage,
        },
    )
    _write(
        dilemmas_output,
        {
            **packet_template,
            "dilemmas": dilemmas,
            "resolved_without_dispute": resolved,
        },
    )
    _write(translations_output, {**translation_template, "records": translations})
    print(json.dumps({"dilemmas": len(dilemmas), "resolved_without_dispute": len(resolved), "translations": len(translations), "usage": usage}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    split_parser = sub.add_parser("split")
    split_parser.add_argument("--queue", type=Path, required=True)
    split_parser.add_argument("--output-dir", type=Path, required=True)
    split_parser.add_argument("--max-dilemmas", type=int, default=8)
    split_parser.add_argument("--case-id", required=True)
    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--manifest", type=Path, required=True)
    merge_parser.add_argument("--candidate-output", type=Path, required=True)
    merge_parser.add_argument("--dilemmas-output", type=Path, required=True)
    merge_parser.add_argument("--translations-output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "split":
        split(args.queue, args.output_dir, args.max_dilemmas, args.case_id)
    else:
        merge(args.manifest, args.candidate_output, args.dilemmas_output, args.translations_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
