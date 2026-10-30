from __future__ import annotations

import unittest

from aletheia_lite.case_candidate_annotation import (
    CandidateAnnotationError,
    build_annotation_prompt,
    validate_annotation_response,
)
from aletheia_lite.case_discovery import (
    EventCluster,
    EventLead,
    SampleComposition,
)


class CandidateAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.leads = [
            EventLead(
                lead_id=f"lead-{index}",
                provider_id=provider,
                provider_record_id=f"record-{index}",
                source_name=domain,
                source_domain=domain,
                url=f"https://{domain}/story",
                canonical_url=f"https://{domain}/story",
                title=title,
                summary="metadata summary",
                published_at="2024-04-01T08:00:00Z",
                language=language,
                topic_tag="fixture",
            )
            for index, (provider, domain, language, title) in enumerate([
                ("rss-he", "he.example", "he", "כותרת אירוע"),
                ("gdelt-doc-2", "en.example", "en", "Event headline"),
            ])
        ]
        self.cluster = EventCluster(
            cluster_id="CLUSTER-FIXTURE",
            topic_tag="fixture",
            lead_ids=tuple(lead.lead_id for lead in self.leads),
            provider_ids=("gdelt-doc-2", "rss-he"),
            source_domains=("en.example", "he.example"),
            languages=("en", "he"),
            window_start="2024-04-01",
            window_end="2024-04-01",
        )
        self.composition = SampleComposition(1, 1, 0, 0)
        self.response = {
            "title": "כותרת ניטרלית",
            "neutral_summary": "סיכום המבוסס על שתי רשומות הגילוי.",
            "scope_relation": "CORE",
            "responsibility_class": "PROVISIONAL",
            "evidence_clarity": "PARTIAL",
            "war_nexus_rationale": "האירוע מתואר כחלק ישיר מזירת המלחמה.",
            "material_impact_on_israel": True,
            "observed_lead_ids": ["lead-0", "lead-1"],
            "proposed_keywords": {
                "he": ["מילת חיפוש"],
                "en": ["search term"],
                "ar": ["كلمة بحث"],
            },
            "unresolved_hypotheses": [],
            "is_coherent_event": True,
            "is_canonical_turning_point": True,
            "turning_point_rationale": "האירוע משנה את השלב הצבאי המתואר.",
            "transformative_dimensions": ["MILITARY_PHASE"],
            "exclusion_reason": None,
        }

    def test_prompt_contains_only_bounded_observed_records_and_no_scores(self):
        prompt = build_annotation_prompt(self.cluster, self.leads, self.composition)
        self.assertEqual(len(prompt["observed_leads"]), 2)
        serialized = str(prompt).lower()
        self.assertNotIn("truth_score", serialized)
        self.assertNotIn("article_body", serialized)

    def test_valid_response_becomes_bounded_annotation(self):
        annotation = validate_annotation_response(
            self.response, self.cluster, self.leads
        )
        self.assertEqual(annotation.scope_relation, "CORE")
        self.assertEqual(annotation.proposed_keywords["ar"], ("كلمة بحث",))

    def test_hallucinated_lead_is_rejected(self):
        response = dict(self.response)
        response["observed_lead_ids"] = ["lead-0", "lead-invented"]
        with self.assertRaisesRegex(CandidateAnnotationError, "supplied observed"):
            validate_annotation_response(response, self.cluster, self.leads)

    def test_media_score_field_is_rejected(self):
        response = dict(self.response)
        response["truth_score"] = 4
        with self.assertRaisesRegex(CandidateAnnotationError, "unexpected"):
            validate_annotation_response(response, self.cluster, self.leads)

    def test_incoherent_temporal_bundle_is_rejected_without_candidate(self):
        response = dict(self.response)
        response["is_coherent_event"] = False
        response["exclusion_reason"] = "הרשומות מתארות שני אירועים שונים."
        with self.assertRaisesRegex(CandidateAnnotationError, "incoherent"):
            validate_annotation_response(response, self.cluster, self.leads)

    def test_coherent_event_must_retain_two_discovery_providers(self):
        third = EventLead(
            lead_id="lead-2",
            provider_id="rss-he",
            provider_record_id="record-2",
            source_name="second.example",
            source_domain="second.example",
            url="https://second.example/story",
            canonical_url="https://second.example/story",
            title="כותרת נוספת",
            summary="",
            published_at="2024-04-01T09:00:00Z",
            language="he",
            topic_tag="fixture",
        )
        cluster = EventCluster(
            cluster_id="CLUSTER-ONE-PROVIDER",
            topic_tag="fixture",
            lead_ids=("lead-0", "lead-2"),
            provider_ids=("rss-he",),
            source_domains=("he.example", "second.example"),
            languages=("he",),
            window_start="2024-04-01",
            window_end="2024-04-01",
        )
        response = dict(self.response)
        response["observed_lead_ids"] = ["lead-0", "lead-2"]
        with self.assertRaisesRegex(CandidateAnnotationError, "two providers"):
            validate_annotation_response(
                response, cluster, [self.leads[0], third]
            )


if __name__ == "__main__":
    unittest.main()
