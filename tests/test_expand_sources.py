"""Unit tests for ``job_search_agent.expand_sources`` and ``_aggregate_stats``."""
import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import SourceStats, _aggregate_stats, expand_sources  # noqa: E402


class TestExpandSources(unittest.TestCase):
    def test_no_pages_key_passes_through(self):
        src = {"name": "A", "url": "https://x/jobs", "selector": "a"}
        self.assertEqual(expand_sources([src]), [src])

    def test_pages_one_passes_through(self):
        src = {"name": "A", "url": "https://x/jobs", "selector": "a", "pages": 1}
        # pages<=1 is a no-op, but the pagination keys are still present —
        # _scrape_source ignores them, so passing the dict through unchanged
        # is correct and avoids surprising key removal.
        self.assertEqual(expand_sources([src]), [src])

    def test_steps_start_index_by_default_page_size(self):
        src = {
            "name": "jobs.ac.uk Director",
            "url": "https://www.jobs.ac.uk/search/?keywords=director&startIndex=1",
            "selector": ".j-search-result__text > a",
            "pages": 3,
        }
        out = expand_sources([src])
        self.assertEqual(len(out), 3)
        starts = [parse_qs(urlparse(s["url"]).query)["startIndex"][0] for s in out]
        self.assertEqual(starts, ["1", "26", "51"])

    def test_all_pages_keep_same_name_and_drop_pagination_keys(self):
        src = {
            "name": "Board",
            "url": "https://x/search/?keywords=k&startIndex=1",
            "selector": "a",
            "pages": 2,
        }
        out = expand_sources([src])
        self.assertTrue(all(s["name"] == "Board" for s in out))
        for s in out:
            self.assertNotIn("pages", s)
            self.assertNotIn("page_param", s)
            self.assertNotIn("page_size", s)

    def test_query_preserved_on_every_page(self):
        # The stateful-search workaround depends on the keyword/facet query
        # being present in EVERY paginated request.
        src = {
            "name": "Board",
            "url": "https://x/search/?keywords=chief+executive&sortOrder=1&startIndex=1",
            "selector": "a",
            "pages": 2,
        }
        out = expand_sources([src])
        for s in out:
            q = parse_qs(urlparse(s["url"]).query)
            self.assertEqual(q["keywords"], ["chief executive"])
            self.assertEqual(q["sortOrder"], ["1"])

    def test_facet_brackets_not_percent_encoded(self):
        src = {
            "name": "Discipline",
            "url": "https://x/search/?academicDisciplineFacet[]=law&startIndex=1",
            "selector": "a",
            "pages": 2,
        }
        out = expand_sources([src])
        for s in out:
            self.assertIn("academicDisciplineFacet[]=law", s["url"])

    def test_custom_page_param_and_size(self):
        src = {
            "name": "Board",
            "url": "https://x/jobs?page=2",
            "selector": "a",
            "pages": 2,
            "page_param": "page",
            "page_size": 1,
        }
        out = expand_sources([src])
        pages = [parse_qs(urlparse(s["url"]).query)["page"][0] for s in out]
        self.assertEqual(pages, ["2", "3"])

    def test_missing_param_defaults_to_one(self):
        # No startIndex in the base URL — pagination should start at 1.
        src = {
            "name": "Board",
            "url": "https://x/search/?keywords=k",
            "selector": "a",
            "pages": 2,
        }
        out = expand_sources([src])
        starts = [parse_qs(urlparse(s["url"]).query)["startIndex"][0] for s in out]
        self.assertEqual(starts, ["1", "26"])

    def test_mixed_sources(self):
        plain = {"name": "Plain", "url": "https://x/a", "selector": "a"}
        paged = {
            "name": "Paged",
            "url": "https://x/b?startIndex=1",
            "selector": "a",
            "pages": 2,
        }
        out = expand_sources([plain, paged])
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], plain)
        self.assertTrue(all(s["name"] == "Paged" for s in out[1:]))


class TestAggregateStats(unittest.TestCase):
    def test_merges_pages_by_name(self):
        stats = [
            SourceStats(name="Board", links_matched=25, candidates=4, new_scored=1),
            SourceStats(name="Board", links_matched=25, candidates=2, new_scored=0),
            SourceStats(name="Other", links_matched=10, candidates=1, new_scored=1),
        ]
        out = _aggregate_stats(stats)
        self.assertEqual([s.name for s in out], ["Board", "Other"])
        board = out[0]
        self.assertEqual(board.links_matched, 50)
        self.assertEqual(board.candidates, 6)
        self.assertEqual(board.new_scored, 1)

    def test_listing_failed_only_when_all_pages_fail(self):
        partial = [
            SourceStats(name="B", listing_failed=True),
            SourceStats(name="B", links_matched=25, listing_failed=False),
        ]
        self.assertFalse(_aggregate_stats(partial)[0].listing_failed)

        total = [
            SourceStats(name="B", listing_failed=True),
            SourceStats(name="B", listing_failed=True),
        ]
        self.assertTrue(_aggregate_stats(total)[0].listing_failed)


if __name__ == "__main__":
    unittest.main()
