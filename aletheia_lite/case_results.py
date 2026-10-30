from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from .judgment_room import CaseMaterial
from .research_workspace import load_corpus_index


PHASE_ORDER = {
    "INITIAL_REPORT": 0,
    "EARLY_UPDATE": 1,
    "LATER_INVESTIGATION": 2,
}
PHASE_LABELS_HE = {
    "INITIAL_REPORT": "דיווח ראשוני",
    "EARLY_UPDATE": "עדכון מוקדם",
    "LATER_INVESTIGATION": "בדיקה מאוחרת",
}
LANGUAGE_LABELS_HE = {"he": "עברית", "en": "אנגלית", "ar": "ערבית"}
TRUTH_WEIGHTS = {"T1": 25.0, "T2": 15.0, "T3": 15.0, "T4": 10.0}
TRUTH_WEIGHT = sum(TRUTH_WEIGHTS.values())


@dataclass(frozen=True)
class ScoreRecord:
    row_id: str
    case_id: str
    level: str
    entity_id: str
    language: str | None
    label: str
    truth_score: float | None
    narrative_pressure: float | None
    aletheia_65_35: float | None
    aletheia_50_50: float | None
    aletheia_75_25: float | None
    truth_coverage: float
    narrative_coverage: float
    overall_coverage: float
    critical_flags: list[str]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _weighted_component_result(
    scores: dict[str, int | None], weights: dict[str, float]
) -> tuple[float | None, float, float, float]:
    numerator = 0.0
    available = 0.0
    for component, weight in weights.items():
        score = scores.get(component)
        if score is None:
            continue
        numerator += weight * (score / 4.0)
        available += weight
    result = None if available == 0 else 100.0 * numerator / available
    coverage = 0.0 if not weights else available / sum(weights.values())
    return result, coverage, numerator, available


def _critical_flags(rows: Iterable[dict[str, Any]]) -> list[str]:
    flags: set[str] = set()
    for row in rows:
        component = row["component"]
        score = row.get("effective_score")
        if component == "T1" and score == 0:
            flags.add("CRITICAL_FACT_CONFLICT")
        elif component == "T2" and score == 0:
            flags.add("SOURCE_INTEGRITY_FAILURE")
        elif component == "N2" and score == 4:
            flags.add("EXTREME_CAUSAL_OVERRIDE")
        elif component == "N5" and score == 4:
            flags.add("DEHUMANIZING_OR_INCITING_LANGUAGE")
        if (
            row.get("model_confidence") == "LOW"
            and component in {"T1", "T2", "N1", "N2"}
        ):
            flags.add("LOW_CONFIDENCE")
    return sorted(flags)


