"""Human adjudication layer for the lightweight Aletheia research workflow."""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


COMPONENT_LABELS = {
    "T1": "התאמת טענות לעוגנים",
    "T2": "שקיפות מקורות וראיות",
    "T3": "נאמנות לאי־ודאות",
    "T4": "תיקון ועדכון לאורך זמן",
    "N1": "השמטה או בולטות סלקטיבית",
    "N2": "סגירה מוקדמת של סיבה או אחריות",
    "N3": "טיפול בראיות־נגד עובדתיות",
    "N4": "כותרת וליד מול גוף הכתבה",
    "N5": "טעינה רגשית או מוסרית",
    "F1": "בולטות הגדרת הבעיה",
    "F2": "ארגון סוכנות, סיבתיות ואחריות",
    "F3": "הערכה מוסרית ולגיטימציה",
    "F4": "כיוון פעולה או תכלית אסטרטגית",
    "F5": "חיזוק מבני ורטורי",
}

SCORE_LADDERS: dict[str, list[tuple[str, str]]] = {
    "T1": [
        ("4", "כל הטענות המרכזיות שאומצו תואמות לעוגנים; ייחוסים מדויקים ומחלוקות נשמרות כמחלוקות."),
        ("3", "התמונה המרכזית תואמת, עם אי־דיוק קטן שאינו מהותי."),
        ("2", "תמונה מעורבת: טענה מהותית חלשה או מיושנת, אך לא נסתרה בבירור."),
        ("1", "טענה מרכזית שאומצה מתנגשת בראיה מהותית, או מחלוקת מוצגת למעשה כעובדה."),
        ("0", "סתירה מרכזית חמורה, המצאה, או טענה שהופרכה ומוצגת כעובדה."),
        ("N/A", "אין עוגן פעיל ותואם לטענה המרכזית שאומצה או לייחוס המרכזי."),
    ],
    "T2": [
        ("4", "זהות או תפקיד מדויק, סוג הראיה, שרשרת ההיסק והמגבלות המהותיות גלויים במידה מספקת."),
        ("3", "רוב הטענות ניתנות למעקב; נותר פער קטן אחד."),
        ("2", "ייחוס מעורב, עקיף או עמום, או שלא ניתן לשחזר את השרשרת והמגבלות המכריעות."),
        ("1", "הטענה המרכזית נשענת על דיווח עמום או סמכות וכמעט שאינה ניתנת למעקב."),
        ("0", "ייחוס כוזב, הלבנת מקור, המצאה, או טענה מרכזית בלתי־נתמכת כידיעה מערכתית."),
        ("N/A", "אין טענה עובדתית חיצונית שמצריכה ייחוס."),
    ],
    "T3": [
        ("4", "ידוע, נטען, מוערך, שנוי במחלוקת ולא־ידוע נותרים מובחנים בעקביות."),
        ("3", "הכיול נכון ברובו; קיימת הגזמה קלה ולא מהותית."),
        ("2", "אי־הוודאות מוזכרת, אך מחלוקת מהותית נדחסת או מיטשטשת."),
        ("1", "טענה מרכזית שבמחלוקת מנוסחת בוודאות גבוהה, או מידע מבוסס מוקטן באופן מטעה להשערה."),
        ("0", "ודאות קטגורית מוחקת אי־ודאות מרכזית ידועה."),
        ("N/A", "אין טענה שנושאת אי־ודאות."),
    ],
    "N1": [
        ("0", "הראיות המהותיות מקבלות משקל יחסי מתאים; אין השמטה שמשנה את התמונה."),
        ("1", "הטיית בולטות קלה, אך התמונה המהותית עדיין זמינה."),
        ("2", "ראיית־נגד מהותית מוצנעת או מקוצרת ויוצרת הטיה ניכרת."),
        ("3", "מגבלה או ראיית־נגד מרכזית וזמינה הושמטה או נקברה ומשנה מהותית את ההבנה."),
        ("4", "בחירה שיטתית מבליטה תמיכה ומסתירה מידע נגדי מרכזי."),
        ("N/A", "לא הייתה ראיית־נגד מהותית זמינה, או שהסוג וההיקף אינם יוצרים ציפייה סבירה לכללה."),
    ],
    "N2": [
        ("0", "עמדת הסיבה או האחריות תואמת לראיות, לייחוס ולהסתייגויות הנדרשות."),
        ("1", "הניסוח או המבנה מרמזים מעט מעבר למה שהראיות תומכות בו."),
        ("2", "נוצרת מסקנה סיבתית ניכרת בעוד הבסיס הניתן לבחינה חלקי."),
        ("3", "הכתבה קובעת סיבה או אחריות בביטחון למרות מחלוקת מהותית וראיות מתחרות."),
        ("4", "האשמה קטגורית סותרת ראיות חזקות זמינות או נמשכת אחרי הפרכה מהותית."),
        ("N/A", "הכתבה אינה עוסקת בסיבה או באחריות."),
    ],
    "N3": [
        ("0", "לא הושמטה או עוותה ראיית־נגד עובדתית מהותית."),
        ("1", "ראיית־נגד משנית אינה מפותחת, אך עדיין מובנת."),
        ("2", "ראיית־נגד מהותית מוזכרת אך נקברת, נדחסת או נשפטת ברף מחמיר משמעותית."),
        ("3", "ראיית־נגד מרכזית וזמינה מושמטת מכתבה שמקדמת מסקנה מרכזית."),
        ("4", "ראיית־נגד מרכזית מעוותת, מדוכאת או מוצגת באופן מטעה כדי ליצור מראית עין של ודאות."),
        ("N/A", "אין מסקנה שיש לה ראיית־נגד רלוונטית ניתנת לזיהוי."),
    ],
    "N4": [
        ("0", "הכותרת והליד משמרים את הראיות, הייחוס והסייגים שבגוף."),
        ("1", "פישוט שאינו מזיק."),
        ("2", "הכותרת או הליד חזקים במידה ניכרת מן הגוף בוודאות, באשמה או ברגש."),
        ("3", "סייגי הגוף מערערים מהותית את הרושם המרכזי של הכותרת."),
        ("4", "הכותרת או הליד קובעים כעובדה טענה מרכזית שהגוף אינו תומך בה או סותר."),
        ("N/A", "הכותרת או הליד המקוריים אינם זמינים."),
    ],
    "N5": [
        ("0", "שפת מערכת תיאורית ומידתית."),
        ("1", "שפה רגשית מוגבלת ומוצדקת שאינה מעוותת את התמונה."),
        ("2", "ניסוחים טעונים חוזרים בקול המערכת ומכוונים שיפוט, אך הראיות עדיין נגישות."),
        ("3", "תיוג מוסרי חריף או דמוניזציה מחליפים במידה רבה ראיות או אי־ודאות."),
        ("4", "סנסציוניות קיצונית, דה־הומניזציה, הסתה או אשמה קולקטיבית שולטים בטקסט."),
        ("N/A", "זמין רק קטע טכני או לא־מערכתי."),
    ],
}

