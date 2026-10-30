from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date

from jsonschema import Draft202012Validator, FormatChecker

from aletheia_lite.case_discovery import (
    ClusterAnnotation,
    CandidateEligibilityError,
    DiscoveryQuery,
    EventLead,
    GDELTDocLeadProvider,
    GoogleNewsRSSLeadProvider,
    RSSLeadProvider,
    SampleComposition,
    build_case_candidate,
    build_temporal_entity_bundles,
    cluster_event_leads,
    discover_from_providers,
)
from aletheia_lite.case_generator_contracts import load_contract_schema
from aletheia_lite.collector import HTTPResult


class FakeHTTPClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return HTTPResult(url=url, status_code=200, headers={}, content=self.payload)


class FixedProvider:
    def __init__(self, provider_id, leads=None, error=None):
        self.provider_id = provider_id
        self.leads = list(leads or [])
        self.error = error

    def discover(self, query):
        if self.error:
            raise RuntimeError(self.error)
        return self.leads


def lead(*, key, provider, domain, language, title, published_at="2024-04-01T08:00:00Z"):
    return EventLead(
        lead_id=f"lead-{key}",
        provider_id=provider,
        provider_record_id=f"record-{key}",
        source_name=domain,
        source_domain=domain,
        url=f"https://{domain}/{key}",
        canonical_url=f"https://{domain}/{key}",
        title=title,
        summary="",
        published_at=published_at,
        language=language,
        topic_tag="fixture-topic",
    )


class CaseDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.query = DiscoveryQuery(
            query_id="QUERY-FIXTURE-001",
            topic_tag="fixture-topic",
            window_start=date(2024, 4, 1),
            window_end=date(2024, 4, 10),
            terms={
                "he": ("אירוע ישראל",),
                "en": ("Israel event",),
                "ar": ("حدث إسرائيل",),
            },
            max_leads_per_provider=20,
        )
        self.leads = [
            lead(
                key="he",
                provider="rss-he",
                domain="hebrew.example",
                language="he",
                title="אירוע ישראל בדיווח ראשון",
            ),
            lead(
                key="en",
                provider="gdelt-doc-2",
                domain="english.example",
                language="en",
                title="Israel event in an initial report",
                published_at="2024-04-02T09:00:00Z",
            ),
            lead(
                key="ar",
                provider="rss-ar",
                domain="arabic.example",
                language="ar",
                title="حدث إسرائيل في تقرير أولي",
                published_at="2024-04-02T10:00:00Z",
            ),
        ]
        self.aliases = {"israel": ("ישראל", "Israel", "إسرائيل")}
        self.cluster = cluster_event_leads(
            self.leads, entity_aliases=self.aliases
        )[0]
        self.annotation = ClusterAnnotation(
            title="אירוע בדיקה רב-לשוני",
            neutral_summary="שלושה מקורות ציבוריים תיארו אירוע מדומה בתוך חלון זמן משותף.",
            scope_relation="CORE",
            responsibility_class="PROVISIONAL",
            evidence_clarity="PARTIAL",
            war_nexus_rationale="האירוע המדומה מערב את ישראל כשחקן ישיר במלחמה.",
            material_impact_on_israel=True,
            observed_lead_ids=tuple(item.lead_id for item in self.leads),
            proposed_keywords={
                "he": ("אירוע ישראל",),
                "en": ("Israel event",),
                "ar": ("حدث إسرائيل",),
            },
        )
        self.composition = SampleComposition(
            total_cases=1,
            direct_cases=1,
            external_indirect_cases=0,
            prewar_formative_cases=0,
        )

    def test_multilingual_observed_leads_form_traceable_candidate(self):
        self.assertEqual(len(self.cluster.lead_ids), 3)
        candidate = build_case_candidate(
            self.cluster, self.leads, self.annotation, self.composition
        )
        self.assertEqual(candidate["coverage_estimate"], {"he": 1, "en": 1, "ar": 1})
        self.assertNotIn("truth_score", candidate)
        schema = load_contract_schema()
        candidate_schema = {"$ref": "#/$defs/CaseCandidate", "$defs": schema["$defs"]}
        Draft202012Validator(
            candidate_schema, format_checker=FormatChecker()
        ).validate(candidate)

    def test_annotation_cannot_invent_an_unobserved_lead(self):
        annotation = replace(
            self.annotation,
            observed_lead_ids=(*self.annotation.observed_lead_ids, "lead-invented"),
        )
        with self.assertRaisesRegex(CandidateEligibilityError, "observed leads"):
            build_case_candidate(
                self.cluster, self.leads, annotation, self.composition
            )

    def test_external_local_coverage_without_material_impact_is_rejected(self):
        annotation = replace(
            self.annotation,
            scope_relation="EXTERNAL_INDIRECT",
            material_impact_on_israel=False,
        )
        with self.assertRaisesRegex(CandidateEligibilityError, "no documented material impact"):
            build_case_candidate(
                self.cluster, self.leads, annotation, self.composition
            )

    def test_unresolved_requires_two_plausible_hypotheses(self):
        annotation = replace(
            self.annotation,
            responsibility_class="UNRESOLVED",
            evidence_clarity="UNRESOLVED",
            unresolved_hypotheses=("השערה אחת",),
        )
        with self.assertRaisesRegex(CandidateEligibilityError, "at least two"):
            build_case_candidate(
                self.cluster, self.leads, annotation, self.composition
            )

    def test_unresolved_hypotheses_are_preserved_for_the_human_gate(self):
        annotation = replace(
            self.annotation,
            responsibility_class="UNRESOLVED",
            evidence_clarity="UNRESOLVED",
            unresolved_hypotheses=("השערה א", "השערה ב"),
        )
        candidate = build_case_candidate(
            self.cluster, self.leads, annotation, self.composition
        )
        self.assertEqual(
            candidate["unresolved_hypotheses"], ["השערה א", "השערה ב"]
        )

    def test_balance_guard_preserves_six_direct_case_minimum(self):
        annotation = replace(
            self.annotation,
            scope_relation="EXTERNAL_INDIRECT",
            material_impact_on_israel=True,
        )
        nearly_full = SampleComposition(
            total_cases=7,
            direct_cases=5,
            external_indirect_cases=0,
            prewar_formative_cases=1,
        )
        with self.assertRaisesRegex(CandidateEligibilityError, "six-direct-case"):
            build_case_candidate(self.cluster, self.leads, annotation, nearly_full)

    def test_provider_failure_is_audited_without_losing_other_results(self):
        report = discover_from_providers(
            self.query,
            [
                FixedProvider("working", self.leads),
                FixedProvider("broken", error="synthetic outage"),
            ],
        )
        self.assertEqual(len(report.leads), 3)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].provider_id, "broken")

    def test_temporal_entity_bundle_preserves_two_provider_requirement(self):
        bundles = build_temporal_entity_bundles(
            [replace(item, published_at="2024-04-01T08:00:00Z") for item in self.leads],
            entity_aliases={"israel": ("ישראל", "Israel", "إسرائيل")},
            generic_entities=frozenset(),
        )
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(bundles[0].provider_ids), 3)
        self.assertEqual(len(bundles[0].languages), 3)

    def test_rss_adapter_filters_metadata_without_fetching_articles(self):
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Fixture</title>
        <item><title>Israel event update</title>
        <link>https://rss.example/report?utm_source=test</link>
        <guid>rss-1</guid><pubDate>Tue, 02 Apr 2024 08:00:00 GMT</pubDate>
        <description>Observed metadata only</description></item>
        <item><title>Unrelated sports</title><link>https://rss.example/sport</link>
        <pubDate>Tue, 02 Apr 2024 09:00:00 GMT</pubDate></item>
        </channel></rss>"""
        client = FakeHTTPClient(payload)
        provider = RSSLeadProvider(
            provider_id="rss-fixture",
            source_name="RSS Fixture",
            feed_url="https://rss.example/feed.xml",
            language="en",
            client=client,
        )
        results = provider.discover(self.query)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].canonical_url, "https://rss.example/report")
        self.assertEqual(client.calls, [("https://rss.example/feed.xml", None)])

    def test_gdelt_adapter_uses_bounded_articlelist_json_query(self):
        payload = json.dumps({
            "articles": [
                {
                    "url": "https://english.example/gdelt",
                    "title": "Israel event report",
                    "domain": "english.example",
                    "language": "English",
                    "seendate": "20240402T101500Z",
                },
                {
                    "url": "https://unsupported.example/gdelt",
                    "title": "Unsupported language",
                    "domain": "unsupported.example",
                    "language": "French",
                    "seendate": "20240402T101500Z",
                },
            ]
        }).encode("utf-8")
        client = FakeHTTPClient(payload)
        results = GDELTDocLeadProvider(client).discover(self.query)
        self.assertEqual(len(results), 1)
        url, params = client.calls[0]
        self.assertIn("gdeltproject.org", url)
        self.assertEqual(params["mode"], "artlist")
        self.assertEqual(params["format"], "json")
        self.assertLessEqual(int(params["maxrecords"]), 250)
        self.assertEqual(params["startdatetime"], "20240401000000")
        self.assertEqual(params["enddatetime"], "20240410235959")

    def test_google_news_adapter_is_bounded_metadata_only_second_provider(self):
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Google News</title>
        <item><title>Israel event update</title>
        <link>https://news.google.com/rss/articles/fixture</link>
        <guid>google-1</guid><pubDate>Tue, 02 Apr 2024 08:00:00 GMT</pubDate>
        <source url="https://publisher.example">Publisher Example</source>
        </item></channel></rss>"""
        client = FakeHTTPClient(payload)
        query = DiscoveryQuery(
            query_id="QUERY-GNEWS-001",
            topic_tag="fixture-topic",
            window_start=date(2024, 4, 1),
            window_end=date(2024, 4, 10),
            terms={"he": (), "en": ("Israel event",), "ar": ()},
            max_leads_per_provider=6,
        )
        results = GoogleNewsRSSLeadProvider(client).discover(query)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider_id, "google-news-rss")
        self.assertEqual(results[0].source_domain, "publisher.example")
        url, params = client.calls[0]
        self.assertEqual(url, "https://news.google.com/rss/search")
        self.assertIn("after:2024-04-01", params["q"])
        self.assertIn("before:2024-04-11", params["q"])


if __name__ == "__main__":
    unittest.main()
