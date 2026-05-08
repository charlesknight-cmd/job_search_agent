"""Unit tests for ``job_search_agent._parse_retry_after``."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import RETRY_AFTER_CAP, _parse_retry_after  # noqa: E402


class TestParseRetryAfter(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(_parse_retry_after(None))

    def test_blank(self):
        self.assertIsNone(_parse_retry_after(""))
        self.assertIsNone(_parse_retry_after("   "))

    def test_non_numeric(self):
        # HTTP-date form is intentionally unsupported
        self.assertIsNone(_parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))
        self.assertIsNone(_parse_retry_after("not-a-number"))

    def test_negative_rejected(self):
        self.assertIsNone(_parse_retry_after("-5"))

    def test_normal_value(self):
        self.assertEqual(_parse_retry_after("12"), 12.0)

    def test_capped_at_max(self):
        self.assertEqual(_parse_retry_after("9999"), RETRY_AFTER_CAP)


if __name__ == "__main__":
    unittest.main()
