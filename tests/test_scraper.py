"""Unit tests for scraper control-flow that does not need real HTTP."""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import Database, HE_CONFIG, _scrape_source  # noqa: E402


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {}


async def _noop_sleep(_delay):
    return None


class TestScrapeSource(unittest.TestCase):
    def test_duplicate_listing_urls_are_fetched_once(self):
        detail_calls = []
        listing_url = "https://example.test/jobs"

        async def fake_fetch(_client, url):
            if url == listing_url:
                return _FakeResponse("""
                    <html><body>
                    <a class="job" href="/job/1">Director of Education</a>
                    <a class="job" href="/job/1">Director of Education and Learning</a>
                    </body></html>
                """)
            detail_calls.append(url)
            return _FakeResponse(
                "<main>Permanent senior leadership role focused on governance "
                "and student experience across higher education.</main>"
            )

        src = {
            "name": "Example Source",
            "url": listing_url,
            "selector": "a.job",
        }
        with Database(":memory:") as db:
            with patch("job_search_agent.fetch_with_retry", side_effect=fake_fetch):
                with patch("job_search_agent.asyncio.sleep", side_effect=_noop_sleep):
                    jobs, stats, seen_urls = asyncio.run(
                        _scrape_source(None, src, HE_CONFIG, db)
                    )

        self.assertEqual(stats.candidates, 1)
        self.assertEqual(len(detail_calls), 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(seen_urls, [])


if __name__ == "__main__":
    unittest.main()
