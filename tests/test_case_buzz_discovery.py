from __future__ import annotations

from datetime import date, timedelta
import json
import unittest

from aletheia_lite.case_discovery import (
    CoveragePoint,
    DiscoveryQuery,
    GDELTTimelineVolumeProvider,
    detect_coverage_peaks,
)


class _Response:
    def __init__(self, payload: dict):
        self.content = json.dumps(payload).encode("utf-8")


class _Client:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return _Response(self.payload)


class BuzzDiscoveryTests(unittest.TestCase):
    def test_timeline_provider_normalizes_raw_count(self):
        client = _Client(
            {
                "timeline": [
                    {
                        "series": "Article Count",
                        "data": [
                            {"date": "20240401T000000Z", "value": 125, "norm": 10000},
                            {"date": "20240402T000000Z", "value": 0, "norm": 11000},
                        ],
                    }
                ]
            }
        )
        provider = GDELTTimelineVolumeProvider(client)
        query = DiscoveryQuery(
            query_id="q",
            topic_tag="lane",
            window_start=date(2024, 4, 1),
            window_end=date(2024, 4, 2),
            terms={"he": (), "en": ("Israel Gaza",), "ar": ()},
        )
        points = provider.discover(query)
        self.assertEqual(2, len(points))
        self.assertAlmostEqual(0.0125, points[0].normalized_share)
        self.assertEqual("timelinevolraw", client.calls[0][1]["mode"])

    def test_peak_detector_finds_spike_and_separates_nearby_days(self):
        start = date(2024, 1, 1)
        points = []
        for offset in range(60):
            share = 0.01
            count = 100
            if offset == 20:
                share, count = 0.035, 350
            if offset == 22:
                share, count = 0.03, 300
            if offset == 45:
                share, count = 0.028, 280
            points.append(
                CoveragePoint(
                    day=start + timedelta(days=offset),
                    article_count=count,
                    monitored_article_count=10000,
                    normalized_share=share,
                )
            )
        peaks = detect_coverage_peaks(
            points,
            maximum=3,
            minimum_lift=1.25,
            minimum_percentile=0.7,
            minimum_separation_days=14,
        )
        self.assertEqual([start + timedelta(days=20), start + timedelta(days=45)], [
            peak.day for peak in peaks
        ])
        self.assertGreater(peaks[0].lift_over_local_baseline, 3)

    def test_peak_detector_does_not_promote_flat_high_volume(self):
        start = date(2024, 1, 1)
        points = [
            CoveragePoint(
                day=start + timedelta(days=offset),
                article_count=1000,
                monitored_article_count=100000,
                normalized_share=0.01,
            )
            for offset in range(45)
        ]
        self.assertEqual([], detect_coverage_peaks(points))


if __name__ == "__main__":
    unittest.main()
