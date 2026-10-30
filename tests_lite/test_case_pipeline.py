from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aletheia_lite.case_generator_contracts import basket_content_hash
from aletheia_lite.case_generator_ui import approve_topic_candidate, decide_article_basket
from aletheia_lite.case_pipeline import (
    CasePipelineError,
    CasePipelineRunner,
    PipelineStage,
    StageResult,
)
from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_workspace import CaseWorkspace


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "case_generator/fixtures/case002_offline_fixture_v1.json").read_text(
        encoding="utf-8"
    )
)


class CasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = CaseRegistry(self.root / "registry.sqlite3")
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        self.case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="offline pipeline test",
        )
        basket = copy.deepcopy(FIXTURE["basket"])
        basket["case_id"] = self.case_id
        basket["basket_id"] = f"BASKET-{self.case_id}-V1"
        basket["basket_hash"] = basket_content_hash(basket)
        self.registry.save_basket(basket)
        decide_article_basket(
            self.registry,
            self.case_id,
            decision="APPROVE",
            actor="principal-researcher",
            rationale="approved offline pipeline basket",
        )
        self.workspace = CaseWorkspace(self.root, self.case_id)

    def _handlers(self, *, conflict=False, fail_once=None, expensive=None):
        calls = {stage: 0 for stage in PipelineStage if stage != PipelineStage("COLLECTION")}
        calls[PipelineStage.COLLECTION] = 0

        def make_handler(stage):
            def handler(context):
                calls[stage] += 1
                if fail_once == stage and calls[stage] == 1:
                    raise RuntimeError(f"synthetic {stage.value} interruption")
                artifact = context.workspace.write_json_artifact(
                    "analysis" if stage not in {PipelineStage.FINALIZATION, PipelineStage.DASHBOARD} else "results",
                    f"{stage.value.lower()}.json",
                    {
                        "schema_version": "offline-fixture-v1",
                        "case_id": context.case_id,
                        "stage": stage.value,
                        "synthetic": True,
                    },
                )
                return StageResult(
                    completed_units=1,
                    total_units=1,
                    cost_nis=(101 if expensive == stage else 1),
                    artifact_refs=(artifact,),
                    requires_adjudication=(
                        conflict and stage == PipelineStage.SECOND_REVIEW
                    ),
                    summary="offline fixture stage",
                )

            return handler

        handlers = {
            stage: make_handler(stage)
            for stage in (
                PipelineStage.COLLECTION,
                PipelineStage.ANCHORS,
                PipelineStage.ANALYSIS,
                PipelineStage.SECOND_REVIEW,
                PipelineStage.FINALIZATION,
                PipelineStage.CLOUD_SYNC,
                PipelineStage.DASHBOARD,
            )
        }
        return handlers, calls

    def test_offline_fixture_completes_without_case001_paths_and_replays(self):
        handlers, calls = self._handlers()
        runner = CasePipelineRunner(
            registry=self.registry,
            workspace=self.workspace,
            handlers=handlers,
            memory_limit_mb=1024,
        )
        result = runner.run()
        self.assertEqual(result.state, "CLOSED")
        self.assertEqual(len(result.completed_stages), 7)
        self.assertEqual(result.cumulative_cost_nis, 7)
        for stage in handlers:
            self.assertEqual(calls[stage], 1)
            checkpoint = self.registry.latest_checkpoint(self.case_id, stage.value)
            self.assertEqual(checkpoint["status"], "COMPLETE")
            self.assertNotIn("case001", json.dumps(checkpoint).lower())

        replay = runner.run()
        self.assertEqual(replay.state, "CLOSED")
        self.assertEqual(len(replay.reused_stages), 7)
        for stage in handlers:
            self.assertEqual(calls[stage], 1)

    def test_hard_conflict_stops_before_finalization(self):
        handlers, calls = self._handlers(conflict=True)
        result = CasePipelineRunner(
            registry=self.registry,
            workspace=self.workspace,
            handlers=handlers,
            memory_limit_mb=1024,
        ).run()
        self.assertEqual(result.state, "AWAITING_ADJUDICATION")
        self.assertTrue(result.requires_adjudication)
        self.assertEqual(calls[PipelineStage.FINALIZATION], 0)
        self.assertEqual(calls[PipelineStage.CLOUD_SYNC], 0)

    def test_interrupted_stage_resumes_from_completed_checkpoints(self):
        handlers, calls = self._handlers(fail_once=PipelineStage.ANALYSIS)
        runner = CasePipelineRunner(
            registry=self.registry,
            workspace=self.workspace,
            handlers=handlers,
            memory_limit_mb=1024,
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic ANALYSIS"):
            runner.run()
        self.assertEqual(self.registry.get_case(self.case_id)["current_state"], "FAILED")
        result = runner.run()
        self.assertEqual(result.state, "CLOSED")
        self.assertEqual(calls[PipelineStage.COLLECTION], 1)
        self.assertEqual(calls[PipelineStage.ANCHORS], 1)
        self.assertEqual(calls[PipelineStage.ANALYSIS], 2)

    def test_project_cost_cap_is_a_hard_gate(self):
        handlers, _ = self._handlers(expensive=PipelineStage.COLLECTION)
        runner = CasePipelineRunner(
            registry=self.registry,
            workspace=self.workspace,
            handlers=handlers,
            memory_limit_mb=1024,
        )
        with self.assertRaisesRegex(CasePipelineError, "cost cap"):
            runner.run()
        self.assertEqual(self.registry.get_case(self.case_id)["current_state"], "FAILED")


if __name__ == "__main__":
    unittest.main()
