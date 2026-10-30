from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from aletheia_lite.basket_builder import (
    ArticleLead,
    SelectedArticle,
    StagedArticle,
    load_publisher_roster,
)
from aletheia_lite.basket_progress import (
    case_run_lock,
    input_signature,
    load_attempt_cache,
    load_lead_cache,
    load_run_state,
    load_stage_cache,
    mark_snapshot_recovery_complete,
    previous_basket_leads,
    process_is_alive,
    recover_staged_from_snapshot_cache,
    restore_staged_from_previous_basket,
    run_is_active,
    save_attempt_cache,
    save_lead_cache,
    save_run_state,
    save_stage_cache,
    snapshot_recovery_is_complete,
)
from aletheia_lite.snapshots import LocalSnapshotStore

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "config/multicase_publisher_roster_v1.json"


class BasketProgressTests(unittest.TestCase):
    def setUp(self):
        self.roster = load_publisher_roster(ROSTER_PATH)
        self.manifest = {
            "case_id": "CASE-TEST",
            "window_start": "2024-04-01",
            "window_end": "2024-04-08",
        }
        self.signature = input_signature(self.manifest, self.roster)

    def staged_article(self):
        slot = self.roster.slots[0]
        lead = ArticleLead(
            source_id=slot.primary.source_id,
            url=f"https://{slot.primary.domain}/article",
            title="Test article",
            published_at="2024-04-01T08:00:00Z",
            provider_id="fixture",
            sample_phase="INITIAL_REPORT",
        )
        return StagedArticle(
            selected=SelectedArticle(slot=slot, outlet=slot.primary, lead=lead),
            canonical_url=lead.url,
            title=lead.title,
            published_at=lead.published_at,
            snapshot_ref="local-snapshot://CASE-TEST/source/hash.gz",
            snapshot_hash="a" * 64,
            content_hash="b" * 64,
            fetch_method="DIRECT",
        )

    def test_state_survives_reload_and_detects_active_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_run_state(
                temporary,
                "CASE-TEST",
                "RUN-1",
                "RUNNING",
                pid=os.getpid(),
                articles_staged=4,
            )
            state = load_run_state(temporary, "CASE-TEST")
            self.assertEqual(state["articles_staged"], 4)
            self.assertTrue(run_is_active(state))
            state["pid"] = 999_999_999
            self.assertFalse(run_is_active(state))

    @patch("aletheia_lite.basket_progress.os.kill")
    @patch("aletheia_lite.basket_progress.subprocess.run")
    def test_zombie_process_does_not_keep_run_active(self, mock_run, mock_kill):
        mock_kill.return_value = None
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Z+\n"
        self.assertFalse(process_is_alive(321))

    def test_attempt_cache_roundtrip_and_signature_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_attempt_cache(
                temporary,
                "CASE-002",
                "signature-a",
                {"https://example.com/a": 1, "https://example.com/b": 2},
            )
            self.assertEqual(
                load_attempt_cache(temporary, "CASE-002", "signature-a"),
                {
                    "https://example.com/a": 1,
                    "https://example.com/b": 2,
                },
            )
            self.assertEqual(
                load_attempt_cache(temporary, "CASE-002", "signature-b"), {}
            )

    def test_recovers_matching_legacy_snapshot_into_correct_source(self):
        slot = self.roster.slots[0]
        lead = ArticleLead(
            source_id=slot.primary.source_id,
            url=f"https://{slot.primary.domain}/matching-article",
            title="Recovered article",
            published_at="2024-04-01T08:00:00Z",
            provider_id="fixture",
            sample_phase="INITIAL_REPORT",
        )
        body = " ".join(["World Central Kitchen evidence"] * 30)
        html = (
            "<html><head><title>Recovered article</title>"
            f'<meta property="og:url" content="{lead.url}"></head>'
            f"<body><article><p>{body}</p></article></body></html>"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalSnapshotStore(
                Path(temporary) / "CASE-TEST" / "snapshots"
            )
            store.put(
                case_id="CASE-TEST",
                source_id="legacy-wrong-source",
                content=html,
                media_type="text/html",
            )
            recovered = recover_staged_from_snapshot_cache(
                {"case_id": "CASE-TEST"},
                [lead],
                self.roster,
                temporary,
                [],
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].selected.outlet.source_id, lead.source_id)
            self.assertEqual(recovered[0].fetch_method, "LOCAL_CACHE")

    def test_snapshot_recovery_marker_is_scoped_to_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(
                snapshot_recovery_is_complete(temporary, "CASE-002", "one")
            )
            mark_snapshot_recovery_complete(temporary, "CASE-002", "one")
            self.assertTrue(
                snapshot_recovery_is_complete(temporary, "CASE-002", "one")
            )
            self.assertFalse(
                snapshot_recovery_is_complete(temporary, "CASE-002", "two")
            )

    def test_previous_basket_leads_survive_roster_signature_change(self):
        staged = self.staged_article()
        basket = {
            "articles": [
                {
                    "source_id": staged.selected.outlet.source_id,
                    "url": staged.selected.lead.url,
                    "title": staged.title,
                    "published_at": staged.published_at,
                    "sample_phase": staged.selected.lead.sample_phase,
                },
                {
                    "source_id": "removed-source",
                    "url": "https://example.com/removed",
                    "title": "Removed",
                    "published_at": "2024-04-01",
                    "sample_phase": "INITIAL_REPORT",
                },
            ]
        }
        restored = previous_basket_leads(basket, self.roster)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].provider_id, "previous-basket")
        self.assertEqual(restored[0].source_id, staged.selected.outlet.source_id)

    def test_previous_basket_restores_verified_snapshot_directly(self):
        staged = self.staged_article()
        body = " ".join(["World Central Kitchen verified evidence"] * 30)
        html = (
            "<html><head><title>Recovered prior article</title></head>"
            f"<body><article><p>{body}</p></article></body></html>"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalSnapshotStore(
                Path(temporary) / "CASE-TEST" / "snapshots"
            )
            receipt = store.put(
                case_id="CASE-TEST",
                source_id=staged.selected.outlet.source_id,
                content=html,
                media_type="text/html",
            )
            basket = {
                "case_id": "CASE-TEST",
                "articles": [
                    {
                        "source_id": staged.selected.outlet.source_id,
                        "url": staged.selected.lead.url,
                        "canonical_url": staged.canonical_url,
                        "title": staged.title,
                        "published_at": staged.published_at,
                        "sample_phase": staged.selected.lead.sample_phase,
                        "snapshot_ref": receipt.ref,
                        "snapshot_hash": receipt.sha256,
                    }
                ],
            }
            restored = restore_staged_from_previous_basket(
                basket, self.roster, temporary, []
            )
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].fetch_method, "LOCAL_CACHE")
            self.assertEqual(restored[0].snapshot_hash, receipt.sha256)

    def test_leads_and_staged_articles_round_trip(self):
        staged = self.staged_article()
        with tempfile.TemporaryDirectory() as temporary:
            save_lead_cache(
                temporary,
                "CASE-TEST",
                self.signature,
                [staged.selected.lead],
                {"fixture": 1},
            )
            leads, counts = load_lead_cache(
                temporary, "CASE-TEST", self.signature
            )
            self.assertEqual(leads, [staged.selected.lead])
            self.assertEqual(counts, {"fixture": 1})

            save_stage_cache(
                temporary, "CASE-TEST", self.signature, [staged]
            )
            restored = load_stage_cache(
                temporary,
                "CASE-TEST",
                self.signature,
                self.roster,
            )
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].content_hash, staged.content_hash)
            self.assertEqual(
                restored[0].selected.slot.slot_id,
                staged.selected.slot.slot_id,
            )

    def test_empty_lead_cache_is_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            save_lead_cache(
                temporary,
                "CASE-TEST",
                self.signature,
                [],
                {"google-news-rss": 0, "gdelt-doc-2": 0},
            )
            self.assertIsNone(
                load_lead_cache(temporary, "CASE-TEST", self.signature)
            )

    def test_lock_rejects_a_second_live_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            with case_run_lock(temporary, "CASE-TEST", "RUN-1"):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with case_run_lock(temporary, "CASE-TEST", "RUN-2"):
                        pass

    def test_stage_cache_drops_a_stale_listing_page(self):
        staged = self.staged_article()
        listing_lead = ArticleLead(
            source_id=staged.selected.lead.source_id,
            url="https://example.com/Tagit/iran-attack",
            title=staged.title,
            published_at=staged.published_at,
            provider_id=staged.selected.lead.provider_id,
            sample_phase=staged.selected.lead.sample_phase,
        )
        listing = StagedArticle(
            selected=SelectedArticle(
                slot=staged.selected.slot,
                outlet=staged.selected.outlet,
                lead=listing_lead,
            ),
            canonical_url=listing_lead.url,
            title=listing_lead.title,
            published_at=listing_lead.published_at,
            snapshot_ref=staged.snapshot_ref,
            snapshot_hash=staged.snapshot_hash,
            content_hash=staged.content_hash,
            fetch_method=staged.fetch_method,
        )
        with tempfile.TemporaryDirectory() as temporary:
            save_stage_cache(
                temporary, "CASE-TEST", self.signature, [listing]
            )
            restored = load_stage_cache(
                temporary, "CASE-TEST", self.signature, self.roster
            )
            self.assertEqual(restored, [])


if __name__ == "__main__":
    unittest.main()
