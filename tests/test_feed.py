"""Unit tests for ``job_search_agent.parse_feed_entries``."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import parse_feed_entries  # noqa: E402


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Feed</title>
  <item>
    <title>UNIVERSITY OF SURREY: Pro-Vice-Chancellor and Executive Dean</title>
    <link>https://example.com/unijobs/listing/411930/pvc</link>
  </item>
  <item>
    <title>UNIVERSITY OF OXFORD: Dean, Said Business School</title>
    <link>https://example.com/unijobs/listing/411988/dean</link>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed</title>
  <entry>
    <title>Director of Estates</title>
    <link rel="self" href="https://example.com/self"/>
    <link rel="alternate" href="https://example.com/job/1"/>
  </entry>
</feed>"""


class TestParseFeedEntries(unittest.TestCase):
    def test_rss_items(self):
        entries = parse_feed_entries(RSS)
        self.assertEqual(len(entries), 2)
        title, link, employer = entries[0]
        self.assertIn("Pro-Vice-Chancellor", title)
        self.assertEqual(link, "https://example.com/unijobs/listing/411930/pvc")
        self.assertIsNone(employer)

    def test_atom_prefers_alternate_link(self):
        entries = parse_feed_entries(ATOM)
        self.assertEqual(len(entries), 1)
        title, link, _ = entries[0]
        self.assertEqual(title, "Director of Estates")
        # rel="alternate" is the human-facing page, not rel="self"
        self.assertEqual(link, "https://example.com/job/1")

    def test_malformed_xml_returns_empty(self):
        self.assertEqual(parse_feed_entries("<rss><item><title>oops"), [])
        self.assertEqual(parse_feed_entries("not xml at all <script>"), [])
        self.assertEqual(parse_feed_entries(""), [])

    def test_item_missing_title_or_link_skipped(self):
        xml = """<rss version="2.0"><channel>
          <item><title>No link here</title></item>
          <item><link>https://example.com/x</link></item>
          <item><title>Good</title><link>https://example.com/good</link></item>
        </channel></rss>"""
        entries = parse_feed_entries(xml)
        self.assertEqual(entries, [("Good", "https://example.com/good", None)])

    def test_namespaced_atom_localname(self):
        # ElementTree prepends "{ns}" to tags; the parser must strip it.
        entries = parse_feed_entries(ATOM)
        self.assertTrue(entries and entries[0][0] == "Director of Estates")


if __name__ == "__main__":
    unittest.main()