VALID_OUTCOMES = {"ACCEPT_MODEL", "ACCEPT_CHALLENGE", "CUSTOM", "DEFER"}
VALID_PRECEDENT_MODES = {"CASE_ONLY", "PERSUASIVE_PRECEDENT", "BINDING_POLICY"}

ISSUE_TAGS_HE = {
    "PRIVILEGED_ACCESS": "גישה מיוחסת",
    "OFFICIAL_SOURCE": "מקור רשמי",
    "CLASSIFIED_EVIDENCE": "ראיה מסווגת",
    "RECONSTRUCTABILITY": "יכולת שחזור",
    "WARTIME_REPORTING": "דיווח בזמן מלחמה",
    "ANONYMOUS_SOURCE": "מקור אנונימי",
    "SOURCE_ROLE_AMBIGUITY": "עמימות בתפקיד המקור",
    "PRELIMINARY_INTELLIGENCE": "מודיעין ראשוני",
    "ADOPTED_CAUSAL_CLAIM": "אימוץ טענה סיבתית",
    "PUBLICATION_TIME_KNOWLEDGE": "הידע בזמן הפרסום",
    "LIVE_DISPUTE": "מחלוקת פעילה",
    "ANCHOR_CONFLICT": "סתירה לעוגן",
    "CATEGORICAL_CERTAINTY": "ודאות קטגורית",
    "PREMATURE_CLOSURE": "סגירה מוקדמת",
    "NO_MATCHED_ANCHOR": "אין עוגן תואם",
    "PRIMARY_VISUAL_INVESTIGATION": "חקירה חזותית ראשונית",
    "SELF_GENERATED_EVIDENCE": "ראיה שהופקה בידי המערכת",
    "SCOPE_DISCIPLINE": "משמעת היקף",
    "HEADLINE_ADOPTION": "אימוץ בכותרת",
    "ATTRIBUTED_BODY": "ייחוס בגוף הכתבה",
    "CAUSAL_CLAIM": "טענה סיבתית",
    "HEADLINE_BODY_MISMATCH": "פער כותרת–גוף",
    "VIEWPOINT_BALANCE": "איזון דוברים",
    "ONE_SIDED_REPORTING": "דיווח חד־צדדי",
    "MATERIAL_OMISSION": "השמטה מהותית",
    "INTERESTED_ACTOR_CLAIM": "טענת צד מעוניין",
    "FACTUAL_COUNTEREVIDENCE": "ראיית־נגד עובדתית",
    "VERSION_ALIGNMENT_DELTA": "פער בין גרסאות המדידה",
    "HARD_VERSION_ALIGNMENT_DELTA": "פער קשה בין גרסאות המדידה",
    "ATTRIBUTED_HEADLINE": "ייחוס בכותרת",
    "OUTLET_ADOPTION": "אימוץ בקול המערכת",
    "UNCERTAINTY_CALIBRATION": "כיול אי־ודאות",
    "SOURCE_VOICE_AMBIGUITY": "עמימות בין קול המקור לקול המערכת",
    "HUMAN_REFERENCE_COMPARISON": "השוואה לאמת־מידה אנושית",
    "MIDPOINT_REVIEW": "ביקורת ציון אמצע",
    "QUALITY_AUDIT_SAMPLE": "מדגם ביקורת איכות",
    "EDITORIAL_LANGUAGE": "לשון מערכתית",
    "HARD_HUMAN_REFERENCE_CONFLICT": "סתירה קשה לאמת־מידה אנושית",
    "APPROVED_HUMAN_REFERENCE_APPLIED": "אמת־מידה אנושית מאושרת",
}


@dataclass(frozen=True)
class CaseMaterial:
    case_id: str
    documents: list[dict[str, Any]]
    dilemmas: list[dict[str, Any]]
    anchors: dict[str, dict[str, Any]]
    reference_scores: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_policy_registry(base_dir: Path) -> dict[str, Any]:
    """Load and validate the human-approved policy layer separately from verdicts."""
    registry = _read_json(
        base_dir / "calibration/case001_policy_decisions_v1.json"
    )
    decisions = registry.get("decisions", [])
    if registry.get("case_id") != "CASE-001":
        raise ValueError("Policy registry is not for CASE-001")
    if len(decisions) != 5:
        raise ValueError("CASE-001 policy registry must contain D1 through D5")
    packet_items = {item.get("packet_item") for item in decisions}
    if packet_items != {"D1", "D2", "D3", "D4", "D5"}:
        raise ValueError("Policy registry is missing a reconciliation decision")
    if any(item.get("choice") != "A" for item in decisions):
        raise ValueError("Policy registry does not match the approved recommendations")
    if any(not str(item.get("rule_he") or "").strip() for item in decisions):
        raise ValueError("Every policy decision requires a written rule")
    applications = [
        _read_json(base_dir / relative_path)
        for relative_path in registry.get("application_files", [])
    ]
    for application in applications:
        if application.get("case_id") != registry["case_id"]:
            raise ValueError("Policy application case does not match its registry")
        if application.get("status") not in {
            "PENDING_PRINCIPAL_RESEARCHER_DECISION",
            "RESOLVED",
        }:
            raise ValueError("Unknown policy application status")
        if sum(
            bool(option.get("recommended"))
            for option in application.get("options", [])
        ) != 1:
            raise ValueError("Policy application requires exactly one recommendation")
        if application["status"] == "RESOLVED":
            option_ids = {
                option["option_id"] for option in application.get("options", [])
            }
            if application.get("selected_option") not in option_ids:
                raise ValueError("Resolved policy application needs a valid selection")
            if not str(application.get("resolution_rationale_he") or "").strip():
                raise ValueError("Resolved policy application needs a rationale")
    registry["open_applications"] = [
        item for item in applications
        if item["status"] == "PENDING_PRINCIPAL_RESEARCHER_DECISION"
    ]
    registry["resolved_applications"] = [
        item for item in applications if item["status"] == "RESOLVED"
    ]
    return registry


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _read_corpus(path: Path, document_ids: set[str]) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["document_id"]: row
            for row in csv.DictReader(handle)
            if row["document_id"] in document_ids
        }


def _read_evidence_translations(
    paths: list[Path],
) -> dict[tuple[str, str], dict[str, Any]]:
    translations: dict[tuple[str, str], dict[str, Any]] = {}
    translation_ids: set[str] = set()
    for path in paths:
        payload = _read_json(path)
        for record in payload["records"]:
            key = (record["document_id"], record["source_text"])
            if key in translations or record["translation_id"] in translation_ids:
                raise ValueError(
                    f"Duplicate evidence translation for {record['translation_id']}"
                )
            if record["target_language"] != "he":
                raise ValueError(
                    "Judgment-room translation must target Hebrew: "
                    f"{record['translation_id']}"
                )
            translations[key] = record
            translation_ids.add(record["translation_id"])
    return translations