def load_t4_candidates(
    root: Path, candidate_path: Path | None = None
) -> dict[str, dict[str, Any]]:
    path = candidate_path or (root / "calibration/case001_t4_candidates_v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["trajectory_id"]: item for item in payload["trajectories"]}


def build_t4_trajectories(
    root: Path,
    material: CaseMaterial,
    effective_rows: list[dict[str, Any]],
    *,
    corpus: list[dict[str, Any]] | None = None,
    candidates: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the nine frozen publisher-slot trajectories for human T4 review."""
    corpus = corpus or load_corpus_index(root)
    metadata = {row["document_id"]: row for row in corpus}
    components_by_document: dict[str, dict[str, dict[str, Any]]] = {}
    for document in material.documents:
        components_by_document[document["document_id"]] = {
            item["component"]: item for item in document["components"]
        }
    rows_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in effective_rows:
        rows_by_document[row["document_id"]].append(row)
    candidates = candidates or load_t4_candidates(root)

    slots: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in corpus:
        slots[(row["language"], row["publisher_slot"])].append(row)
    if len(slots) != 9:
        raise ValueError(f"Expected nine publisher slots, found {len(slots)}")

    trajectories: list[dict[str, Any]] = []
    for (language, publisher_slot), documents in sorted(slots.items()):
        documents.sort(key=lambda item: PHASE_ORDER[item["sample_phase"]])
        if [item["sample_phase"] for item in documents] != list(PHASE_ORDER):
            raise ValueError(f"Incomplete trajectory: {publisher_slot}")
        source_names = sorted({item["source_name"] for item in documents})
        trajectory_id = f"{material.case_id}:T4:{publisher_slot}"
        legacy_trajectory_id = (
            f"{material.case_id.replace('-', '')}:T4:{publisher_slot}"
        )
        candidate = candidates.get(trajectory_id)
        if candidate is None and legacy_trajectory_id in candidates:
            trajectory_id = legacy_trajectory_id
            candidate = candidates[trajectory_id]
        if candidate is None:
            raise ValueError(f"Missing T4 candidate: {trajectory_id}")
        articles = []
        for document in documents:
            document_id = document["document_id"]
            effective = {
                item["component"]: item for item in rows_by_document[document_id]
            }
            model_components = components_by_document.get(document_id, {})
            focus = {}
            for component in ("T1", "T3"):
                score_row = effective[component]
                model_component = model_components.get(component, {})
                focus[component] = {
                    "score": score_row["effective_score"],
                    "authority": score_row["authority"],
                    "rationale": score_row["effective_rationale"],
                    "evidence_span": model_component.get("evidence_span"),
                }
            articles.append({
                "document_id": document_id,
                "source_name": document["source_name"],
                "title": document["title"],
                "published_at": document["published_at"],
                "phase": document["sample_phase"],
                "phase_he": PHASE_LABELS_HE[document["sample_phase"]],
                "focus_components": focus,
            })
        split_slot = len(source_names) > 1
        trajectories.append({
            "schema_version": "1.0",
            "trajectory_id": trajectory_id,
            "case_id": material.case_id,
            "publisher_slot": publisher_slot,
            "language": language,
            "language_he": LANGUAGE_LABELS_HE[language],
            "source_names": source_names,
            "split_slot": split_slot,
            "decision_required": not split_slot,
            "articles": articles,
            "proposal": candidate,
        })
    return trajectories


def _average(values: Iterable[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return None if not available else sum(available) / len(available)


def _tf_critical_flags(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Return only evidence-fidelity flags allowed by TF-2D/1.0."""

    flags: set[str] = set()
    for row in rows:
        component = row["component"]
        score = row.get("effective_score")
        if component == "T1" and score == 0:
            flags.add("CRITICAL_FACT_CONFLICT")
        elif component == "T2" and score == 0:
            flags.add("SOURCE_INTEGRITY_FAILURE")
        if (
            component in TRUTH_WEIGHTS
            and row.get("model_confidence") == "LOW"
            and TRUTH_WEIGHTS[component] >= 9
        ):
            flags.add("LOW_CONFIDENCE")
    return sorted(flags)


def build_tf_score_records(
    root: Path,
    material: CaseMaterial,
    effective_rows: list[dict[str, Any]],
    trajectory_decisions: dict[str, dict[str, Any]],
    frame_results: dict[str, Any],
    *,
    corpus: list[dict[str, Any]] | None = None,
    case_label: str | None = None,
) -> list[ScoreRecord]:
    """Build the active TF-2D result without aggregating N or calculating A.

    The legacy ``ScoreRecord`` envelope is retained for storage and dashboard
    compatibility.  Its narrative and A fields are deliberately null, while
    F remains an independent value inside ``details.frame_intensity`` and the
    canonical frame-results artifact.
    """

    corpus = corpus or load_corpus_index(root)
    metadata = {row["document_id"]: row for row in corpus}
    rows_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in effective_rows:
        rows_by_document[row["document_id"]].append(row)
    if len(rows_by_document) != 27 or any(
        len(rows) != 8 for rows in rows_by_document.values()
    ):
        raise ValueError("TF-2D scoring requires 27 documents with eight effective components")
    if (
        frame_results.get("status") != "COMPLETE"
        or frame_results.get("documents") != 27
        or frame_results.get("unresolved_human_reviews") != 0
    ):
        raise ValueError("TF-2D scoring requires complete frame results")
    frame_by_document = {
        row["document_id"]: row for row in frame_results.get("records", [])
    }
    if set(frame_by_document) != set(rows_by_document):
        raise ValueError("TF-2D T and F document rosters do not match")

    records: list[ScoreRecord] = []
    document_records: dict[str, ScoreRecord] = {}
    for document_id in sorted(rows_by_document):
        rows = rows_by_document[document_id]
        by_component = {row["component"]: row for row in rows}
        truth_scores = {
            component: by_component[component].get("effective_score")
            for component in ("T1", "T2", "T3")
        }
        truth, truth_coverage, _, _ = _weighted_component_result(
            truth_scores,
            {component: TRUTH_WEIGHTS[component] for component in truth_scores},
        )
        frame_record = frame_by_document[document_id]
        record = ScoreRecord(
            row_id=f"{material.case_id}:DOCUMENT:{document_id}",
            case_id=material.case_id,
            level="DOCUMENT",
            entity_id=document_id,
            language=metadata[document_id]["language"],
            label=metadata[document_id]["title"],
            truth_score=_round(truth),
            narrative_pressure=None,
            aletheia_65_35=None,
            aletheia_50_50=None,
            aletheia_75_25=None,
            truth_coverage=_round(truth_coverage) or 0.0,
            narrative_coverage=0.0,
            overall_coverage=_round(truth_coverage) or 0.0,
            critical_flags=_tf_critical_flags(rows),
            details={
                "methodology_version": "TF-2D/1.0",
                "source_name": metadata[document_id]["source_name"],
                "publisher_slot": metadata[document_id]["publisher_slot"],
                "phase": metadata[document_id]["sample_phase"],
                "published_at": metadata[document_id]["published_at"],
                "component_scores": truth_scores,
                "diagnostic_n_components": {
                    component: by_component[component].get("effective_score")
                    for component in ("N1", "N2", "N3", "N4", "N5")
                },
                "frame_intensity": {
                    "score": frame_record["frame_score"],
                    "coverage": 1.0,
                    "component_scores": frame_record["component_scores"],
                },
                "t4_included": False,
            },
        )
        records.append(record)
        document_records[document_id] = record

    by_slot: dict[tuple[str, str], list[str]] = defaultdict(list)
    for document in corpus:
        by_slot[(document["language"], document["publisher_slot"])].append(
            document["document_id"]
        )
    slot_records: list[ScoreRecord] = []
    for (language, publisher_slot), document_ids in sorted(by_slot.items()):
        if len(document_ids) != 3:
            raise ValueError(f"Slot does not contain three documents: {publisher_slot}")
        trajectory_id = f"{material.case_id}:T4:{publisher_slot}"
        legacy_id = f"{material.case_id.replace('-', '')}:T4:{publisher_slot}"
        t4 = trajectory_decisions.get(trajectory_id) or trajectory_decisions.get(legacy_id)
        if t4 is None:
            raise ValueError(f"Missing T4 decision: {trajectory_id}")

        numerator = 0.0
        available = 0.0
        component_means: dict[str, float | None] = {}
        for component in ("T1", "T2", "T3"):
            values = [
                document_records[document_id].details["component_scores"][component]
                for document_id in document_ids
                if document_records[document_id].details["component_scores"][component]
                is not None
            ]
            mean_score = _average(values)
            component_means[component] = _round(mean_score)
            if mean_score is not None:
                numerator += TRUTH_WEIGHTS[component] * (mean_score / 4.0)
                available += TRUTH_WEIGHTS[component]
        component_means["T4"] = t4["score"]
        if t4["score"] is not None:
            numerator += TRUTH_WEIGHTS["T4"] * (t4["score"] / 4.0)
            available += TRUTH_WEIGHTS["T4"]
        truth = None if not available else 100.0 * numerator / available
        truth_coverage = available / TRUTH_WEIGHT
        frame_score = _average(
            frame_by_document[document_id]["frame_score"]
            for document_id in document_ids
        )
        children = [document_records[document_id] for document_id in document_ids]
        record = ScoreRecord(
            row_id=f"{material.case_id}:PUBLISHER_SLOT:{publisher_slot}",
            case_id=material.case_id,
            level="PUBLISHER_SLOT",
            entity_id=publisher_slot,
            language=language,
            label=" / ".join(sorted({metadata[item]["source_name"] for item in document_ids})),
            truth_score=_round(truth),
            narrative_pressure=None,
            aletheia_65_35=None,
            aletheia_50_50=None,
            aletheia_75_25=None,
            truth_coverage=_round(truth_coverage) or 0.0,
            narrative_coverage=0.0,
            overall_coverage=_round(truth_coverage) or 0.0,
            critical_flags=sorted({flag for child in children for flag in child.critical_flags}),
            details={
                "methodology_version": "TF-2D/1.0",
                "document_ids": sorted(document_ids),
                "component_means": component_means,
                "frame_intensity": {"score": _round(frame_score), "coverage": 1.0},
                "t4_decision_id": t4["decision_id"],
                "t4_na_reason": t4["na_reason"],
            },
        )
        records.append(record)
        slot_records.append(record)

    stratum_records: list[ScoreRecord] = []
    for language in ("he", "en", "ar"):
        children = [record for record in slot_records if record.language == language]
        if len(children) != 3:
            raise ValueError(f"Expected three slots for language {language}")
        truth = _average(record.truth_score for record in children)
        frame_score = _average(
            record.details["frame_intensity"]["score"] for record in children
        )
        truth_coverage = _average(record.truth_coverage for record in children)
        record = ScoreRecord(
            row_id=f"{material.case_id}:STRATUM:{language}",
            case_id=material.case_id,
            level="STRATUM",
            entity_id=language,
            language=language,
            label=LANGUAGE_LABELS_HE[language],
            truth_score=_round(truth),
            narrative_pressure=None,
            aletheia_65_35=None,
            aletheia_50_50=None,
            aletheia_75_25=None,
            truth_coverage=_round(truth_coverage) or 0.0,
            narrative_coverage=0.0,
            overall_coverage=_round(truth_coverage) or 0.0,
            critical_flags=sorted({flag for child in children for flag in child.critical_flags}),
            details={
                "methodology_version": "TF-2D/1.0",
                "publisher_slots": [child.entity_id for child in children],
                "frame_intensity": {"score": _round(frame_score), "coverage": 1.0},
            },
        )
        records.append(record)
        stratum_records.append(record)

    truth = _average(record.truth_score for record in stratum_records)
    frame_score = _average(
        record.details["frame_intensity"]["score"] for record in stratum_records
    )
    truth_coverage = _average(record.truth_coverage for record in stratum_records)
    records.append(
        ScoreRecord(
            row_id=f"{material.case_id}:CASE:{material.case_id}",
            case_id=material.case_id,
            level="CASE",
            entity_id=material.case_id,
            language=None,
            label=case_label or material.case_id,
            truth_score=_round(truth),
            narrative_pressure=None,
            aletheia_65_35=None,
            aletheia_50_50=None,
            aletheia_75_25=None,
            truth_coverage=_round(truth_coverage) or 0.0,
            narrative_coverage=0.0,
            overall_coverage=_round(truth_coverage) or 0.0,
            critical_flags=sorted(
                {flag for child in stratum_records for flag in child.critical_flags}
            ),
            details={
                "methodology_version": "TF-2D/1.0",
                "strata": [record.entity_id for record in stratum_records],
                "frame_intensity": {"score": _round(frame_score), "coverage": 1.0},
            },
        )
    )
    return records


def build_scoring_readiness(
    material: CaseMaterial,
    effective_rows: list[dict[str, Any]],
    trajectories: list[dict[str, Any]],
    trajectory_decisions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    effective_keys = {
        (row["document_id"], row["component"])
        for row in effective_rows
        if row["status"] != "PENDING"
    }
    required_ids = {item["trajectory_id"] for item in trajectories}
    pending_ids = sorted(required_ids - set(trajectory_decisions))
    return {
        "schema_version": "1.0",
        "case_id": material.case_id,
        "status": "READY" if len(effective_keys) == 216 and not pending_ids else "PENDING_T4",
        "effective_article_components": len(effective_keys),
        "expected_article_components": 216,
        "trajectory_decisions": len(required_ids & set(trajectory_decisions)),
        "expected_trajectory_decisions": 9,
        "pending_trajectory_ids": pending_ids,
        "human_article_decisions": sum(
            row["authority"] == "HUMAN_DECISION" for row in effective_rows
        ),
        "provisional_model_components": sum(
            row["authority"] == "MODEL_PROVISIONAL" for row in effective_rows
        ),
        "critical_flags": _critical_flags(effective_rows),
    }
