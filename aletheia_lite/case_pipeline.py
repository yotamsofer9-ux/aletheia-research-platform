from __future__ import annotations

import hashlib
import resource
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .case_generator import CaseState
from .case_registry import CaseRegistry
from .case_workspace import CaseWorkspace


PIPELINE_VERSION = "multicase-pipeline-v1"


class PipelineStage(str, Enum):
    COLLECTION = "COLLECTION"
    ANCHORS = "ANCHORS"
    ANALYSIS = "ANALYSIS"
    SECOND_REVIEW = "SECOND_REVIEW"
    FINALIZATION = "FINALIZATION"
    CLOUD_SYNC = "CLOUD_SYNC"
    DASHBOARD = "DASHBOARD"


RUNNING_STAGES = (
    PipelineStage.COLLECTION,
    PipelineStage.ANCHORS,
    PipelineStage.ANALYSIS,
    PipelineStage.SECOND_REVIEW,
)
FINALIZING_STAGES = (
    PipelineStage.FINALIZATION,
    PipelineStage.CLOUD_SYNC,
    PipelineStage.DASHBOARD,
)


class CasePipelineError(RuntimeError):
    """Raised when a bounded multi-case run violates its execution contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024 * 1024) if sys.platform == "darwin" else float(value) / 1024


@dataclass(frozen=True)
class StageContext:
    case_id: str
    manifest: Mapping[str, Any]
    basket: Mapping[str, Any]
    workspace: CaseWorkspace
    remaining_cost_nis: float
    memory_limit_mb: float
    pipeline_version: str


@dataclass(frozen=True)
class StageResult:
    completed_units: int
    total_units: int
    cost_nis: float
    artifact_refs: tuple[str, ...]
    requires_adjudication: bool = False
    summary: str = ""

    def __post_init__(self) -> None:
        if self.completed_units < 0 or self.total_units < 0:
            raise ValueError("stage counters cannot be negative")
        if self.completed_units > self.total_units:
            raise ValueError("completed units cannot exceed total units")
        if self.cost_nis < 0:
            raise ValueError("stage cost cannot be negative")
        if not all(str(ref).strip() for ref in self.artifact_refs):
            raise ValueError("artifact references cannot be empty")


@dataclass(frozen=True)
class PipelineRunResult:
    case_id: str
    state: str
    completed_stages: tuple[str, ...]
    reused_stages: tuple[str, ...]
    cumulative_cost_nis: float
    requires_adjudication: bool


StageHandler = Callable[[StageContext], StageResult]


class CasePipelineRunner:
    """Sequential, checkpointed runner; stage implementations remain explicit."""

    def __init__(
        self,
        *,
        registry: CaseRegistry,
        workspace: CaseWorkspace,
        handlers: Mapping[PipelineStage, StageHandler],
        memory_limit_mb: float = 300,
        pipeline_version: str = PIPELINE_VERSION,
        stop_after_running_stages: bool = False,
    ):
        self.registry = registry
        self.workspace = workspace
        self.handlers = dict(handlers)
        self.memory_limit_mb = float(memory_limit_mb)
        self.pipeline_version = pipeline_version
        self.stop_after_running_stages = bool(stop_after_running_stages)
        missing = set((*RUNNING_STAGES, *FINALIZING_STAGES)) - set(self.handlers)
        if missing:
            raise ValueError(f"missing pipeline handlers: {sorted(item.value for item in missing)}")
        if not 50 <= self.memory_limit_mb <= 1_024:
            raise ValueError("memory limit must be between 50 and 1024 MB")

    def _idempotency_key(self, stage: PipelineStage, basket_hash: str) -> str:
        digest = hashlib.sha256(
            f"{self.workspace.case_id}|{basket_hash}|{stage.value}|{self.pipeline_version}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        return f"{self.workspace.case_id}-{stage.value.lower()}-{digest}"

    def _completed_checkpoint(
        self, stage: PipelineStage, basket_hash: str
    ) -> dict[str, Any] | None:
        checkpoint = self.registry.latest_checkpoint(
            self.workspace.case_id, stage.value
        )
        if (
            checkpoint
            and checkpoint.get("status") == "COMPLETE"
            and checkpoint.get("idempotency_key")
            == self._idempotency_key(stage, basket_hash)
        ):
            return checkpoint
        return None

    def _cumulative_cost(self, basket_hash: str) -> float:
        total = 0.0
        for stage in (*RUNNING_STAGES, *FINALIZING_STAGES):
            checkpoint = self._completed_checkpoint(stage, basket_hash)
            if checkpoint:
                total += float(checkpoint["cost_nis"])
        return total

    def _run_stage(
        self,
        stage: PipelineStage,
        manifest: Mapping[str, Any],
        basket: Mapping[str, Any],
        cumulative_cost: float,
    ) -> tuple[StageResult, bool]:
        basket_hash = str(basket["basket_hash"])
        cached = self._completed_checkpoint(stage, basket_hash)
        if cached:
            return (
                StageResult(
                    completed_units=int(cached["completed_units"]),
                    total_units=int(cached["total_units"]),
                    cost_nis=float(cached["cost_nis"]),
                    artifact_refs=tuple(cached["artifact_refs"]),
                    requires_adjudication=bool(
                        cached.get("requires_adjudication", False)
                    ),
                    summary=str(cached.get("summary") or ""),
                ),
                True,
            )
        limit = float(manifest["max_project_cost_nis"])
        remaining = limit - cumulative_cost
        if remaining < 0:
            raise CasePipelineError("project cost cap was already exceeded")
        if _peak_rss_mb() > self.memory_limit_mb:
            raise CasePipelineError("memory limit reached before stage execution")
        context = StageContext(
            case_id=self.workspace.case_id,
            manifest=manifest,
            basket=basket,
            workspace=self.workspace,
            remaining_cost_nis=remaining,
            memory_limit_mb=self.memory_limit_mb,
            pipeline_version=self.pipeline_version,
        )
        result = self.handlers[stage](context)
        if result.completed_units != result.total_units:
            raise CasePipelineError(f"stage {stage.value} returned incomplete work")
        if result.cost_nis > remaining:
            raise CasePipelineError(
                f"stage {stage.value} exceeds the remaining project cost cap"
            )
        peak = _peak_rss_mb()
        if peak > self.memory_limit_mb:
            raise CasePipelineError(f"stage {stage.value} exceeded the memory limit")
        checkpoint = {
            "checkpoint_id": (
                f"CHECKPOINT-{self.workspace.case_id}-{stage.value}-"
                + self._idempotency_key(stage, basket_hash).rsplit("-", 1)[-1].upper()
            ),
            "case_id": self.workspace.case_id,
            "stage": stage.value,
            "status": "COMPLETE",
            "completed_units": result.completed_units,
            "total_units": result.total_units,
            "cost_nis": result.cost_nis,
            "peak_rss_mb": peak,
            "artifact_refs": list(result.artifact_refs),
            "recorded_at": _utc_now(),
            "failure_reason": None,
            "idempotency_key": self._idempotency_key(stage, basket_hash),
            "requires_adjudication": result.requires_adjudication,
            "summary": result.summary,
        }
        self.registry.save_checkpoint(checkpoint)
        return result, False

    def run(self) -> PipelineRunResult:
        case = self.registry.get_case(self.workspace.case_id)
        manifest = case.get("manifest")
        if not manifest:
            raise CasePipelineError("case has no approved manifest")
        basket = self.registry.get_basket(self.workspace.case_id)
        if basket.get("registry_status") != "APPROVED":
            raise CasePipelineError("runner requires the exact approved basket")

        state = CaseState(case["current_state"])
        if state == CaseState.CLOSED:
            return PipelineRunResult(
                case_id=self.workspace.case_id,
                state=state.value,
                completed_stages=(),
                reused_stages=tuple(
                    stage.value for stage in (*RUNNING_STAGES, *FINALIZING_STAGES)
                ),
                cumulative_cost_nis=self._cumulative_cost(basket["basket_hash"]),
                requires_adjudication=False,
            )
        if state in {CaseState.PAUSED, CaseState.FAILED}:
            self.registry.resume(
                self.workspace.case_id,
                actor="multicase-runner",
                reason="resume from the latest completed checkpoint",
            )
            state = CaseState(
                self.registry.get_case(self.workspace.case_id)["current_state"]
            )
        if state == CaseState.AWAITING_ADJUDICATION:
            return PipelineRunResult(
                case_id=self.workspace.case_id,
                state=state.value,
                completed_stages=(),
                reused_stages=(),
                cumulative_cost_nis=self._cumulative_cost(basket["basket_hash"]),
                requires_adjudication=True,
            )
        if state == CaseState.READY_TO_RUN:
            self.registry.advance(
                self.workspace.case_id,
                CaseState.RUNNING,
                actor="multicase-runner",
                reason="approved basket begins bounded research pipeline",
            )
            state = CaseState.RUNNING
        if state not in {CaseState.RUNNING, CaseState.FINALIZING}:
            raise CasePipelineError(f"case cannot run from state {state.value}")

        completed: list[str] = []
        reused: list[str] = []
        cumulative = self._cumulative_cost(basket["basket_hash"])
        try:
            if state == CaseState.RUNNING:
                for stage in RUNNING_STAGES:
                    result, was_reused = self._run_stage(
                        stage, manifest, basket, cumulative
                    )
                    cumulative += 0 if was_reused else result.cost_nis
                    (reused if was_reused else completed).append(stage.value)
                    if result.requires_adjudication:
                        self.registry.advance(
                            self.workspace.case_id,
                            CaseState.AWAITING_ADJUDICATION,
                            actor="multicase-runner",
                            reason="hard conflict survived autonomous second review",
                        )
                        return PipelineRunResult(
                            case_id=self.workspace.case_id,
                            state=CaseState.AWAITING_ADJUDICATION.value,
                            completed_stages=tuple(completed),
                            reused_stages=tuple(reused),
                            cumulative_cost_nis=cumulative,
                            requires_adjudication=True,
                        )
                self.registry.advance(
                    self.workspace.case_id,
                    CaseState.FINALIZING,
                    actor="multicase-runner",
                    reason="automated research stages completed without unresolved conflict",
                )
                if self.stop_after_running_stages:
                    return PipelineRunResult(
                        case_id=self.workspace.case_id,
                        state=CaseState.FINALIZING.value,
                        completed_stages=tuple(completed),
                        reused_stages=tuple(reused),
                        cumulative_cost_nis=cumulative,
                        requires_adjudication=False,
                    )

            for stage in FINALIZING_STAGES:
                result, was_reused = self._run_stage(stage, manifest, basket, cumulative)
                cumulative += 0 if was_reused else result.cost_nis
                (reused if was_reused else completed).append(stage.value)
            self.registry.advance(
                self.workspace.case_id,
                CaseState.CLOSED,
                actor="multicase-runner",
                reason="results, cloud synchronization, and dashboard completed",
            )
        except Exception as error:
            current = CaseState(
                self.registry.get_case(self.workspace.case_id)["current_state"]
            )
            if current in {CaseState.RUNNING, CaseState.FINALIZING}:
                self.registry.fail(
                    self.workspace.case_id,
                    actor="multicase-runner",
                    reason=str(error),
                )
            raise

        return PipelineRunResult(
            case_id=self.workspace.case_id,
            state=CaseState.CLOSED.value,
            completed_stages=tuple(completed),
            reused_stages=tuple(reused),
            cumulative_cost_nis=cumulative,
            requires_adjudication=False,
        )
