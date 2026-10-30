from __future__ import annotations

import json
import hashlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT
    / "case_generator"
    / "schemas"
    / "case_generator_contracts_v1.schema.json"
)

FORBIDDEN_SELECTION_FIELDS = {
    "t",
    "n",
    "a",
    "t_score",
    "n_score",
    "a_score",
    "truth_score",
    "narrative_score",
    "aletheia_score",
    "component_scores",
}


class CaseGeneratorContractError(ValueError):
    """Raised when a case-generator payload violates a frozen contract rule."""


def stable_content_hash(value: Mapping[str, Any], *, exclude: set[str]) -> str:
    stable = {key: child for key, child in value.items() if key not in exclude}
    encoded = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_content_hash(candidate: Mapping[str, Any]) -> str:
    return stable_content_hash(candidate, exclude={"candidate_hash", "state"})


def basket_content_hash(basket: Mapping[str, Any]) -> str:
    return stable_content_hash(basket, exclude={"basket_hash", "status"})


def basket_article_payload_changed(
    previous: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Return whether a new version materially changes the article payload.

    Basket versions, timestamps and change-reason text are audit metadata.  They
    must not make an unchanged article basket look like a successful discovery
    pass at Gate 2.
    """

    return json.dumps(
        previous.get("articles") or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) != json.dumps(
        candidate.get("articles") or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_contract_schema(path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_case_generator_fixture(
    payload: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
) -> None:
    """Validate JSON structure and the cross-record rules JSON Schema cannot express."""

    _reject_selection_scores(payload)

    active_schema = dict(schema) if schema is not None else load_contract_schema()
    validator = Draft202012Validator(active_schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise CaseGeneratorContractError(f"schema violation at {location}: {first.message}")

    candidate = payload["candidate"]
    decision = payload["topic_decision"]
    manifest = payload["manifest"]
    basket = payload["basket"]

    if candidate["candidate_hash"] != candidate_content_hash(candidate):
        raise CaseGeneratorContractError("candidate_hash does not match candidate content")
    if basket["basket_hash"] != basket_content_hash(basket):
        raise CaseGeneratorContractError("basket_hash does not match basket content")

    if decision["gate"] != "TOPIC" or decision["decision"] != "APPROVE":
        raise CaseGeneratorContractError("fixture must contain an approved topic gate")
    if decision["target_hash"] != candidate["candidate_hash"]:
        raise CaseGeneratorContractError("topic decision is not bound to candidate hash")
    if manifest["topic_approval_hash"] != candidate["candidate_hash"]:
        raise CaseGeneratorContractError("manifest is not bound to approved candidate hash")
    if manifest["candidate_id"] != candidate["candidate_id"]:
        raise CaseGeneratorContractError("manifest candidate_id does not match candidate")
    if decision["case_id"] != manifest["case_id"]:
        raise CaseGeneratorContractError("topic decision case_id does not match manifest")
    if basket["case_id"] != manifest["case_id"]:
        raise CaseGeneratorContractError("basket case_id does not match manifest")

    validate_basket_contract(basket, manifest)


def validate_basket_contract(
    basket: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    articles = list(basket["articles"])
    minimum = int(manifest["minimum_articles"])
    target = int(manifest["target_articles"])
    if not minimum <= len(articles) <= target:
        raise CaseGeneratorContractError(
            f"basket must contain {minimum}–{target} articles; found {len(articles)}"
        )

    for field in ("article_id", "canonical_url", "snapshot_hash"):
        values = [article[field] for article in articles]
        if len(values) != len(set(values)):
            raise CaseGeneratorContractError(f"duplicate {field} in basket")

    language_counts = Counter(article["language"] for article in articles)
    if set(language_counts) != {"he", "en", "ar"}:
        raise CaseGeneratorContractError("basket must contain Hebrew, English and Arabic")

    by_slot: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for article in articles:
        by_slot[article["publisher_slot"]].append(article)

    for slot, slot_articles in by_slot.items():
        source_counts = Counter(article["source_id"] for article in slot_articles)
        if len(source_counts) > 2:
            raise CaseGeneratorContractError(
                f"publisher slot {slot} mixes more than two outlets"
            )
        if len(source_counts) == 2:
            if sorted(source_counts.values()) != [1, 2]:
                raise CaseGeneratorContractError(
                    f"publisher slot {slot} must use a 2+1 replacement pattern"
                )
            profiles = {article["editorial_profile"] for article in slot_articles}
            if len(profiles) != 1:
                raise CaseGeneratorContractError(
                    f"publisher slot {slot} replacement has a different editorial profile"
                )
            replacements = [
                article
                for article in slot_articles
                if article["replacement_kind"] == "SIMILAR_OUTLET"
            ]
            # A slot may contain two same-profile substitute outlets when the
            # primary is unavailable (for example a 2+1 split across two
            # public replacements). Every substituted article stays visible
            # for explicit Gate-2 approval.
            if len(replacements) not in {1, 2, 3} or any(
                not article["requires_human_approval"] for article in replacements
            ):
                raise CaseGeneratorContractError(
                    f"publisher slot {slot} 2+1 replacement is not fully flagged for human approval"
                )


def _reject_selection_scores(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_SELECTION_FIELDS:
                location = ".".join((*path, str(key)))
                raise CaseGeneratorContractError(
                    f"selection payload contains forbidden score field: {location}"
                )
            _reject_selection_scores(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_selection_scores(child, (*path, str(index)))
