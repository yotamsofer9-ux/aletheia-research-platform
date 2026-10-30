from __future__ import annotations

from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aletheia_lite.case_candidate_annotation import AnnotationResult
from aletheia_lite.case_discovery import ClusterAnnotation, DiscoveryQuery, EventLead
from aletheia_lite.case_registry import CaseRegistry
from scripts.run_case_discovery import (
    discover_candidates,
    discover_provider_with_cache,
)


class FixedProvider:
    def __init__(self, provider_id, language):
        self.provider_id = provider_id
        self.language = language

    def discover(self, query):
        self.calls = getattr(self, "calls", 0) + 1
        languages = ("en",) if self.language == "en" else ("he", "ar")
        leads = []
        for language in languages:
            key = f"{self.provider_id}-{language}-{query.query_id}"
            title = {
                "he": "ישראל חמאס אירוע בדיקה",
                "en": "Israel Hamas test event",
                "ar": "إسرائيل حماس حدث اختبار",
            }[language]
            leads.append(
                EventLead(
                    lead_id=f"lead-{key}",
                    provider_id=self.provider_id,
                    provider_record_id=key,
                    source_name=f"source-{language}",
                    source_domain=f"{self.provider_id}-{language}.example",
                    url=f"https://{self.provider_id}-{language}.example/{key}",
                    canonical_url=f"https://{self.provider_id}-{language}.example/{key}",
                    title=title,
                    summary="",
                    published_at="2024-01-10T10:00:00Z",
                    language=language,
                    topic_tag=query.topic_tag,
                )
            )
        return leads


class FixedAnnotator:
    def __init__(self, **kwargs):
        del kwargs

    def annotate(self, cluster, leads, composition):
        del leads, composition
        return AnnotationResult(
            annotation=ClusterAnnotation(
                title="אירוע בדיקה מתועד",
                neutral_summary="אשכול בדיקה משלושה פריטי מטא־דאטה ציבוריים.",
                scope_relation="CORE",
                responsibility_class="PROVISIONAL",
                evidence_clarity="PARTIAL",
                war_nexus_rationale="האשכול מתאר אירוע מלחמתי ישיר.",
                material_impact_on_israel=True,
                observed_lead_ids=cluster.lead_ids,
                proposed_keywords={
                    "he": ("חמאס אירוע",),
                    "en": ("Hamas event",),
                    "ar": ("حماس حدث",),
                },
            ),
            model_name="fixture-model",
            protocol_version="case-candidate-annotation-v1.0",
            usage={"prompt_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        )


class CaseDiscoveryRunnerTests(unittest.TestCase):
    def test_fixed_frame_registers_traceable_candidate_and_writes_json_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = {
                "frame_version": "fixture-frame-v1",
                "methodology_version": "case-generator-contract-v1.0",
                "max_leads_per_provider_per_window": 20,
                "max_clusters_per_window_for_annotation": 1,
                "max_total_model_annotations": 1,
                "max_leads_per_annotation": 10,
                "terms": {
                    "he": ["חמאס"],
                    "en": ["Hamas"],
                    "ar": ["حماس"],
                },
                "entity_aliases": {
                    "israel": ["ישראל", "Israel", "إسرائيل"],
                    "hamas": ["חמאס", "Hamas", "حماس"]
                },
                "windows": [
                    {
                        "query_id": "FIXTURE-WINDOW",
                        "topic_tag": "fixture-period",
                        "window_start": "2024-01-01",
                        "window_end": "2024-03-31",
                    }
                ],
            }
            frame_path = root / "frame.json"
            frame_path.write_text(json.dumps(frame, ensure_ascii=False), encoding="utf-8")
            registry_path = root / "registry.sqlite3"
            with (
                patch(
                    "scripts.run_case_discovery.GDELTDocLeadProvider",
                    return_value=FixedProvider("gdelt-doc-2", "en"),
                ),
                patch(
                    "scripts.run_case_discovery.GoogleNewsRSSLeadProvider",
                    return_value=FixedProvider("google-news-rss", "multi"),
                ),
                patch(
                    "scripts.run_case_discovery.VertexClusterAnnotator",
                    FixedAnnotator,
                ),
            ):
                report = discover_candidates(
                    frame_path=frame_path,
                    registry_path=registry_path,
                    annotate=True,
                    project="fixture",
                    location="fixture",
                    model="fixture",
                    report_dir=root / "reports",
                    cache_root=root / "cache",
                )
            self.assertEqual(len(report["registered_candidates"]), 1)
            self.assertEqual(report["total_model_tokens"], 20)
            self.assertEqual(report["cloud_writes"], 0)
            candidate = CaseRegistry(registry_path).list_candidates()[0]
            self.assertEqual(len(candidate["evidence_leads"]), 3)
            self.assertEqual(candidate["registry_state"], "PROPOSED")
            self.assertTrue(Path(report["report_path"]).exists())

    def test_provider_cache_replays_success_without_a_second_network_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FixedProvider("provider-one", "en")
            query = DiscoveryQuery(
                query_id="CACHE-WINDOW",
                topic_tag="fixture-period",
                window_start=date(2024, 1, 1),
                window_end=date(2024, 3, 31),
                terms={"he": ("חמאס",), "en": ("Hamas",), "ar": ("حماس",)},
                max_leads_per_provider=20,
            )
            cache_root = Path(temporary) / "cache"
            first, first_failure, first_status = discover_provider_with_cache(
                provider,
                query,
                cache_root=cache_root,
                frame_version="fixture-frame-v1",
            )
            second, second_failure, second_status = discover_provider_with_cache(
                provider,
                query,
                cache_root=cache_root,
                frame_version="fixture-frame-v1",
            )
            self.assertEqual(provider.calls, 1)
            self.assertIsNone(first_failure)
            self.assertIsNone(second_failure)
            self.assertEqual(first, second)
            self.assertEqual(first_status["mode"], "NETWORK_FETCH")
            self.assertEqual(second_status["mode"], "CACHE_HIT")


if __name__ == "__main__":
    unittest.main()
