from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aletheia_lite.collector import (
    Candidate,
    CollectionError,
    HTTPResult,
    SourceCollector,
    canonicalize_url,
    extract_article,
    amp_recovery_url,
    title_body_coherent,
)
from aletheia_lite.config import CaseConfig
from aletheia_lite.pipeline import CollectionPipeline
from aletheia_lite.manual_import import import_local_file
from aletheia_lite.storage import ResearchStore, StoredDocument
from aletheia_lite.snapshots import LocalSnapshotStore


def build_config() -> CaseConfig:
    return CaseConfig.from_dict(
        {
            "case_id": "TEST-LITE",
            "title": "Offline test",
            "window_start": "2023-10-17T00:00:00Z",
            "window_end": "2023-10-26T00:00:00Z",
            "keywords": ["hospital", "gaza"],
            "max_documents": 5,
            "max_documents_per_source": 3,
            "network": {"min_text_chars": 50, "max_text_chars": 1000},
            "sources": [
                {
                    "source_id": "source-a",
                    "name": "Source A",
                    "group": "DOMESTIC_ISRAELI",
                    "language": "en",
                    "discovery": "urls",
                    "article_urls": [
                        {
                            "url": "https://example.com/a",
                            "sample_phase": "INITIAL_REPORT"
                        }
                    ],
                },
                {
                    "source_id": "source-b",
                    "name": "Source B",
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "urls",
                    "article_urls": ["https://example.org/b"],
                },
            ],
        }
    )


class FakePipelineCollector:
    def __init__(self, config: CaseConfig):
        self.config = config

    def discover(self, source):
        if source.source_id == "source-a":
            return [Candidate(source_id=source.source_id, url="https://example.com/a")]
        return [
            Candidate(source_id=source.source_id, url="https://example.org/b"),
            Candidate(source_id=source.source_id, url="https://example.org/fail"),
        ]

    def fetch(self, source, candidate):
        if candidate.url.endswith("fail"):
            raise CollectionError("fixture failure", 503)
        # Both source documents intentionally share a body and therefore a hash.
        return StoredDocument(
            case_id=self.config.case_id,
            source_id=source.source_id,
            url=candidate.url,
            canonical_url=canonicalize_url(candidate.url),
            title="Hospital report",
            published_at="2023-10-18T00:00:00+00:00",
            archived_at=None,
            text="Same sufficiently long hospital report body for deduplication.",
            content_hash="same-content-hash",
            fetch_method="FIXTURE",
            http_status=200,
            truncated=False,
        )


class FakeHTTPClient:
    def __init__(self, payload: bytes):
        self.payload = payload

    def get(self, url, params=None):
        return HTTPResult(url=url, status_code=200, headers={}, content=self.payload)