def _read_visual_exhibits(
    paths: list[Path],
    *,
    case_id: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    exhibits: dict[tuple[str, str], list[dict[str, Any]]] = {}
    exhibit_ids: set[str] = set()
    required_text = (
        "title_he", "caption_he", "context_he", "proves_he", "does_not_prove_he"
    )
    for path in paths:
        payload = _read_json(path)
        if payload["case_id"] != case_id:
            raise ValueError(
                f"Visual-exhibit case mismatch: {payload['case_id']} != {case_id}"
            )
        for record in payload["records"]:
            exhibit_id = record["exhibit_id"]
            if exhibit_id in exhibit_ids:
                raise ValueError(f"Duplicate visual exhibit: {exhibit_id}")
            exhibit_ids.add(exhibit_id)
            if record["media_type"] not in {"video", "image"}:
                raise ValueError(f"Unsupported visual media type: {exhibit_id}")
            if not record["poster_url"].startswith("https://"):
                raise ValueError(f"Visual exhibit needs an HTTPS poster: {exhibit_id}")
            if record["media_type"] == "video" and not record["media_url"].startswith(
                "https://"
            ):
                raise ValueError(f"Visual exhibit needs an HTTPS video: {exhibit_id}")
            if any(not record.get(field, "").strip() for field in required_text):
                raise ValueError(
                    f"Visual exhibit is missing Hebrew scope text: {exhibit_id}"
                )
            for component in record["components"]:
                exhibits.setdefault(
                    (record["document_id"], component), []
                ).append(record)
    for records in exhibits.values():
        records.sort(key=lambda item: (item.get("order", 0), item["exhibit_id"]))
    return exhibits


def load_case_material(base_dir: Path) -> CaseMaterial:
    """Join frozen model queues, article text, anchors, and counterarguments."""
    queue_paths = sorted(
        (base_dir / "analysis/runs").glob("case001_article_review_queue_v1*.json")
    )
    if not queue_paths:
        raise ValueError("No CASE-001 review queue files found")
    document_index: dict[str, dict[str, Any]] = {}
    for path in queue_paths:
        for document in _read_json(path)["documents"]:
            document_index[document["document_id"]] = document
    documents = list(document_index.values())

    reference_payload = _read_json(
        base_dir / "calibration/case001_smoke_human_reference_v1.json"
    )
    reference_document_ids = {
        article["document_id"] for article in reference_payload["articles"]
    }
    document_ids = (
        {document["document_id"] for document in documents}
        | reference_document_ids
    )
    corpus = _read_corpus(
        base_dir / "exports/case001_research_pilot_27.csv", document_ids
    )
    for document in documents:
        document["article"] = corpus.get(document["document_id"], {})

    anchors = {
        anchor["anchor_id"]: anchor
        for anchor in _read_jsonl(base_dir / "anchors/case001_factual_anchors_v1.jsonl")
    }
    dilemma_paths = sorted(
        (base_dir / "calibration").glob("case001_judgment_dilemmas_v1*.json")
    )
    if not dilemma_paths:
        raise ValueError("No CASE-001 judgment dilemma files found")
    dilemma_payloads = [_read_json(path) for path in dilemma_paths]
    case_ids = {payload["case_id"] for payload in dilemma_payloads}
    if len(case_ids) != 1:
        raise ValueError(f"Judgment dilemma case mismatch: {sorted(case_ids)}")
    case_id = next(iter(case_ids))
    visual_exhibits = _read_visual_exhibits(
        sorted(
            (base_dir / "calibration").glob("case001_visual_exhibits_v1*.json")
        ),
        case_id=case_id,
    )
    evidence_translations = _read_evidence_translations(
        sorted(
            (base_dir / "calibration").glob(
                "case001_evidence_translations_he_v1*.json"
            )
        )
    )
    challenge_index: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in dilemma_payloads:
        for item in payload["dilemmas"]:
            key = (item["document_id"], item["component"])
            if key in challenge_index:
                raise ValueError(f"Duplicate courtroom dilemma: {key}")
            challenge_index[key] = item

    dilemmas: list[dict[str, Any]] = []
    routed_keys: set[tuple[str, str]] = set()
    for document in documents:
        for component in document["components"]:
            if component["decision"] != "HUMAN_REVIEW":
                continue
            key = (document["document_id"], component["component"])
            routed_keys.add(key)
            if key not in challenge_index:
                raise ValueError(f"Missing counterargument for routed component {key}")
            challenge = challenge_index[key]
            decisive_exhibit = component.get("evidence_span") or challenge.get(
                "curated_exhibit"
            )
            translation = None
            if decisive_exhibit:
                translation = evidence_translations.get(
                    (document["document_id"], decisive_exhibit["quote_original"])
                )
            anchor_review = challenge.get("anchor_review", [])
            anchor_refs = list(dict.fromkeys(
                component.get("anchor_refs", [])
                + challenge["challenge"].get("anchor_refs", [])
                + [item["anchor_id"] for item in anchor_review]
            ))
            missing_anchors = [ref for ref in anchor_refs if ref not in anchors]
            if missing_anchors:
                raise ValueError(
                    f"Unknown anchors for routed component {key}: {missing_anchors}"
                )
            principal_review = principal_review_assessment(
                component.get("score"), challenge["challenge"].get("score")
            )
            dilemmas.append({
                **challenge,
                "case_id": case_id,
                "source_id": document["source_id"],
                "language": document["language"],
                "sample_phase": document["sample_phase"],
                "prompt_version": document["prompt_version"],
                "article": document["article"],
                "model": component,
                "decisive_exhibit": decisive_exhibit,
                "evidence_translation": translation,
                "visual_exhibits": visual_exhibits.get(key, []),
                "anchor_review": anchor_review,
                "anchors": [anchors[ref] for ref in anchor_refs],
                "principal_review": principal_review,
                "principal_review_required": principal_review["required"],
            })

    # Dilemma packets are append-only research history. A later prompt or
    # routing-policy version may stop routing an older dilemma, while the
    # original packet and its verdict must remain auditable. Missing packets
    # for currently routed components are rejected above; historical extras
    # are intentionally ignored in the live courtroom view.
    dilemmas.sort(key=lambda item: (
        item["article"].get("published_at", ""), item["source_id"], item["component"]
    ))
    reference_scores: list[dict[str, Any]] = []
    for article in reference_payload["articles"]:
        corpus_article = corpus.get(article["document_id"], {})
        for component_id, component in article["components"].items():
            reference_scores.append({
                "record_id": (
                    f"REFERENCE:{reference_payload['reference_id']}:"
                    f"{article['document_id']}:{component_id}"
                ),
                "reference_id": reference_payload["reference_id"],
                "reference_status": reference_payload["status"],
                "case_id": reference_payload["case_id"],
                "document_id": article["document_id"],
                "source_id": corpus_article.get("source_id", ""),
                "source_name": article["source_name"],
                "published_at": article["publication_date"],
                "sample_phase": corpus_article.get("sample_phase", ""),
                "component": component_id,
                "score": component.get("recommended_score"),
                "na_reason": component.get("na_reason"),
                "confidence": component.get("confidence"),
                "rationale": component["rationale"],
                "evidence_span": {
                    "quote_original": component.get("evidence_quote", ""),
                    "location": "HUMAN_REFERENCE",
                },
                "anchor_refs": component.get("anchor_refs", []),
                "codebook_version": reference_payload["codebook_version"],
                "created_at": reference_payload["created_at"],
                "created_by": reference_payload["created_by"],
            })
    reference_scores.sort(key=lambda item: (
        item["published_at"], item["source_name"], item["component"]
    ))
    return CaseMaterial(
        case_id, documents, dilemmas, anchors,
        reference_scores,
    )


def derive_policy_rule(
    dilemma: dict[str, Any],
    outcome: str,
    final_score: int | None,
    final_na_reason: str | None,
    rationale: str,
) -> str:
    """Build an outcome-consistent fallback when the judge writes no general rule."""
    result = str(final_score) if final_score is not None else f"N/A ({final_na_reason})"
    if outcome == "ACCEPT_MODEL":
        return (
            f"בסוגיות שבתחום התחולה התקבלה עמדת Vertex ונקבע "
            f"{dilemma['component']}={result}. הנימוק המנחה: {rationale.strip()}"
        )
    if outcome == "ACCEPT_CHALLENGE":
        return (
            f"{dilemma['challenge']['label_he']}: "
            f"{dilemma['challenge']['argument_he']}"
        )
    if outcome == "CUSTOM":
        return f"כלל שנקבע בפסק דין עצמאי, {dilemma['component']}={result}: {rationale.strip()}"
    return ""


class JudgmentStore:
    """Append-only human decisions with one current version per dilemma."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS adjudication_decisions (
                    decision_id TEXT PRIMARY KEY,
                    dilemma_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    component TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('ACCEPT_MODEL', 'ACCEPT_CHALLENGE', 'CUSTOM', 'DEFER')
                    ),
                    final_score INTEGER CHECK (final_score BETWEEN 0 AND 4),
                    final_na_reason TEXT,
                    rationale TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    decision_version INTEGER NOT NULL,
                    supersedes_decision_id TEXT,
                    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                    dilemma_snapshot_json TEXT NOT NULL,
                    FOREIGN KEY (supersedes_decision_id) REFERENCES adjudication_decisions(decision_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_current_decision_per_dilemma
                    ON adjudication_decisions(dilemma_id) WHERE is_current = 1;
                CREATE INDEX IF NOT EXISTS decision_history_by_dilemma
                    ON adjudication_decisions(dilemma_id, decision_version DESC);

                CREATE TABLE IF NOT EXISTS component_score_ledger (
                    record_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    sample_phase TEXT NOT NULL,
                    component TEXT NOT NULL,
                    component_label TEXT NOT NULL,
                    authority TEXT NOT NULL CHECK (
                        authority IN (
                            'MODEL_PROPOSAL', 'HUMAN_REFERENCE', 'HUMAN_DECISION'
                        )
                    ),
                    score INTEGER CHECK (score BETWEEN 0 AND 4),
                    na_reason TEXT,
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT,
                    prompt_version TEXT NOT NULL,
                    model_confidence TEXT,
                    evidence_span_json TEXT,
                    anchor_refs_json TEXT NOT NULL DEFAULT '[]',
                    decision_id TEXT,
                    decision_version INTEGER,
                    decided_by TEXT,
                    recorded_at TEXT,
                    supersedes_record_id TEXT,
                    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                    is_effective INTEGER NOT NULL CHECK (is_effective IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS score_ledger_by_component
                    ON component_score_ledger(case_id, document_id, component, authority);
                CREATE INDEX IF NOT EXISTS score_ledger_current_effective
                    ON component_score_ledger(is_current, is_effective, component);

                CREATE TABLE IF NOT EXISTS trajectory_decisions (
                    decision_id TEXT PRIMARY KEY,
                    trajectory_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    publisher_slot TEXT NOT NULL,
                    language TEXT NOT NULL,
                    score INTEGER CHECK (score BETWEEN 0 AND 4),
                    na_reason TEXT,
                    rationale TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    decision_version INTEGER NOT NULL,
                    supersedes_decision_id TEXT,
                    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                    trajectory_snapshot_json TEXT NOT NULL,
                    FOREIGN KEY (supersedes_decision_id)
                        REFERENCES trajectory_decisions(decision_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_current_trajectory_decision
                    ON trajectory_decisions(trajectory_id) WHERE is_current = 1;
                CREATE INDEX IF NOT EXISTS trajectory_history
                    ON trajectory_decisions(trajectory_id, decision_version DESC);
            """)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(adjudication_decisions)"
                ).fetchall()
            }
            migrations = {
                "precedent_mode": (
                    "ALTER TABLE adjudication_decisions ADD COLUMN precedent_mode "
                    "TEXT NOT NULL DEFAULT 'PERSUASIVE_PRECEDENT'"
                ),
                "policy_rule": (
                    "ALTER TABLE adjudication_decisions ADD COLUMN policy_rule "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "policy_scope_json": (
                    "ALTER TABLE adjudication_decisions ADD COLUMN policy_scope_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                ),
                "policy_metadata_version": (
                    "ALTER TABLE adjudication_decisions ADD COLUMN policy_metadata_version "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)

    def backfill_policy_metadata(self, dilemmas: list[dict[str, Any]]) -> int:
        """Attach the current taxonomy to decisions made before precedents existed."""
        dilemma_index = {item["dilemma_id"]: item for item in dilemmas}
        updated = 0
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT decision_id, dilemma_id, outcome, final_score,
                          final_na_reason, rationale, policy_rule,
                          policy_scope_json, policy_metadata_version
                   FROM adjudication_decisions
                   WHERE policy_metadata_version < 1
                      OR policy_rule = '' OR policy_scope_json = '[]'"""
            ).fetchall()
            for row in rows:
                dilemma = dilemma_index.get(row["dilemma_id"])
                if dilemma is None:
                    continue
                policy_rule = derive_policy_rule(
                    dilemma,
                    row["outcome"],
                    row["final_score"],
                    row["final_na_reason"],
                    row["rationale"],
                )
                policy_scope = row["policy_scope_json"]
                if not json.loads(policy_scope):
                    policy_scope = json.dumps(
                        dilemma.get("issue_tags", []), ensure_ascii=False
                    )
                connection.execute(
                    """UPDATE adjudication_decisions
                       SET policy_rule = ?, policy_scope_json = ?,
                           policy_metadata_version = 1
                       WHERE decision_id = ?""",
                    (policy_rule, policy_scope, row["decision_id"]),
                )
                updated += 1
        return updated

    def current_decisions(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM adjudication_decisions WHERE is_current = 1 ORDER BY decided_at DESC"
            ).fetchall()
        return {row["dilemma_id"]: dict(row) for row in rows}

    def history(self, dilemma_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM adjudication_decisions WHERE dilemma_id = ? ORDER BY decision_version DESC",
                (dilemma_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_history(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM adjudication_decisions ORDER BY decided_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_score_ledger(self, material: CaseMaterial) -> int:
        """Materialize every model score and human verdict without deleting history."""
        dilemma_index = {item["dilemma_id"]: item for item in material.dilemmas}
        document_index = {
            document["document_id"]: document for document in material.documents
        }
        reference_confirmed_keys = {
            (document["document_id"], component["component"])
            for document in material.documents
            for component in document["components"]
            if component["decision"] == "HUMAN_REFERENCE_CONFIRMED"
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_human_keys = {
                row["dilemma_id"]
                for row in connection.execute(
                    """SELECT dilemma_id FROM adjudication_decisions
                       WHERE is_current = 1 AND outcome != 'DEFER'"""
                ).fetchall()
            }
            for reference in material.reference_scores:
                reference_is_effective = int(
                    reference["document_id"] not in document_index
                    or (
                        reference["document_id"], reference["component"]
                    ) in reference_confirmed_keys
                )
                connection.execute(
                    """INSERT INTO component_score_ledger (
                           record_id, case_id, document_id, source_id,
                           source_name, published_at, sample_phase,
                           component, component_label, authority, score,
                           na_reason, rationale, status, outcome,
                           prompt_version, model_confidence,
                           evidence_span_json, anchor_refs_json,
                           decision_id, decision_version, decided_by,
                           recorded_at, supersedes_record_id,
                           is_current, is_effective
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'HUMAN_REFERENCE', ?, ?, ?, ?,
                                 'APPROVED_HUMAN_REFERENCE', ?, ?, ?, ?, ?,
                                 NULL, ?, ?, NULL, 1, ?)
                       ON CONFLICT(record_id) DO UPDATE SET
                           is_current = excluded.is_current,
                           is_effective = excluded.is_effective""",
                    (
                        reference["record_id"],
                        reference["case_id"],
                        reference["document_id"],
                        reference["source_id"],
                        reference["source_name"],
                        reference["published_at"],
                        reference["sample_phase"],
                        reference["component"],
                        COMPONENT_LABELS[reference["component"]],
                        reference["score"],
                        reference["na_reason"],
                        reference["rationale"],
                        reference["reference_status"],
                        f"codebook-{reference['codebook_version']}",
                        reference["confidence"],
                        json.dumps(
                            reference["evidence_span"], ensure_ascii=False
                        ),
                        json.dumps(
                            reference["anchor_refs"], ensure_ascii=False
                        ),
                        reference["reference_id"],
                        reference["created_by"],
                        reference["created_at"],
                        reference_is_effective,
                    ),
                )
            for document in material.documents:
                article = document.get("article", {})
                source_id = document["source_id"]
                source_name = article.get("source_name") or source_id
                for component in document["components"]:
                    component_id = component["component"]
                    dilemma_id = (
                        f"{material.case_id}:{document['document_id']}:{component_id}"
                    )
                    legacy_dilemma_id = (
                        f"{material.case_id.replace('-', '')}:"
                        f"{document['document_id']}:{component_id}"
                    )
                    if (
                        dilemma_id not in dilemma_index
                        and dilemma_id not in current_human_keys
                        and (
                            legacy_dilemma_id in dilemma_index
                            or legacy_dilemma_id in current_human_keys
                        )
                    ):
                        dilemma_id = legacy_dilemma_id
                    dilemma = dilemma_index.get(dilemma_id)
                    below_human_threshold = (
                        component["decision"] == "HUMAN_REVIEW"
                        and dilemma is not None
                        and not dilemma.get("principal_review_required", True)
                    )
                    model_is_effective = int(
                        dilemma_id not in current_human_keys
                        and (
                            component["decision"] == "PROVISIONAL_ACCEPT"
                            or (
                            below_human_threshold
                            )
                        )
                    )
                    ledger_status = (
                        "PROVISIONAL_ACCEPT_V4"
                        if below_human_threshold
                        else component["decision"]
                    )
                    record_id = (
                        f"MODEL:{material.case_id}:{document['document_id']}:"
                        f"{component_id}:{document['prompt_version']}"
                    )
                    score = component.get("score")
                    connection.execute(
                        """UPDATE component_score_ledger
                           SET is_current = 0, is_effective = 0
                           WHERE authority = 'MODEL_PROPOSAL'
                             AND case_id = ? AND document_id = ?
                             AND component = ? AND record_id != ?""",
                        (
                            material.case_id,
                            document["document_id"],
                            component_id,
                            record_id,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO component_score_ledger (
                               record_id, case_id, document_id, source_id,
                               source_name, published_at, sample_phase,
                               component, component_label, authority, score,
                               na_reason, rationale, status, outcome,
                               prompt_version, model_confidence,
                               evidence_span_json, anchor_refs_json,
                               decision_id, decision_version, decided_by,
                               recorded_at, supersedes_record_id,
                               is_current, is_effective
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                     'MODEL_PROPOSAL', ?, ?, ?, ?, NULL,
                                     ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL,
                                     1, ?)
                           ON CONFLICT(record_id) DO UPDATE SET
                               score = excluded.score,
                               na_reason = excluded.na_reason,
                               rationale = excluded.rationale,
                               status = excluded.status,
                               prompt_version = excluded.prompt_version,
                               model_confidence = excluded.model_confidence,
                               evidence_span_json = excluded.evidence_span_json,
                               anchor_refs_json = excluded.anchor_refs_json,
                               is_current = excluded.is_current,
                               is_effective = excluded.is_effective""",
                        (
                            record_id,
                            material.case_id,
                            document["document_id"],
                            source_id,
                            source_name,
                            article.get("published_at", ""),
                            document["sample_phase"],
                            component_id,
                            COMPONENT_LABELS[component_id],
                            score,
                            None if score is not None else "MODEL_NOT_APPLICABLE",
                            component.get("rationale", ""),
                            ledger_status,
                            document["prompt_version"],
                            component.get("confidence"),
                            json.dumps(
                                component.get("evidence_span"), ensure_ascii=False
                            ),
                            json.dumps(
                                component.get("anchor_refs", []), ensure_ascii=False
                            ),
                            model_is_effective,
                        ),
                    )

            connection.execute(
                """UPDATE component_score_ledger
                   SET is_current = 0, is_effective = 0
                   WHERE authority = 'HUMAN_DECISION'"""
            )
            decisions = connection.execute(
                "SELECT * FROM adjudication_decisions ORDER BY decided_at"
            ).fetchall()
            for row in decisions:
                decision = dict(row)
                dilemma = dilemma_index.get(decision["dilemma_id"])
                document = document_index.get(decision["document_id"], {})
                snapshot = json.loads(decision["dilemma_snapshot_json"])
                article = (
                    dilemma["article"] if dilemma is not None
                    else snapshot.get("article", {})
                )
                source_id = (
                    dilemma["source_id"] if dilemma is not None
                    else article.get("source_id", document.get("source_id", ""))
                )
                evidence = snapshot.get("decisive_exhibit")
                if evidence is None:
                    evidence = snapshot.get("model", {}).get("evidence_span")
                record_id = f"HUMAN:{decision['decision_id']}"
                is_current = int(bool(decision["is_current"]))
                is_effective = int(
                    bool(decision["is_current"])
                    and decision["outcome"] != "DEFER"
                )
                connection.execute(
                    """INSERT INTO component_score_ledger (
                           record_id, case_id, document_id, source_id,
                           source_name, published_at, sample_phase,
                           component, component_label, authority, score,
                           na_reason, rationale, status, outcome,
                           prompt_version, model_confidence,
                           evidence_span_json, anchor_refs_json,
                           decision_id, decision_version, decided_by,
                           recorded_at, supersedes_record_id,
                           is_current, is_effective
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'HUMAN_DECISION', ?, ?, ?, ?, ?, ?, NULL,
                                 ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(record_id) DO UPDATE SET
                           is_current = excluded.is_current,
                           is_effective = excluded.is_effective""",
                    (
                        record_id,
                        decision["case_id"],
                        decision["document_id"],
                        source_id,
                        article.get("source_name") or source_id,
                        article.get("published_at", ""),
                        (
                            dilemma["sample_phase"] if dilemma is not None
                            else document.get("sample_phase", "")
                        ),
                        decision["component"],
                        COMPONENT_LABELS[decision["component"]],
                        decision["final_score"],
                        decision["final_na_reason"],
                        decision["rationale"],
                        "CURRENT" if is_current else "SUPERSEDED",
                        decision["outcome"],
                        snapshot.get("prompt_version", document.get("prompt_version", "")),
                        json.dumps(evidence, ensure_ascii=False),
                        json.dumps(snapshot.get("anchor_ids", []), ensure_ascii=False),
                        decision["decision_id"],
                        decision["decision_version"],
                        decision["decided_by"],
                        decision["decided_at"],
                        (
                            f"HUMAN:{decision['supersedes_decision_id']}"
                            if decision["supersedes_decision_id"] else None
                        ),
                        is_current,
                        is_effective,
                    ),
                )
            return connection.execute(
                "SELECT COUNT(*) FROM component_score_ledger"
            ).fetchone()[0]

    def score_ledger_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM component_score_ledger
                   ORDER BY published_at, source_name, component,
                            authority, decision_version"""
            ).fetchall()
        return [dict(row) for row in rows]

    def current_trajectory_decisions(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM trajectory_decisions
                   WHERE is_current = 1 ORDER BY decided_at DESC"""
            ).fetchall()
        return {row["trajectory_id"]: dict(row) for row in rows}

    def trajectory_history(self, trajectory_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM trajectory_decisions
                   WHERE trajectory_id = ? ORDER BY decision_version DESC""",
                (trajectory_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_trajectory_history(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trajectory_decisions ORDER BY decided_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_trajectory_decision(
        self,
        trajectory: dict[str, Any],
        *,
        score: int | None,
        na_reason: str | None,
        rationale: str,
        decided_by: str,
    ) -> dict[str, Any]:
        """Append one T4 decision and retain all prior versions."""
        if score is not None and score not in {0, 1, 2, 3, 4}:
            raise ValueError("T4 score must be 0-4 or N/A")
        if score is None and not (na_reason or "").strip():
            raise ValueError("An N/A T4 decision requires a reason")
        if not rationale.strip():
            raise ValueError("A T4 decision requires a rationale")
        if not decided_by.strip():
            raise ValueError("A T4 decision requires a judge")

        decision_id = str(uuid.uuid4())
        decided_at = datetime.now(timezone.utc).isoformat()
        snapshot = json.dumps(
            trajectory, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """SELECT decision_id, decision_version
                   FROM trajectory_decisions
                   WHERE trajectory_id = ? AND is_current = 1""",
                (trajectory["trajectory_id"],),
            ).fetchone()
            version = previous["decision_version"] + 1 if previous else 1
            previous_id = previous["decision_id"] if previous else None
            if previous:
                connection.execute(
                    """UPDATE trajectory_decisions SET is_current = 0
                       WHERE decision_id = ?""",
                    (previous_id,),
                )
            connection.execute(
                """INSERT INTO trajectory_decisions (
                       decision_id, trajectory_id, case_id, publisher_slot,
                       language, score, na_reason, rationale, decided_by,
                       decided_at, decision_version, supersedes_decision_id,
                       is_current, trajectory_snapshot_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    decision_id,
                    trajectory["trajectory_id"],
                    trajectory["case_id"],
                    trajectory["publisher_slot"],
                    trajectory["language"],
                    score,
                    (na_reason or "").strip() or None,
                    rationale.strip(),
                    decided_by.strip(),
                    decided_at,
                    version,
                    previous_id,
                    snapshot,
                ),
            )
        return self.current_trajectory_decisions()[trajectory["trajectory_id"]]

    def save_decision(
        self,
        dilemma: dict[str, Any],
        outcome: str,
        rationale: str,
        decided_by: str,
        custom_score: int | None = None,
        custom_is_na: bool = False,
        custom_na_reason: str | None = None,
        precedent_mode: str = "PERSUASIVE_PRECEDENT",
        policy_rule: str | None = None,
        policy_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        if outcome not in VALID_OUTCOMES:
            raise ValueError("Unknown adjudication outcome")
        if not rationale.strip():
            raise ValueError("Decision rationale is required")
        if not decided_by.strip():
            raise ValueError("Judge name or role is required")
        if precedent_mode not in VALID_PRECEDENT_MODES:
            raise ValueError("Unknown precedent mode")
        explicit_rule = (policy_rule or "").strip()
        if precedent_mode == "BINDING_POLICY" and not explicit_rule:
            raise ValueError("A binding policy requires an explicit rule")
        normalized_scope = policy_scope or dilemma.get("issue_tags", [])

        if outcome == "ACCEPT_MODEL":
            final_score = dilemma["model"].get("score")
            final_na_reason = None if final_score is not None else "MODEL_NOT_APPLICABLE"
        elif outcome == "ACCEPT_CHALLENGE":
            final_score = dilemma["challenge"].get("score")
            final_na_reason = dilemma["challenge"].get("na_reason")
            if final_score is None and not final_na_reason:
                final_na_reason = "CHALLENGE_NOT_APPLICABLE"
        elif outcome == "CUSTOM":
            if custom_is_na:
                final_score = None
                final_na_reason = (custom_na_reason or "HUMAN_NOT_APPLICABLE").strip()
            else:
                if custom_score not in {0, 1, 2, 3, 4}:
                    raise ValueError("A custom decision requires a score from 0 to 4 or N/A")
                final_score = custom_score
                final_na_reason = None
        else:
            final_score = None
            final_na_reason = "DEFERRED"

        normalized_rule = explicit_rule or derive_policy_rule(
            dilemma, outcome, final_score, final_na_reason, rationale
        )

        decision_id = str(uuid.uuid4())
        decided_at = datetime.now(timezone.utc).isoformat()
        snapshot_payload = {
            "dilemma_id": dilemma["dilemma_id"],
            "case_id": dilemma["case_id"],
            "document_id": dilemma["document_id"],
            "component": dilemma["component"],
            "issue_tags": dilemma.get("issue_tags", []),
            "question_he": dilemma["question_he"],
            "bench_note_he": dilemma["bench_note_he"],
            "prompt_version": dilemma["prompt_version"],
            "model": dilemma["model"],
            "challenge": dilemma["challenge"],
            "decisive_exhibit": dilemma.get("decisive_exhibit"),
            "evidence_translation": dilemma.get("evidence_translation"),
            "visual_exhibits": dilemma.get("visual_exhibits", []),
            "anchor_review": dilemma.get("anchor_review", []),
            "article": {
                key: dilemma["article"].get(key)
                for key in ("source_id", "source_name", "title", "published_at", "content_hash", "snapshot_hash")
            },
            "anchor_ids": [anchor["anchor_id"] for anchor in dilemma["anchors"]],
        }
        snapshot = json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT decision_id, decision_version FROM adjudication_decisions WHERE dilemma_id = ? AND is_current = 1",
                (dilemma["dilemma_id"],),
            ).fetchone()
            version = previous["decision_version"] + 1 if previous else 1
            previous_id = previous["decision_id"] if previous else None
            if previous:
                connection.execute(
                    "UPDATE adjudication_decisions SET is_current = 0 WHERE decision_id = ?", (previous_id,)
                )
            connection.execute("""
                INSERT INTO adjudication_decisions (
                    decision_id, dilemma_id, case_id, document_id, component,
                    outcome, final_score, final_na_reason, rationale, decided_by,
                    decided_at, decision_version, supersedes_decision_id,
                    is_current, dilemma_snapshot_json, precedent_mode,
                    policy_rule, policy_scope_json, policy_metadata_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1)
            """, (
                decision_id, dilemma["dilemma_id"], dilemma["case_id"],
                dilemma["document_id"], dilemma["component"], outcome,
                final_score, final_na_reason, rationale.strip(), decided_by.strip(),
                decided_at, version, previous_id, snapshot, precedent_mode,
                normalized_rule,
                json.dumps(normalized_scope, ensure_ascii=False),
            ))
        return self.current_decisions()[dilemma["dilemma_id"]]


def decision_status(decision: dict[str, Any] | None) -> str:
    if decision is None:
        return "PENDING"
    if decision["outcome"] == "DEFER":
        return "DEFERRED"
    return "DECIDED"


def split_dilemmas_by_status(
    dilemmas: Iterable[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep actionable dilemmas and decided history in separate collections."""
    actionable: list[dict[str, Any]] = []
    decided: list[dict[str, Any]] = []
    for dilemma in dilemmas:
        status = decision_status(decisions.get(dilemma["dilemma_id"]))
        if status == "DECIDED":
            decided.append(dilemma)
        elif dilemma.get("principal_review_required", True):
            actionable.append(dilemma)
    return actionable, decided


def principal_review_assessment(
    model_score: int | None, challenge_score: int | None
) -> dict[str, Any]:
    """Apply the approved two-point disagreement threshold for human review."""
    numeric_na_mismatch = (model_score is None) != (challenge_score is None)
    score_gap = (
        abs(model_score - challenge_score)
        if isinstance(model_score, int) and isinstance(challenge_score, int)
        else None
    )
    required = numeric_na_mismatch or (score_gap is not None and score_gap >= 2)
    return {
        "required": required,
        "policy_version": "case001-principal-escalation-v4",
        "score_gap": score_gap,
        "numeric_na_mismatch": numeric_na_mismatch,
        "reason": (
            "NUMERIC_NA_MISMATCH"
            if numeric_na_mismatch
            else (
                "SCORE_GAP_AT_LEAST_TWO"
                if required
                else "BELOW_TWO_POINT_HUMAN_THRESHOLD"
            )
        ),
    }


def next_actionable_dilemma_id(
    dilemmas: Iterable[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    *,
    current_dilemma_id: str | None = None,
) -> str | None:
    """Return the next open dilemma, finishing the current article first."""
    actionable, _ = split_dilemmas_by_status(dilemmas, decisions)
    current_document_id = next(
        (
            dilemma.get("document_id")
            for dilemma in dilemmas
            if dilemma["dilemma_id"] == current_dilemma_id
        ),
        None,
    )
    for dilemma in actionable:
        if (
            dilemma["dilemma_id"] != current_dilemma_id
            and dilemma.get("document_id") == current_document_id
        ):
            return dilemma["dilemma_id"]
    for dilemma in actionable:
        if dilemma["dilemma_id"] != current_dilemma_id:
            return dilemma["dilemma_id"]
    return actionable[0]["dilemma_id"] if actionable else None


def stage_court_navigation(
    state: MutableMapping[str, Any], next_dilemma_id: str | None
) -> None:
    """Queue widget-key changes for the beginning of the next Streamlit run."""
    state["_court_list_view_next"] = "ACTIONABLE"
    if next_dilemma_id is None:
        state.pop("_court_actionable_case_id_next", None)
        state["_court_clear_actionable_case_id"] = True
    else:
        state["_court_actionable_case_id_next"] = next_dilemma_id
        state.pop("_court_clear_actionable_case_id", None)


def apply_staged_court_navigation(state: MutableMapping[str, Any]) -> None:
    """Apply queued navigation before the relevant Streamlit widgets exist."""
    next_list_view = state.pop("_court_list_view_next", None)
    if next_list_view is not None:
        state["court_list_view"] = next_list_view
    if state.pop("_court_clear_actionable_case_id", False):
        state.pop("court_actionable_case_id", None)
    next_dilemma_id = state.pop("_court_actionable_case_id_next", None)
    if next_dilemma_id is not None:
        state["court_actionable_case_id"] = next_dilemma_id
        state["_court_actionable_document_sync_case_id"] = next_dilemma_id


def build_effective_rows(
    material: CaseMaterial, decisions: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Create a live view without modifying the frozen model measurements."""
    rows: list[dict[str, Any]] = []
    dilemma_index = {item["dilemma_id"]: item for item in material.dilemmas}
    for document in material.documents:
        article = document.get("article", {})
        for component in document["components"]:
            dilemma_id = (
                f"{material.case_id}:{document['document_id']}:{component['component']}"
            )
            legacy_dilemma_id = (
                f"{material.case_id.replace('-', '')}:"
                f"{document['document_id']}:{component['component']}"
            )
            if (
                dilemma_id not in dilemma_index
                and dilemma_id not in decisions
                and (
                    legacy_dilemma_id in dilemma_index
                    or legacy_dilemma_id in decisions
                )
            ):
                dilemma_id = legacy_dilemma_id
            decision = decisions.get(dilemma_id)
            if decision and decision["outcome"] != "DEFER":
                effective_score = decision["final_score"]
                effective_na_reason = decision["final_na_reason"]
                effective_rationale = decision["rationale"]
                authority, status = "HUMAN_DECISION", "DECIDED"
            elif component["decision"] == "HUMAN_REFERENCE_CONFIRMED":
                reference = component["human_reference"]
                effective_score = reference.get("score")
                effective_na_reason = reference.get("na_reason")
                effective_rationale = reference["rationale"]
                authority, status = "HUMAN_REFERENCE", "REFERENCE_CONFIRMED"
            elif component["decision"] == "HUMAN_REVIEW":
                if not dilemma_index[dilemma_id].get(
                    "principal_review_required", True
                ):
                    effective_score = component.get("score")
                    effective_na_reason = (
                        None if effective_score is not None else "MODEL_NOT_APPLICABLE"
                    )
                    effective_rationale = component.get("rationale", "")
                    authority, status = "MODEL_PROVISIONAL", "PROVISIONAL"
                else:
                    effective_score, effective_na_reason = None, None
                    effective_rationale = decision["rationale"] if decision else ""
                    authority, status = "NONE", "PENDING"
            else:
                effective_score = component.get("score")
                effective_na_reason = None if effective_score is not None else "MODEL_NOT_APPLICABLE"
                effective_rationale = component.get("rationale", "")
                authority, status = "MODEL_PROVISIONAL", "PROVISIONAL"
            rows.append({
                "document_id": document["document_id"],
                "source": article.get("source_name") or document["source_id"],
                "published_at": article.get("published_at", ""),
                "phase": document["sample_phase"],
                "component": component["component"],
                "effective_score": effective_score,
                "na_reason": effective_na_reason,
                "status": status,
                "authority": authority,
                "effective_rationale": effective_rationale,
                "model_score": component.get("score"),
                "model_na_reason": (
                    None if component.get("score") is not None
                    else "MODEL_NOT_APPLICABLE"
                ),
                "model_rationale": component.get("rationale", ""),
                "model_confidence": component.get("confidence"),
                "model_decision": component["decision"],
                "human_outcome": decision["outcome"] if decision else None,
                "decision_version": decision["decision_version"] if decision else None,
            })
    model_document_ids = {
        document["document_id"] for document in material.documents
    }
    for reference in material.reference_scores:
        if reference["document_id"] in model_document_ids:
            continue
        rows.append({
            "document_id": reference["document_id"],
            "source": reference["source_name"],
            "published_at": reference["published_at"],
            "phase": reference["sample_phase"],
            "component": reference["component"],
            "effective_score": reference["score"],
            "na_reason": reference["na_reason"],
            "status": "HUMAN_REFERENCE",
            "authority": "HUMAN_REFERENCE",
            "effective_rationale": reference["rationale"],
            "model_score": None,
            "model_na_reason": None,
            "model_rationale": "",
            "model_confidence": None,
            "model_decision": None,
            "human_outcome": "APPROVED_HUMAN_REFERENCE",
            "decision_version": reference["reference_id"],
        })
    rows.sort(key=lambda item: (
        item["published_at"], item["source"], item["component"]
    ))
    return rows


def build_precedent_records(
    material: CaseMaterial,
    decisions: dict[str, dict[str, Any]],
    policy_registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Turn current human verdicts into a compact, inspectable precedent library."""
    dilemma_index = {item["dilemma_id"]: item for item in material.dilemmas}
    precedent_overrides = {
        item["dilemma_id"]: item
        for item in (policy_registry or {}).get("precedent_overrides", [])
    }
    records: list[dict[str, Any]] = []
    for decision in decisions.values():
        mode = decision.get("precedent_mode") or "PERSUASIVE_PRECEDENT"
        override = precedent_overrides.get(decision["dilemma_id"])
        if override:
            mode = override["effective_mode"]
        if decision["outcome"] == "DEFER" or mode == "CASE_ONLY":
            continue
        dilemma = dilemma_index.get(decision["dilemma_id"])
        if dilemma is None:
            continue
        try:
            issue_tags = json.loads(decision.get("policy_scope_json") or "[]")
        except json.JSONDecodeError:
            issue_tags = []
        if not issue_tags:
            issue_tags = dilemma.get("issue_tags", [])
        warning_key = {
            "ACCEPT_MODEL": "model_policy_warning_he",
            "ACCEPT_CHALLENGE": "challenge_policy_warning_he",
            "CUSTOM": "custom_policy_warning_he",
        }.get(decision["outcome"])
        records.append({
            "precedent_id": decision["decision_id"],
            "dilemma_id": decision["dilemma_id"],
            "case_id": decision["case_id"],
            "document_id": decision["document_id"],
            "component": decision["component"],
            "final_score": decision["final_score"],
            "final_na_reason": decision["final_na_reason"],
            "outcome": decision["outcome"],
            "precedent_mode": mode,
            "policy_rule": decision.get("policy_rule") or dilemma["bench_note_he"],
            "issue_tags": issue_tags,
            "rationale": decision["rationale"],
            "decided_by": decision["decided_by"],
            "decided_at": decision["decided_at"],
            "source": dilemma["article"].get("source_name") or dilemma["source_id"],
            "title": dilemma["article"].get("title", ""),
            "question_he": dilemma["question_he"],
            "policy_warning_he": dilemma.get(warning_key) if warning_key else None,
            "policy_override_id": override.get("policy_id") if override else None,
        })
    records.sort(key=lambda item: item["decided_at"], reverse=True)
    return records


def find_relevant_precedents(
    dilemma: dict[str, Any], precedents: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rank same-component precedents by transparent shared issue tags."""
    target_tags = set(dilemma.get("issue_tags", []))
    matches: list[dict[str, Any]] = []
    for precedent in precedents:
        if precedent["dilemma_id"] == dilemma["dilemma_id"]:
            continue
        if precedent["component"] != dilemma["component"]:
            continue
        shared_tags = sorted(target_tags.intersection(precedent["issue_tags"]))
        if not shared_tags:
            continue
        mode_bonus = 100 if precedent["precedent_mode"] == "BINDING_POLICY" else 0
        matches.append({
            **precedent,
            "shared_tags": shared_tags,
            "relevance_score": mode_bonus + len(shared_tags),
        })
    matches.sort(
        key=lambda item: (item["relevance_score"], item["decided_at"]),
        reverse=True,
    )
    return matches


def find_precedent_conflicts(
    precedents: list[dict[str, Any]],
    policy_registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flag potentially inconsistent rules; never resolve them automatically."""
    conflict_resolutions = {
        (
            item["component"],
            frozenset(item["dilemma_ids"]),
        ): item
        for item in (policy_registry or {}).get("conflict_resolutions", [])
    }
    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(precedents):
        for right in precedents[index + 1:]:
            if left["component"] != right["component"]:
                continue
            shared_tags = sorted(set(left["issue_tags"]).intersection(right["issue_tags"]))
            if not shared_tags:
                continue
            left_result = (left["final_score"], left["final_na_reason"])
            right_result = (right["final_score"], right["final_na_reason"])
            if left_result == right_result:
                continue
            resolution = conflict_resolutions.get((
                left["component"],
                frozenset((left["dilemma_id"], right["dilemma_id"])),
            ))
            if resolution and resolution["status"] == "RESOLVED_BY_SCOPE":
                continue
            conflicts.append({
                "component": left["component"],
                "left": left,
                "right": right,
                "shared_tags": shared_tags,
                "policy_resolution": resolution,
            })
    return conflicts


def decisions_as_jsonl(decisions: Iterable[dict[str, Any]]) -> bytes:
    lines = []
    for decision in decisions:
        public_record = {key: value for key, value in decision.items() if key != "dilemma_snapshot_json"}
        lines.append(json.dumps(public_record, ensure_ascii=False, sort_keys=True))
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
