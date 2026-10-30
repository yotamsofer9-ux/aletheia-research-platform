from __future__ import annotations

import hashlib
import unittest

from scripts.sync_case_to_gcp import _normalize_aggregate_source_hash


class SyncCaseToGcpTests(unittest.TestCase):
    def test_empty_bigquery_aggregate_uses_empty_set_hash(self) -> None:
        self.assertEqual(
            _normalize_aggregate_source_hash(None),
            hashlib.sha256(b"").hexdigest(),
        )

    def test_bigquery_hex_hash_is_normalized_to_lowercase(self) -> None:
        self.assertEqual(_normalize_aggregate_source_hash("ABC123"), "abc123")


if __name__ == "__main__":
    unittest.main()
