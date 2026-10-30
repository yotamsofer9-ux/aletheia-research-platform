from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from aletheia_lite.basket_builder import (
    ArticleLead,
    BasketBuildError,
    GoogleNewsBasketLeadProvider,
    SelectedArticle,
    build_basket_payload,
    classify_sample_phase,
    is_probable_article_url,
    load_publisher_roster,
    load_reviewed_leads,
    missing_roster_source_ids,
    publisher_roster_path,
    select_roster_articles,
    stage_selected_articles,
)
from aletheia_lite.collector import HTTPResult
from aletheia_lite.collector import _matches_keywords


ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "config/multicase_publisher_roster_v1.json"
FIXTURE_PATH = ROOT / "case_generator/fixtures/case002_offline_fixture_v1.json"


class FakeArticleClient:
    def get(self, url, params=None):
        body = (
            "synthetic event generator test אירוע סינתטי حدث اصطناعي "
            + url
            + " "
        ) * 12
        html = (
            f"<html><head><title>{url}</title></head>"
            f"<body><article><p>{body}</p></article></body></html>"
        ).encode("utf-8")
        return HTTPResult(
            url=url,
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=html,
        )


class FakeGoogleNewsClient:
    def __init__(self, domain):
        self.domain = domain
        self.search_params = None

    def get(self, url, params=None):
        if url.endswith("/rss/search"):
            self.search_params = params
            items = []
            for index, published in enumerate(
                (
                    "Mon, 01 Apr 2024 08:00:00 GMT",
                    "Wed, 03 Apr 2024 08:00:00 GMT",
                    "Sat, 06 Apr 2024 08:00:00 GMT",
                )
            ):
                items.append(
                    "<item>"
                    f"<title>Article {index} - Test Source</title>"
                    f"<link>https://news.google.com/rss/articles/id{index}</link>"
                    f"<pubDate>{published}</pubDate>"
                    "<source>Test Source</source>"
                    "</item>"
                )
            body = (
                "<?xml version='1.0' encoding='UTF-8'?><rss><channel>"
                + "".join(items)
                + "</channel></rss>"
            ).encode("utf-8")
        else:
            article_id = url.split("/articles/", 1)[1].split("?", 1)[0]
            body = (
                f'<html data-n-a-sg="sig-{article_id}" '
                'data-n-a-ts="1711970000"></html>'
            ).encode("utf-8")
        return HTTPResult(url=url, status_code=200, headers={}, content=body)

    def post_form(self, url, data):
        envelopes = json.loads(data["f.req"])[0]
        rows = []
        for envelope in envelopes:
            inner = json.loads(envelope[1])
            article_id = inner[2]
            request_id = envelope[3]
            payload = json.dumps(
                ["garturlres", f"https://{self.domain}/{article_id}"]
            )
            rows.append(["wrb.fr", "Fbv4je", payload, None, request_id])
        return HTTPResult(
            url=url,
            status_code=200,
            headers={},
            content=json.dumps(rows).encode("utf-8"),
        )


class GenericActorOnlyClient:
    def get(self, url, params=None):
        body = ("Hezbollah regional conflict context without the named event. " * 12)
        html = (
            "<html><head><title>Generic Hezbollah update</title></head>"
            f"<body><article><p>{body}</p></article></body></html>"
        ).encode("utf-8")
        return HTTPResult(
            url=url,
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=html,
        )


class BasketBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roster = load_publisher_roster(ROSTER_PATH)
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.manifest = cls.fixture["manifest"]

    def test_obvious_listing_urls_are_not_articles(self):
        self.assertFalse(
            is_probable_article_url("https://www.mako.co.il/Tagit/iran-attack")
        )
        self.assertFalse(is_probable_article_url("https://example.com/search/iran"))
        self.assertTrue(
            is_probable_article_url("https://example.com/news/article/iran-attack")
        )

    def test_keyword_matching_tolerates_joined_arabic_names(self):
        self.assertTrue(_matches_keywords("مقتل حسن نصر الله", ("حسن نصرالله",)))

    def test_staging_does_not_accept_a_generic_third_keyword(self):
        slot = next(
            item
            for item in self.roster.slots
            if item.slot_id == "western-guardian"
        )
        manifest = dict(self.manifest)
        manifest["keywords"] = {
            "he": ["חיסול נסראללה", "חסן נסראללה", "חיזבאללה"],
            "en": ["Nasrallah killed", "Hassan Nasrallah", "Hezbollah"],
            "ar": ["مقتل نصرالله", "حسن نصرالله", "حزب الله"],
        }
        selected = [
            SelectedArticle(
                slot=slot,
                outlet=slot.primary,
                lead=ArticleLead(
                    source_id=slot.primary.source_id,
                    url="https://theguardian.com/world/generic-hezbollah-update",
                    title="Generic Hezbollah update",
                    published_at="2024-04-01T08:00:00Z",
                    provider_id="fixture-provider",
                    sample_phase="INITIAL_REPORT",
                ),
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            report = stage_selected_articles(
                manifest,
                selected,
                temporary,
                client=GenericActorOnlyClient(),
            )
        self.assertEqual((), report.staged)
        self.assertIn("configured case keyword", report.failures[0].message)

    def build_leads(self, *, split_slot=False):
        dates = {
            "INITIAL_REPORT": "2024-04-01T08:00:00Z",
            "EARLY_UPDATE": "2024-04-02T08:00:00Z",
            "LATER_INVESTIGATION": "2024-04-06T08:00:00Z",
        }
        leads = []
        for slot in self.roster.slots:
            for phase in dates:
                outlet = slot.primary
                if (
                    split_slot
                    and slot.slot_id == "western-associated-press"
                    and phase == "LATER_INVESTIGATION"
                ):
                    outlet = slot.substitutes[0]
                leads.append(
                    ArticleLead(
                        source_id=outlet.source_id,
                        url=f"https://{outlet.domain}/{phase.lower()}-{slot.slot_id}",
                        title=f"{slot.slot_id} {phase}",
                        published_at=dates[phase],
                        provider_id="fixture-provider",
                        sample_phase=phase,
                    )
                )
        return leads

    def test_roster_preserves_nine_comparable_slots(self):
        self.assertEqual(len(self.roster.slots), 9)
        self.assertEqual(
            Counter(slot.language for slot in self.roster.slots),
            {"he": 3, "en": 3, "ar": 3},
        )
        self.assertEqual(
            {slot.slot_id for slot in self.roster.slots},
            {
                "domestic-israel-hayom",
                "domestic-mekomit",
                "domestic-n12",
                "western-guardian",
                "western-associated-press",
                "western-washington-post",
                "regional-al-jazeera-arabic",
                "regional-bbc-arabic",
                "regional-al-araby-tv",
            },
        )
        local_left_slot = next(
            slot
            for slot in self.roster.slots
            if slot.slot_id == "domestic-mekomit"
        )
        self.assertEqual(
            [outlet.source_id for outlet in local_left_slot.substitutes],
            ["zman-yisrael-hebrew-substitute", "davar-hebrew-substitute"],
        )
        self.assertTrue(
            all(
                outlet.editorial_profile == local_left_slot.editorial_profile
                for outlet in local_left_slot.substitutes
            )
        )

    def test_case_specific_roster_is_preferred_when_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roster_dir = root / "config" / "case_rosters"
            roster_dir.mkdir(parents=True)
            pinned_path = roster_dir / "case-007.json"
            pinned_path.write_text("{}", encoding="utf-8")

            pinned = publisher_roster_path(root, "CASE-007")
            self.assertEqual(pinned, pinned_path)
            fallback = publisher_roster_path(root, "CASE-999")
            self.assertEqual(
                fallback,
                root / "config" / "multicase_publisher_roster_v1.json",
            )

    def test_phase_classification_is_deterministic(self):
        self.assertEqual(
            classify_sample_phase("2024-04-01T20:00:00Z", "2024-04-01"),
            "INITIAL_REPORT",
        )
        self.assertEqual(
            classify_sample_phase("2024-04-02", "2024-04-01"),
            "INITIAL_REPORT",
        )
        self.assertEqual(
            classify_sample_phase("2024-04-04", "2024-04-01"),
            "EARLY_UPDATE",
        )
        self.assertEqual(
            classify_sample_phase("2024-04-05", "2024-04-01"),
            "LATER_INVESTIGATION",
        )
        self.assertEqual(
            classify_sample_phase(
                "2024-04-03T02:00:00+03:00", "2024-04-01"
            ),
            "EARLY_UPDATE",
        )

    def test_reviewed_leads_require_matching_case_source_and_domain(self):
        payload = {
            "case_id": self.manifest["case_id"],
            "leads": [
                {
                    "source_id": "bbc-arabic",
                    "url": "https://bbc.com/arabic/articles/reviewed",
                    "title": "Reviewed public article",
                    "published_at": "2024-04-03T08:00:00Z",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviewed.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            leads = load_reviewed_leads(path, self.manifest, self.roster)
            self.assertEqual(len(leads), 1)
            self.assertEqual(leads[0].provider_id, "reviewed-public-search")
            self.assertEqual(leads[0].sample_phase, "EARLY_UPDATE")
            payload["leads"][0]["url"] = "https://example.com/wrong"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(BasketBuildError, "domain"):
                load_reviewed_leads(path, self.manifest, self.roster)

    def test_selection_prefers_primary_and_allows_only_two_plus_one(self):
        selected = select_roster_articles(
            self.build_leads(split_slot=True), self.roster
        )
        self.assertEqual(len(selected), 27)
        ap_slot = [
            item
            for item in selected
            if item.slot.slot_id == "western-associated-press"
        ]
        self.assertEqual(
            Counter(item.outlet.source_id for item in ap_slot),
            {
                "associated-press-english": 2,
                "clickorlando-ap-wire-substitute": 1,
            },
        )

    def test_google_news_provider_uses_local_language_and_resolves_urls(self):
        outlet = next(
            slot.primary
            for slot in self.roster.slots
            if slot.slot_id == "western-guardian"
        )
        client = FakeGoogleNewsClient(outlet.domain)
        provider = GoogleNewsBasketLeadProvider(client)
        leads = provider.discover(self.manifest, outlet)

        self.assertEqual(len(leads), 3)
        self.assertEqual({lead.sample_phase for lead in leads}, set((
            "INITIAL_REPORT",
            "EARLY_UPDATE",
            "LATER_INVESTIGATION",
        )))
        self.assertTrue(all(lead.url.startswith("https://theguardian.com/") for lead in leads))
        query = client.search_params["q"]
        self.assertIn("synthetic event", query)
        self.assertNotIn("אירוע סינתטי", query)
        self.assertNotIn("حدث اصطناعي", query)

    def test_fallback_targets_only_slots_with_missing_phases(self):
        leads = self.build_leads()
        leads = [
            lead
            for lead in leads
            if not (
                lead.source_id == "associated-press-english"
                and lead.sample_phase == "EARLY_UPDATE"
            )
        ]
        source_ids = missing_roster_source_ids(leads, self.roster)
        self.assertEqual(
            source_ids,
            {
                "associated-press-english",
                "clickorlando-ap-wire-substitute",
            },
        )

    def test_staging_is_local_bounded_and_builds_valid_27_article_basket(self):
        selected = select_roster_articles(
            self.build_leads(split_slot=True), self.roster
        )
        with tempfile.TemporaryDirectory() as temporary:
            report = stage_selected_articles(
                self.manifest,
                selected,
                temporary,
                client=FakeArticleClient(),
            )
            self.assertEqual(report.failures, ())
            self.assertEqual(len(report.staged), 27)
            self.assertTrue(
                all(item.snapshot_ref.startswith("local-snapshot://") for item in report.staged)
            )
            snapshots = list(Path(temporary).rglob("*.gz"))
            self.assertEqual(len(snapshots), 27)

            basket = build_basket_payload(
                self.manifest,
                report.staged,
                version=1,
                created_at="2026-10-08T10:00:00Z",
                change_reason="סל fixture שנבנה מקומית.",
            )
            self.assertEqual(len(basket["articles"]), 27)
            replacement = next(
                article
                for article in basket["articles"]
                if article["source_id"] == "clickorlando-ap-wire-substitute"
            )
            self.assertEqual(replacement["replacement_kind"], "SIMILAR_OUTLET")
            self.assertTrue(replacement["requires_human_approval"])

    def test_incomplete_discovery_cannot_build_below_hard_floor(self):
        selected = select_roster_articles(self.build_leads()[:24], self.roster)
        self.assertEqual(len(selected), 24)
        with tempfile.TemporaryDirectory() as temporary:
            report = stage_selected_articles(
                self.manifest,
                selected,
                temporary,
                client=FakeArticleClient(),
            )
            with self.assertRaisesRegex(Exception, "too short|25"):
                build_basket_payload(
                    self.manifest,
                    report.staged,
                    version=1,
                    created_at="2026-10-08T10:00:00Z",
                    change_reason="סל חסר.",
                )


if __name__ == "__main__":
    unittest.main()