class ConfigAndExtractionTests(unittest.TestCase):
    def test_israel_hayom_amp_recovery_and_coherence_gate(self):
        incoherent = extract_article(
            "<html><head><title>חיסול נסראללה</title></head>"
            "<body><p>מבצע מלגות ופרסום מסחרי כללי</p></body></html>".encode()
        )
        coherent = extract_article(
            "<html><head><title>חיסול נסראללה</title></head>"
            "<body><p>הכתבה עוסקת בחיסול נסראללה בביירות</p></body></html>".encode()
        )
        self.assertFalse(title_body_coherent(incoherent))
        self.assertTrue(title_body_coherent(coherent))
        self.assertEqual(
            "https://www.israelhayom.co.il/news/article/1?amp=1",
            amp_recovery_url("https://www.israelhayom.co.il/news/article/1"),
        )
        self.assertIsNone(amp_recovery_url("https://example.com/article/1"))

    def test_rejects_unbounded_document_count(self):
        raw = {
            "case_id": "x",
            "title": "x",
            "window_start": "2023-01-01T00:00:00Z",
            "window_end": "2023-01-02T00:00:00Z",
            "keywords": ["x"],
            "max_documents": 2001,
            "sources": [
                {
                    "source_id": "s",
                    "name": "S",
                    "group": "DOMESTIC_ISRAELI",
                    "language": "en",
                    "discovery": "urls",
                    "article_urls": ["https://example.com/a"],
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "max_documents"):
            CaseConfig.from_dict(raw)

    def test_rejects_unknown_sample_phase(self):
        raw = {
            "case_id": "x",
            "title": "x",
            "window_start": "2023-01-01T00:00:00Z",
            "window_end": "2023-01-02T00:00:00Z",
            "keywords": ["x"],
            "sources": [
                {
                    "source_id": "s",
                    "name": "S",
                    "group": "DOMESTIC_ISRAELI",
                    "language": "he",
                    "discovery": "urls",
                    "article_urls": [
                        {
                            "url": "https://example.com/a",
                            "sample_phase": "MIDDLE"
                        }
                    ],
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "sample_phase"):
            CaseConfig.from_dict(raw)

    def test_rejects_third_publisher_in_one_slot(self):
        raw = {
            "case_id": "x",
            "title": "x",
            "window_start": "2023-01-01T00:00:00Z",
            "window_end": "2023-01-02T00:00:00Z",
            "keywords": ["x"],
            "sources": [
                {
                    "source_id": source_id,
                    "name": source_id,
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "urls",
                    "publisher_slot": "one-slot",
                    "editorial_profile": "CENTER",
                    "substitute_for": "primary" if source_id != "primary" else None,
                    "article_urls": [f"https://example.com/{source_id}"],
                }
                for source_id in ("primary", "substitute-a", "substitute-b")
            ],
        }
        with self.assertRaisesRegex(ValueError, "more than two publishers"):
            CaseConfig.from_dict(raw)

    def test_rejects_substitute_with_different_editorial_profile(self):
        raw = {
            "case_id": "x",
            "title": "x",
            "window_start": "2023-01-01T00:00:00Z",
            "window_end": "2023-01-02T00:00:00Z",
            "keywords": ["x"],
            "sources": [
                {
                    "source_id": "primary",
                    "name": "Primary",
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "urls",
                    "publisher_slot": "one-slot",
                    "editorial_profile": "CENTER",
                    "article_urls": ["https://example.com/primary"],
                },
                {
                    "source_id": "substitute",
                    "name": "Substitute",
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "urls",
                    "publisher_slot": "one-slot",
                    "editorial_profile": "RIGHT",
                    "substitute_for": "primary",
                    "article_urls": ["https://example.com/substitute"],
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "editorial profile"):
            CaseConfig.from_dict(raw)

    def test_canonical_url_removes_tracking(self):
        value = canonicalize_url(
            "HTTPS://Example.COM/story/?utm_source=x&b=2&a=1#section"
        )
        self.assertEqual(value, "https://example.com/story?a=1&b=2")

    def test_extracts_article_only(self):
        html = b"""
        <html><head><meta property="og:title" content="A title">
        <meta property="article:published_time" content="2023-10-18T01:00:00Z"></head>
        <body><nav>Ignore navigation</nav><article>
        <p>First paragraph about the hospital.</p>
        <p>Second paragraph with evidence and context.</p>
        </article><footer>Ignore footer</footer></body></html>
        """
        article = extract_article(html)
        self.assertEqual(article.title, "A title")
        self.assertIn("First paragraph", article.text)
        self.assertNotIn("Ignore", article.text)

    def test_extracts_hebrew_and_arabic_without_corruption(self):
        html = """
        <html><head><title>בדיקת שפות — اختبار اللغات</title></head>
        <body><article>
        <p>דיווח בעברית על בית החולים והאירוע.</p>
        <p>تقرير باللغة العربية عن المستشفى والحادث.</p>
        </article></body></html>
        """.encode("utf-8")
        article = extract_article(html)
        self.assertIn("בית החולים", article.text)
        self.assertIn("المستشفى", article.text)
        self.assertEqual(article.title, "בדיקת שפות — اختبار اللغات")

    def test_extracts_json_ld_article_body_when_page_has_no_paragraphs(self):
        html = """
        <html><head><script type="application/ld+json">{
          "@type": "NewsArticle",
          "headline": "Structured headline",
          "datePublished": "2024-04-03",
          "articleBody": "World Central Kitchen evidence in a structured article body."
        }</script></head><body><div id="app"></div></body></html>
        """.encode("utf-8")
        article = extract_article(html)
        self.assertEqual(article.title, "Structured headline")
        self.assertEqual(article.published_at, "2024-04-03")
        self.assertIn("structured article body", article.text)

    def test_rss_discovery_filters_date_and_keyword(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Fixture feed</title>
        <item><title>Gaza hospital report</title><link>https://example.com/a</link>
        <pubDate>Wed, 18 Oct 2023 10:00:00 GMT</pubDate><description>blast</description></item>
        <item><title>Unrelated</title><link>https://example.com/b</link>
        <pubDate>Wed, 18 Oct 2023 10:00:00 GMT</pubDate></item>
        </channel></rss>"""
        raw = {
            "case_id": "RSS",
            "title": "RSS",
            "window_start": "2023-10-17T00:00:00Z",
            "window_end": "2023-10-19T00:00:00Z",
            "keywords": ["hospital"],
            "sources": [
                {
                    "source_id": "rss",
                    "name": "RSS",
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "rss",
                    "feed_url": "https://example.com/feed.xml",
                }
            ],
        }
        config = CaseConfig.from_dict(raw)
        collector = SourceCollector(config, FakeHTTPClient(feed))
        candidates = collector.discover(config.sources[0])
        self.assertEqual([item.url for item in candidates], ["https://example.com/a"])

    def test_wayback_guess_uses_day_after_publication(self):
        config = build_config()
        collector = SourceCollector(config, FakeHTTPClient(b""))
        guessed = collector._guessed_wayback_snapshot(
            Candidate(
                source_id="source-a",
                url="https://example.com/story",
                published_at="2023-10-18T10:00:00Z",
            )
        )
        self.assertIn("/20231019100000id_/", guessed.snapshot_url)

    def test_wayback_resolver_preserves_research_metadata(self):
        config = build_config()
        response = (
            b'[["timestamp","original","statuscode","mimetype","digest"],'
            b'["20231020112233","https://example.com/a","200",'
            b'"text/html","abc"]]'
        )
        collector = SourceCollector(config, FakeHTTPClient(response))
        candidate = Candidate(
            source_id="source-a",
            url="https://example.com/a",
            title="A title",
            published_at="2023-10-18T01:00:00Z",
            sample_phase="LATER_INVESTIGATION",
        )

        resolved = collector._resolve_wayback_snapshot(candidate)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source_id, "source-a")
        self.assertEqual(resolved.title, "A title")
        self.assertEqual(resolved.published_at, "2023-10-18T01:00:00Z")
        self.assertEqual(resolved.sample_phase, "LATER_INVESTIGATION")
        self.assertIn("/20231020112233id_/", resolved.snapshot_url)

    def test_fetch_rejects_irrelevant_extraction(self):
        config = build_config()
        irrelevant = (
            b"<html><article><p>Completely unrelated recommendation text "
            b"that is long enough to pass the minimum character limit.</p>"
            b"</article></html>"
        )
        collector = SourceCollector(config, FakeHTTPClient(irrelevant))

        with self.assertRaisesRegex(CollectionError, "case keyword"):
            collector.fetch(
                config.sources[0],
                Candidate(source_id="source-a", url="https://example.com/a"),
            )

    def test_fetch_matches_case_keyword_in_title_case_insensitively(self):
        config = build_config()
        relevant_title = (
            b"<html><head><title>HOSPITAL investigation</title></head>"
            b"<body><article><p>A sufficiently long report body with detailed "
            b"evidence, chronology, attribution, and contextual information.</p>"
            b"</article></body></html>"
        )
        collector = SourceCollector(config, FakeHTTPClient(relevant_title))

        document = collector.fetch(
            config.sources[0],
            Candidate(source_id="source-a", url="https://example.com/a"),
        )

        self.assertEqual(document.title, "HOSPITAL investigation")

    def test_fetch_matches_two_informative_tokens_from_search_phrase(self):
        raw = {
            "case_id": "TEST-PHRASE",
            "title": "Phrase relevance",
            "window_start": "2024-04-13T00:00:00Z",
            "window_end": "2024-04-20T23:59:59Z",
            "keywords": ["Iran attack Israel", "April 2024"],
            "network": {"min_text_chars": 50, "max_text_chars": 1000},
            "sources": [
                {
                    "source_id": "source-a",
                    "name": "Source A",
                    "group": "WESTERN_INTERNATIONAL",
                    "language": "en",
                    "discovery": "urls",
                    "article_urls": ["https://example.com/a"],
                }
            ],
        }
        config = CaseConfig.from_dict(raw)
        html = (
            b"<html><head><title>Israel weighs its next move</title></head>"
            b"<body><article><p>Officials debated a response after Iran sent "
            b"missiles and drones. The report contains enough contextual text."
            b"</p></article></body></html>"
        )
        document = SourceCollector(config, FakeHTTPClient(html)).fetch(
            config.sources[0],
            Candidate(source_id="source-a", url="https://example.com/a"),
        )
        self.assertEqual(document.title, "Israel weighs its next move")

    def test_fetch_rejects_access_interstitial_even_with_relevant_candidate_title(self):
        config = build_config()
        interstitial = (
            b"<html><head><title>Radware Block Page</title></head>"
            b"<body><main><p>Your activity made us think you are a bot. "
            b"Please solve this CAPTCHA to verify you are human and continue "
            b"to the requested publisher page. This explanatory access-check "
            b"message is deliberately long enough to pass the text limit.</p>"
            b"</main></body></html>"
        )
        collector = SourceCollector(config, FakeHTTPClient(interstitial))

        with self.assertRaisesRegex(CollectionError, "access-check page"):
            collector.fetch(
                config.sources[0],
                Candidate(
                    source_id="source-a",
                    url="https://example.com/a",
                    title="Hospital investigation",
                ),
            )


class PipelineTests(unittest.TestCase):
    def test_pipeline_is_incremental_and_deduplicates_content(self):
        config = build_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ResearchStore(Path(temp_dir) / "research.db")
            pipeline = CollectionPipeline(config, store, FakePipelineCollector(config))
            first = pipeline.run()
            self.assertEqual(first.stored, 1)
            self.assertEqual(first.duplicates, 1)
            self.assertEqual(first.failed, 1)
            self.assertEqual(
                store.counts(config.case_id),
                {"READY": 1, "DUPLICATE": 1, "FAILED": 1},
            )

            second = pipeline.run()
            self.assertEqual(second.stored, 0)
            self.assertEqual(second.skipped, 3)
            self.assertEqual(second.failed, 0)
            self.assertEqual(store.ready_count(config.case_id), 1)

            retry = CollectionPipeline(
                config,
                store,
                FakePipelineCollector(config),
                retry_failed=True,
            ).run()
            self.assertEqual(retry.skipped, 2)
            self.assertEqual(retry.failed, 1)

    def test_manual_text_import_respects_limits_and_provenance(self):
        config = build_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "article.txt"
            input_path.write_text(
                "A human-reviewed hospital article with enough evidence and context. " * 3,
                encoding="utf-8",
            )
            store = ResearchStore(temp_path / "research.db")
            run_id, status = import_local_file(
                config=config,
                store=store,
                source=config.sources[0],
                url="https://example.com/manual-story",
                path=input_path,
                title="Manual story",
                published_at="2023-10-18T00:00:00Z",
                file_format="text",
            )
            self.assertTrue(run_id.startswith("run_"))
            self.assertEqual(status, "READY")
            self.assertEqual(store.ready_count(config.case_id), 1)
            snapshots = list((temp_path / "snapshots").rglob("*.gz"))
            self.assertEqual(len(snapshots), 1)

    def test_snapshot_store_is_content_addressed_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshots = LocalSnapshotStore(temp_dir)
            first = snapshots.put(
                case_id="CASE/001",
                source_id="source a",
                content=b"<html>source material</html>",
                media_type="text/html",
            )
            second = snapshots.put(
                case_id="CASE/001",
                source_id="source a",
                content=b"<html>source material</html>",
                media_type="text/html",
            )
            self.assertEqual(first.ref, second.ref)
            self.assertEqual(
                snapshots.read(first.ref),
                b"<html>source material</html>",
            )


if __name__ == "__main__":
    unittest.main()
