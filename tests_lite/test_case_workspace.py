from __future__ import annotations

import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aletheia_lite.case_generator_contracts import basket_content_hash
from aletheia_lite.case_generator_ui import approve_topic_candidate, decide_article_basket
from aletheia_lite.case_registry import CaseRegistry
from aletheia_lite.case_workspace import (
    CaseWorkspace,
    CaseWorkspaceError,
    initialize_case_workspace,
    materialize_analysis_inputs,
)
from aletheia_lite.snapshots import LocalSnapshotStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "case_generator/fixtures/case002_offline_fixture_v1.json").read_text(
        encoding="utf-8"
    )
)


class CaseWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = CaseRegistry(self.root / "case_registry.sqlite3")
        candidate = copy.deepcopy(FIXTURE["candidate"])
        self.registry.register_candidate(candidate)
        self.case_id = approve_topic_candidate(
            self.registry,
            candidate,
            actor="principal-researcher",
            rationale="offline workspace test",
        )
        self.manifest = self.registry.get_case(self.case_id)["manifest"]
        self.staging_root = self.root / "staging"
        snapshot_store = LocalSnapshotStore(
            self.staging_root / self.case_id / "snapshots"
        )
        basket = copy.deepcopy(FIXTURE["basket"])
        basket["case_id"] = self.case_id
        basket["basket_id"] = f"BASKET-{self.case_id}-V1"
        for index, article in enumerate(basket["articles"]):
            body = (
                f"<html><head><title>{article['title']}</title></head>"
                f"<body><article><p>Offline article {index} with enough "
                "deterministic research text for the approved fixture.</p></article>"
                "</body></html>"
            ).encode("utf-8")
            receipt = snapshot_store.put(
                case_id=self.case_id,
                source_id=article["source_id"],
                content=body,
                media_type="text/html",
            )
            article["snapshot_ref"] = receipt.ref
            article["snapshot_hash"] = receipt.sha256
        basket["basket_hash"] = basket_content_hash(basket)
        self.registry.save_basket(basket)
        decide_article_basket(
            self.registry,
            self.case_id,
            decision="APPROVE",
            actor="principal-researcher",
            rationale="approved offline basket",
        )
        self.basket = self.registry.get_basket(self.case_id)

    def test_approved_basket_materializes_isolated_replay_safe_workspace(self):
        workspace = CaseWorkspace(self.root, self.case_id)
        first = initialize_case_workspace(
            workspace,
            self.manifest,
            self.basket,
            staging_root=self.staging_root,
        )
        second = initialize_case_workspace(
            workspace,
            self.manifest,
            self.basket,
            staging_root=self.staging_root,
        )
        self.assertEqual(first["article_count"], 27)
        self.assertEqual(first["promoted_snapshots"], 27)
        self.assertEqual(second["promoted_snapshots"], 0)
        self.assertEqual(second["reused_snapshots"], 27)
        self.assertTrue(workspace.research_db.exists())
        self.assertNotIn("case001", str(workspace.case_root).lower())

    def test_unapproved_basket_cannot_enter_workspace(self):
        unapproved = dict(self.basket)
        unapproved["registry_status"] = "AWAITING_APPROVAL"
        with self.assertRaisesRegex(CaseWorkspaceError, "approved basket"):
            initialize_case_workspace(
                CaseWorkspace(self.root, self.case_id),
                self.manifest,
                unapproved,
                staging_root=self.staging_root,
            )

    def test_analysis_inputs_are_derived_from_approved_workspace(self):
        workspace = CaseWorkspace(self.root, self.case_id)
        initialize_case_workspace(
            workspace,
            self.manifest,
            self.basket,
            staging_root=self.staging_root,
        )
        result = materialize_analysis_inputs(workspace)
        roster = json.loads(
            (workspace.analysis_dir / "analysis_roster.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["documents"], 27)
        self.assertEqual(roster["basket_hash"], self.basket["basket_hash"])
        self.assertEqual(len(roster["document_ids"]), 27)
        self.assertEqual(len(set(roster["document_ids"])), 27)

    def test_snapshot_tampering_is_detected_before_analysis(self):
        article = self.basket["articles"][0]
        relative = Path(article["snapshot_ref"].removeprefix("local-snapshot://"))
        snapshot = self.staging_root / self.case_id / "snapshots" / relative
        snapshot.write_bytes(gzip.compress(b"<html><p>tampered</p></html>", mtime=0))
        with self.assertRaisesRegex(CaseWorkspaceError, "hash mismatch"):
            initialize_case_workspace(
                CaseWorkspace(self.root, self.case_id),
                self.manifest,
                self.basket,
                staging_root=self.staging_root,
            )

    def test_approved_basket_revision_retires_old_document_without_deleting_it(self):
        workspace = CaseWorkspace(self.root, self.case_id)
        initialize_case_workspace(
            workspace,
            self.manifest,
            self.basket,
            staging_root=self.staging_root,
        )
        first_inputs = materialize_analysis_inputs(workspace)
        first_roster = json.loads(
            (workspace.analysis_dir / "analysis_roster.json").read_text(encoding="utf-8")
        )

        revised = copy.deepcopy(self.basket)
        revised["basket_id"] = f"BASKET-{self.case_id}-V2"
        revised["version"] = 2
        revised["status"] = "APPROVED"
        revised["registry_status"] = "APPROVED"
        body = (
            "<html><head><title>Replacement</title></head><body><article>"
            "<p>A replacement article with enough deterministic research text "
            "for the approved fixture and its bounded revision test.</p>"
            "</article></body></html>"
        ).encode("utf-8")
        snapshot_store = LocalSnapshotStore(
            self.staging_root / self.case_id / "snapshots"
        )
        snapshot = snapshot_store.put(
            case_id=self.case_id,
            source_id=revised["articles"][0]["source_id"],
            content=body,
            media_type="text/html",
        )
        article = revised["articles"][0]
        article["article_id"] = f"ARTICLE-{self.case_id}-{snapshot.sha256[:16].upper()}"
        article["url"] = "https://example.com/replacement"
        article["canonical_url"] = article["url"]
        article["title"] = "Replacement"
        article["snapshot_ref"] = snapshot.ref
        article["snapshot_hash"] = snapshot.sha256
        revised["basket_hash"] = basket_content_hash(revised)

        receipt = initialize_case_workspace(
            workspace,
            self.manifest,
            revised,
            staging_root=self.staging_root,
        )
        second_inputs = materialize_analysis_inputs(workspace)
        second_roster = json.loads(
            (workspace.analysis_dir / "analysis_roster.json").read_text(encoding="utf-8")
        )

        self.assertEqual(27, first_inputs["documents"])
        self.assertEqual(27, second_inputs["documents"])
        self.assertEqual(1, receipt["retired_documents"])
        self.assertEqual(26, len(set(first_roster["document_ids"]) & set(second_roster["document_ids"])))
        self.assertTrue(
            (workspace.revisions_dir / self.basket["basket_hash"][:16] / "approved_basket.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
