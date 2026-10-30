from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .case_generator import (
    FAILABLE_STATES,
    PAUSABLE_STATES,
    CaseState,
    CaseTransitionError,
    require_transition,
)
from .case_generator_contracts import (
    CaseGeneratorContractError,
    basket_article_payload_changed,
    basket_content_hash,
    candidate_content_hash,
    validate_basket_contract,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CaseRegistryError(ValueError):
    """Raised when registry state or version binding is invalid."""


class CaseRegistry:
    """Small SQLite registry for the two human gates and resumable execution."""

    SCHEMA_VERSION = 2

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
                CREATE TABLE IF NOT EXISTS schema_versions (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_candidates (
                    candidate_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (candidate_id, version)
                );

                CREATE TABLE IF NOT EXISTS case_runs (
                    case_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    current_basket_hash TEXT,
                    manifest_json TEXT,
                    methodology_version TEXT NOT NULL,
                    resume_state TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (candidate_id, candidate_version)
                        REFERENCES case_candidates(candidate_id, version)
                );

                CREATE TABLE IF NOT EXISTS gate_decisions (
                    decision_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    candidate_id TEXT,
                    gate TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES case_runs(case_id)
                );

                CREATE TABLE IF NOT EXISTS article_baskets (
                    basket_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    basket_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_by_hash TEXT,
                    UNIQUE (case_id, version),
                    FOREIGN KEY (case_id) REFERENCES case_runs(case_id)
                );

                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES case_runs(case_id)
                );

                CREATE TABLE IF NOT EXISTS selection_audit (
                    record_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    balance_impact TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    decision_id TEXT,
                    FOREIGN KEY (case_id) REFERENCES case_runs(case_id)
                );

                CREATE INDEX IF NOT EXISTS idx_candidates_state
                    ON case_candidates(state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_case_runs_state
                    ON case_runs(current_state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_gate_case
                    ON gate_decisions(case_id, gate, recorded_at);
                CREATE INDEX IF NOT EXISTS idx_basket_case
                    ON article_baskets(case_id, version);
                CREATE INDEX IF NOT EXISTS idx_checkpoint_case
                    ON run_checkpoints(case_id, stage, recorded_at);
                """
            )
            self._migrate_gate_decisions(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_gate_case "
                "ON gate_decisions(case_id, gate, recorded_at)"
            )
            connection.execute(
                """
                INSERT INTO schema_versions (component, version, applied_at)
                VALUES ('case_registry', ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    version=excluded.version,
                    applied_at=excluded.applied_at
                """,
                (self.SCHEMA_VERSION, utc_now()),
            )
            connection.commit()

    @staticmethod
    def _migrate_gate_decisions(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(gate_decisions)")
        }
        if "candidate_id" in columns and columns.get("case_id") == 0:
            return
        connection.execute("ALTER TABLE gate_decisions RENAME TO gate_decisions_v1")
        connection.execute(
            """
            CREATE TABLE gate_decisions (
                decision_id TEXT PRIMARY KEY,
                case_id TEXT,
                candidate_id TEXT,
                gate TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                actor TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                rationale TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES case_runs(case_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO gate_decisions (
                decision_id, case_id, candidate_id, gate, target_hash,
                decision, actor, recorded_at, rationale, idempotency_key,
                payload_hash, payload_json
            )
            SELECT decision_id, case_id, NULL, gate, target_hash,
                decision, actor, recorded_at, rationale, idempotency_key,
                payload_hash, payload_json
            FROM gate_decisions_v1
            """
        )
        connection.execute("DROP TABLE gate_decisions_v1")

    def register_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        expected = candidate_content_hash(candidate)
        if candidate.get("candidate_hash") != expected:
            raise CaseRegistryError("candidate_hash does not match candidate content")
        candidate_id = str(candidate["candidate_id"])
        version = int(candidate["version"])
        payload_json = canonical_json(candidate)
        now = utc_now()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                """
                SELECT * FROM case_candidates
                WHERE candidate_id = ? AND version = ?
                """,
                (candidate_id, version),
            ).fetchone()
            if existing:
                if existing["candidate_hash"] != expected:
                    raise CaseRegistryError("candidate version already has different content")
                return self._decode_candidate(existing)
            connection.execute(
                """
                INSERT INTO case_candidates (
                    candidate_id, version, candidate_hash, title, state,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    version,
                    expected,
                    candidate["title"],
                    candidate["state"],
                    payload_json,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_candidate(candidate_id, version)

    def record_topic_decision(
        self,
        candidate_id: str,
        candidate_version: int,
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        if decision["gate"] != "TOPIC":
            raise CaseRegistryError("record_topic_decision requires TOPIC gate")
        if decision.get("candidate_id") != candidate_id:
            raise CaseRegistryError("topic decision candidate_id does not match candidate")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT * FROM case_candidates
                WHERE candidate_id = ? AND version = ?
                """,
                (candidate_id, candidate_version),
            ).fetchone()
            if not candidate:
                raise CaseRegistryError("candidate does not exist")
            if decision["target_hash"] != candidate["candidate_hash"]:
                raise CaseRegistryError("topic decision targets a stale candidate hash")

            cached = self._idempotent_decision(connection, decision)
            if cached is not None:
                connection.commit()
                return cached

            outcome = str(decision["decision"])
            if outcome == "APPROVE":
                case_id = str(decision.get("case_id") or "")
                if not case_id:
                    raise CaseRegistryError("approved topic decision requires case_id")
                prior_approval = connection.execute(
                    """
                    SELECT case_id FROM gate_decisions
                    WHERE gate = 'TOPIC' AND decision = 'APPROVE'
                      AND target_hash = ?
                    LIMIT 1
                    """,
                    (candidate["candidate_hash"],),
                ).fetchone()
                if prior_approval:
                    raise CaseRegistryError(
                        f"candidate is already approved as {prior_approval['case_id']}"
                    )
                occupied_case = connection.execute(
                    "SELECT candidate_hash FROM case_runs WHERE case_id = ?",
                    (case_id,),
                ).fetchone()
                if occupied_case:
                    raise CaseRegistryError("case_id is already assigned")
                current_state = CaseState.BASKET_DISCOVERY
                connection.execute(
                    """
                    INSERT INTO case_runs (
                        case_id, candidate_id, candidate_version, candidate_hash,
                        title, current_state, methodology_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        candidate_id,
                        candidate_version,
                        candidate["candidate_hash"],
                        candidate["title"],
                        current_state.value,
                        json.loads(candidate["payload_json"])["methodology_version"],
                        utc_now(),
                        utc_now(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO state_transitions (
                        case_id, from_state, to_state, reason, actor,
                        recorded_at, decision_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        CaseState.AWAITING_TOPIC_APPROVAL.value,
                        current_state.value,
                        "topic approved",
                        decision["actor"],
                        decision["recorded_at"],
                        decision["decision_id"],
                    ),
                )
            elif outcome != "REJECT":
                raise CaseRegistryError("topic gate accepts only APPROVE or REJECT")

            self._insert_decision(connection, decision, candidate_id=candidate_id)
            candidate_payload = json.loads(candidate["payload_json"])
            balance_impact = canonical_json(
                candidate_payload.get("sample_balance_impact") or {}
            )
            connection.execute(
                """
                INSERT INTO selection_audit (
                    record_id, candidate_id, action, reason, balance_impact,
                    recorded_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"AUDIT-{decision['decision_id']}",
                    candidate_id,
                    "INCLUDED" if outcome == "APPROVE" else "REJECTED",
                    decision["rationale"],
                    balance_impact,
                    decision["recorded_at"],
                    canonical_json({
                        "candidate_id": candidate_id,
                        "decision_id": decision["decision_id"],
                        "action": "INCLUDED" if outcome == "APPROVE" else "REJECTED",
                        "reason": decision["rationale"],
                        "balance_impact": candidate_payload.get("sample_balance_impact") or {},
                        "methodology_version": candidate_payload["methodology_version"],
                    }),
                ),
            )
            connection.execute(
                """
                UPDATE case_candidates SET state = ?, updated_at = ?
                WHERE candidate_id = ? AND version = ?
                """,
                (
                    "REJECTED" if outcome == "REJECT" else "APPROVED",
                    utc_now(),
                    candidate_id,
                    candidate_version,
                ),
            )
            connection.commit()
        return self.get_decision(str(decision["idempotency_key"]))

    def register_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(manifest["case_id"])
        manifest_json = canonical_json(manifest)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)
            if case["manifest_json"]:
                if case["manifest_json"] != manifest_json:
                    raise CaseRegistryError("case manifest is immutable after registration")
                connection.commit()
                return self.get_case(case_id)
            if case["current_state"] not in {
                CaseState.BASKET_DISCOVERY.value,
                CaseState.AWAITING_BASKET_APPROVAL.value,
            }:
                raise CaseRegistryError("manifest can only be registered after topic approval")
            if manifest["candidate_id"] != case["candidate_id"]:
                raise CaseRegistryError("manifest candidate_id does not match approved candidate")
            if manifest["topic_approval_hash"] != case["candidate_hash"]:
                raise CaseRegistryError("manifest targets a stale topic approval")
            if float(manifest["max_project_cost_nis"]) > 100:
                raise CaseRegistryError("project cost ceiling cannot exceed 100 NIS")
            connection.execute(
                "UPDATE case_runs SET manifest_json = ?, updated_at = ? WHERE case_id = ?",
                (manifest_json, utc_now(), case_id),
            )
            connection.commit()
        return self.get_case(case_id)

    def save_basket(self, basket: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(basket["case_id"])
        expected = basket_content_hash(basket)
        if basket.get("basket_hash") != expected:
            raise CaseRegistryError("basket_hash does not match basket content")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)

            existing = connection.execute(
                "SELECT * FROM article_baskets WHERE basket_id = ?",
                (basket["basket_id"],),
            ).fetchone()
            if existing:
                if existing["basket_hash"] != expected:
                    raise CaseRegistryError("basket_id already has different content")
                connection.commit()
                return self._decode_basket(existing)

            if not case["manifest_json"]:
                raise CaseRegistryError("basket requires a registered manifest")
            if case["current_state"] not in {
                CaseState.BASKET_DISCOVERY.value,
                CaseState.AWAITING_BASKET_APPROVAL.value,
                CaseState.READY_TO_RUN.value,
                CaseState.AWAITING_ADJUDICATION.value,
            }:
                raise CaseRegistryError("basket cannot be changed in the current state")
            manifest = json.loads(case["manifest_json"])
            try:
                validate_basket_contract(basket, manifest)
            except CaseGeneratorContractError as error:
                raise CaseRegistryError(str(error)) from error

            latest = connection.execute(
                """
                SELECT * FROM article_baskets
                WHERE case_id = ? ORDER BY version DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            if latest and int(basket["version"]) <= int(latest["version"]):
                raise CaseRegistryError("new basket must increment the version")
            if latest:
                latest_payload = json.loads(latest["payload_json"])
                if not basket_article_payload_changed(latest_payload, basket):
                    raise CaseRegistryError(
                        "new basket version must materially change the article payload"
                    )
            if latest:
                connection.execute(
                    """
                    UPDATE article_baskets
                    SET status = 'SUPERSEDED', superseded_by_hash = ?
                    WHERE basket_id = ?
                    """,
                    (expected, latest["basket_id"]),
                )

            connection.execute(
                """
                INSERT INTO article_baskets (
                    basket_id, case_id, version, basket_hash, status,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basket["basket_id"],
                    case_id,
                    basket["version"],
                    expected,
                    "AWAITING_APPROVAL",
                    canonical_json(basket),
                    basket["created_at"],
                ),
            )
            previous_state = case["current_state"]
            if previous_state != CaseState.AWAITING_BASKET_APPROVAL.value:
                self._transition(
                    connection,
                    case_id,
                    previous_state,
                    CaseState.AWAITING_BASKET_APPROVAL.value,
                    reason="basket version ready for approval",
                    actor="system",
                )
            connection.execute(
                """
                UPDATE case_runs
                SET current_basket_hash = ?, updated_at = ?
                WHERE case_id = ?
                """,
                (expected, utc_now(), case_id),
            )
            connection.commit()
        return self.get_basket(case_id, int(basket["version"]))

    def record_basket_decision(
        self, case_id: str, decision: Mapping[str, Any]
    ) -> dict[str, Any]:
        if decision["gate"] != "BASKET" or decision["case_id"] != case_id:
            raise CaseRegistryError("record_basket_decision requires matching BASKET gate")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)

            cached = self._idempotent_decision(connection, decision)
            if cached is not None:
                connection.commit()
                return cached

            if case["current_state"] != CaseState.AWAITING_BASKET_APPROVAL.value:
                raise CaseRegistryError("case is not awaiting basket approval")
            if decision["target_hash"] != case["current_basket_hash"]:
                raise CaseRegistryError("basket decision targets a stale basket hash")

            outcome = str(decision["decision"])
            targets = {
                "APPROVE": CaseState.READY_TO_RUN,
                "REQUEST_CHANGES": CaseState.BASKET_DISCOVERY,
                "REJECT": CaseState.REJECTED,
            }
            if outcome not in targets:
                raise CaseRegistryError("unknown basket gate outcome")
            destination = targets[outcome]
            self._insert_decision(connection, decision)
            self._transition(
                connection,
                case_id,
                case["current_state"],
                destination.value,
                reason=f"basket gate: {outcome}",
                actor=decision["actor"],
                decision_id=decision["decision_id"],
            )
            if outcome == "APPROVE":
                connection.execute(
                    "UPDATE article_baskets SET status = 'APPROVED' WHERE basket_hash = ?",
                    (decision["target_hash"],),
                )
            elif outcome == "REJECT":
                connection.execute(
                    "UPDATE article_baskets SET status = 'REJECTED' WHERE basket_hash = ?",
                    (decision["target_hash"],),
                )
            connection.commit()
        return self.get_decision(str(decision["idempotency_key"]))

    def advance(self, case_id: str, target: str | CaseState, *, actor: str, reason: str) -> None:
        destination = CaseState(target)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)
            source = CaseState(case["current_state"])
            if source == CaseState.READY_TO_RUN and destination == CaseState.RUNNING:
                if not self._has_current_basket_approval(connection, case):
                    raise CaseRegistryError("RUNNING requires current basket approval")
            self._transition(
                connection,
                case_id,
                source.value,
                destination.value,
                reason=reason,
                actor=actor,
            )
            connection.commit()

    def pause(self, case_id: str, *, actor: str, reason: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)
            source = CaseState(case["current_state"])
            if source not in PAUSABLE_STATES:
                raise CaseRegistryError(f"case cannot pause from {source.value}")
            connection.execute(
                """
                UPDATE case_runs SET resume_state = ?, current_state = ?,
                    failure_reason = NULL, updated_at = ? WHERE case_id = ?
                """,
                (source.value, CaseState.PAUSED.value, utc_now(), case_id),
            )
            self._insert_transition(
                connection, case_id, source.value, CaseState.PAUSED.value,
                reason, actor,
            )
            connection.commit()

    def fail(self, case_id: str, *, actor: str, reason: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)
            source = CaseState(case["current_state"])
            if source not in FAILABLE_STATES:
                raise CaseRegistryError(f"case cannot fail from {source.value}")
            connection.execute(
                """
                UPDATE case_runs SET resume_state = ?, current_state = ?,
                    failure_reason = ?, updated_at = ? WHERE case_id = ?
                """,
                (source.value, CaseState.FAILED.value, reason, utc_now(), case_id),
            )
            self._insert_transition(
                connection, case_id, source.value, CaseState.FAILED.value,
                reason, actor,
            )
            connection.commit()

    def resume(self, case_id: str, *, actor: str, reason: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = self._require_case_row(connection, case_id)
            source = CaseState(case["current_state"])
            if source not in {CaseState.PAUSED, CaseState.FAILED}:
                raise CaseRegistryError("only paused or failed cases can resume")
            if not case["resume_state"]:
                raise CaseRegistryError("case has no resumable state")
            destination = CaseState(case["resume_state"])
            connection.execute(
                """
                UPDATE case_runs SET current_state = ?, resume_state = NULL,
                    failure_reason = NULL, updated_at = ? WHERE case_id = ?
                """,
                (destination.value, utc_now(), case_id),
            )
            self._insert_transition(
                connection, case_id, source.value, destination.value, reason, actor
            )
            connection.commit()

    def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(checkpoint["case_id"])
        payload_json = canonical_json(checkpoint)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with closing(self.connect()) as connection:
            self._require_case_row(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM run_checkpoints WHERE idempotency_key = ?",
                (checkpoint["idempotency_key"],),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise CaseRegistryError("checkpoint idempotency key was reused")
                return json.loads(existing["payload_json"])
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                    checkpoint_id, case_id, stage, status, recorded_at,
                    idempotency_key, payload_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint["checkpoint_id"],
                    case_id,
                    checkpoint["stage"],
                    checkpoint["status"],
                    checkpoint["recorded_at"],
                    checkpoint["idempotency_key"],
                    payload_hash,
                    payload_json,
                ),
            )
            connection.commit()
        return dict(checkpoint)

    def get_candidate(self, candidate_id: str, version: int) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM case_candidates WHERE candidate_id = ? AND version = ?",
                (candidate_id, version),
            ).fetchone()
            if not row:
                raise CaseRegistryError("candidate does not exist")
            return self._decode_candidate(row)

    def list_candidates(self, state: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT candidate.* FROM case_candidates AS candidate
            JOIN (
                SELECT candidate_id, MAX(version) AS version
                FROM case_candidates GROUP BY candidate_id
            ) AS latest
              ON latest.candidate_id = candidate.candidate_id
             AND latest.version = candidate.version
        """
        parameters: tuple[Any, ...] = ()
        if state:
            query += " WHERE candidate.state = ?"
            parameters = (state,)
        query += " ORDER BY candidate.updated_at DESC, candidate.candidate_id"
        with closing(self.connect()) as connection:
            return [
                self._decode_candidate(row)
                for row in connection.execute(query, parameters).fetchall()
            ]

    def list_cases(self, state: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM case_runs"
        parameters: tuple[Any, ...] = ()
        if state:
            query += " WHERE current_state = ?"
            parameters = (state,)
        query += " ORDER BY case_id"
        with closing(self.connect()) as connection:
            results = []
            for row in connection.execute(query, parameters).fetchall():
                result = dict(row)
                result["manifest"] = (
                    json.loads(result.pop("manifest_json"))
                    if result.get("manifest_json")
                    else None
                )
                results.append(result)
            return results

    def next_case_id(self, *, first: int = 2, last: int = 8) -> str:
        occupied = {row["case_id"] for row in self.list_cases()}
        for number in range(first, last + 1):
            candidate = f"CASE-{number:03d}"
            if candidate not in occupied:
                return candidate
        raise CaseRegistryError("all approved case identifiers CASE-002–CASE-008 are assigned")

    def list_selection_audit(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM selection_audit ORDER BY recorded_at, record_id"
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

    def get_case(self, case_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = self._require_case_row(connection, case_id)
            result = dict(row)
            result["manifest"] = (
                json.loads(result.pop("manifest_json"))
                if result.get("manifest_json")
                else None
            )
            return result

    def get_basket(self, case_id: str, version: int | None = None) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            if version is None:
                row = connection.execute(
                    """
                    SELECT * FROM article_baskets
                    WHERE case_id = ? ORDER BY version DESC LIMIT 1
                    """,
                    (case_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM article_baskets WHERE case_id = ? AND version = ?",
                    (case_id, version),
                ).fetchone()
            if not row:
                raise CaseRegistryError("basket does not exist")
            return self._decode_basket(row)

    def get_decision(self, idempotency_key: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM gate_decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if not row:
                raise CaseRegistryError("decision does not exist")
            return json.loads(row["payload_json"])

    def latest_decision(
        self, case_id: str, gate: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT payload_json FROM gate_decisions WHERE case_id = ?"
        parameters: tuple[Any, ...] = (case_id,)
        if gate:
            query += " AND gate = ?"
            parameters = (case_id, gate)
        query += " ORDER BY recorded_at DESC, rowid DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def count_decisions(self, case_id: str, gate: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM gate_decisions WHERE case_id = ?"
        parameters: tuple[Any, ...] = (case_id,)
        if gate:
            query += " AND gate = ?"
            parameters = (case_id, gate)
        with closing(self.connect()) as connection:
            return int(connection.execute(query, parameters).fetchone()[0])

    def latest_checkpoint(self, case_id: str, stage: str | None = None) -> dict[str, Any] | None:
        query = "SELECT payload_json FROM run_checkpoints WHERE case_id = ?"
        parameters: tuple[Any, ...] = (case_id,)
        if stage:
            query += " AND stage = ?"
            parameters = (case_id, stage)
        query += " ORDER BY recorded_at DESC, rowid DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
            return json.loads(row["payload_json"]) if row else None

    @staticmethod
    def _require_case_row(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM case_runs WHERE case_id = ?", (case_id,)
        ).fetchone()
        if not row:
            raise CaseRegistryError("case does not exist or topic was not approved")
        return row

    @staticmethod
    def _decode_candidate(row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["payload_json"])
        result["registry_state"] = row["state"]
        return result

    @staticmethod
    def _decode_basket(row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["payload_json"])
        result["registry_status"] = row["status"]
        result["superseded_by_hash"] = row["superseded_by_hash"]
        return result

    @staticmethod
    def _idempotent_decision(
        connection: sqlite3.Connection, decision: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT payload_hash, payload_json FROM gate_decisions WHERE idempotency_key = ?",
            (decision["idempotency_key"],),
        ).fetchone()
        if not row:
            return None
        payload_json = canonical_json(decision)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if row["payload_hash"] != payload_hash:
            raise CaseRegistryError("decision idempotency key was reused")
        return json.loads(row["payload_json"])

    @staticmethod
    def _insert_decision(
        connection: sqlite3.Connection,
        decision: Mapping[str, Any],
        *,
        candidate_id: str | None = None,
    ) -> None:
        payload_json = canonical_json(decision)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO gate_decisions (
                decision_id, case_id, candidate_id, gate, target_hash, decision, actor,
                recorded_at, rationale, idempotency_key, payload_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision["decision_id"],
                decision.get("case_id"),
                candidate_id or decision.get("candidate_id"),
                decision["gate"],
                decision["target_hash"],
                decision["decision"],
                decision["actor"],
                decision["recorded_at"],
                decision["rationale"],
                decision["idempotency_key"],
                payload_hash,
                payload_json,
            ),
        )

    def _transition(
        self,
        connection: sqlite3.Connection,
        case_id: str,
        source: str,
        target: str,
        *,
        reason: str,
        actor: str,
        decision_id: str | None = None,
    ) -> None:
        try:
            require_transition(source, target)
        except CaseTransitionError as error:
            raise CaseRegistryError(str(error)) from error
        connection.execute(
            """
            UPDATE case_runs SET current_state = ?, updated_at = ?,
                failure_reason = NULL WHERE case_id = ?
            """,
            (target, utc_now(), case_id),
        )
        self._insert_transition(
            connection, case_id, source, target, reason, actor, decision_id
        )

    @staticmethod
    def _insert_transition(
        connection: sqlite3.Connection,
        case_id: str,
        source: str,
        target: str,
        reason: str,
        actor: str,
        decision_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO state_transitions (
                case_id, from_state, to_state, reason, actor,
                recorded_at, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (case_id, source, target, reason, actor, utc_now(), decision_id),
        )

    @staticmethod
    def _has_current_basket_approval(
        connection: sqlite3.Connection, case: sqlite3.Row
    ) -> bool:
        if not case["current_basket_hash"]:
            return False
        row = connection.execute(
            """
            SELECT 1 FROM gate_decisions
            WHERE case_id = ? AND gate = 'BASKET' AND decision = 'APPROVE'
              AND target_hash = ?
            LIMIT 1
            """,
            (case["case_id"], case["current_basket_hash"]),
        ).fetchone()
        return row is not None
