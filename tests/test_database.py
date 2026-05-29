"""Tests for the SQLite dedup/stale-marking layer in ``job_search_agent.Database``.

Runs under both ``pytest`` and ``python -m unittest`` — only stdlib is used.
Uses an in-memory database so no temp files are left behind.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import Database, Job  # noqa: E402


def _make_job(fingerprint: str, url: str, title: str = "Director of Things") -> Job:
    return Job(
        source="test",
        title=title,
        employer="Test Employer",
        url=url,
        description="A permanent senior leadership role",
        score=42.0,
        fingerprint=fingerprint,
    )


class TestMarkStale(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def _age_all(self, hours: int = 200):
        # Push every row's last_seen_at into the past so mark_stale's cutoff
        # would catch it unless status protects it.
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self.db.conn.execute("UPDATE jobs SET last_seen_at = ?", (old,))
        self.db.conn.commit()

    def _status(self, url: str) -> str:
        return self.db.conn.execute(
            "SELECT status FROM jobs WHERE url = ?", (url,)
        ).fetchone()["status"]

    def _set_status(self, url: str, status: str):
        self.db.conn.execute(
            "UPDATE jobs SET status = ? WHERE url = ?", (status, url)
        )
        self.db.conn.commit()

    def test_unseen_new_job_marked_stale(self):
        self.db.upsert_job(_make_job("fp1", "https://example.test/1"))
        self._age_all()
        marked = self.db.mark_stale(hours=168)
        self.assertEqual(marked, 1)
        self.assertEqual(self._status("https://example.test/1"), "stale")

    def test_curated_statuses_preserved(self):
        # Regression: status doubles as the user's application pipeline, so a
        # disappearing listing must not overwrite 'interested'/'applied'.
        for fp, url, status in [
            ("fp_i", "https://example.test/interested", "interested"),
            ("fp_a", "https://example.test/applied", "applied"),
            ("fp_r", "https://example.test/rejected", "rejected"),
            ("fp_n", "https://example.test/new", "new"),
        ]:
            self.db.upsert_job(_make_job(fp, url))
            self._set_status(url, status)
        self._age_all()

        marked = self.db.mark_stale(hours=168)

        self.assertEqual(marked, 1)  # only the 'new' row flips
        self.assertEqual(self._status("https://example.test/interested"), "interested")
        self.assertEqual(self._status("https://example.test/applied"), "applied")
        self.assertEqual(self._status("https://example.test/rejected"), "rejected")
        self.assertEqual(self._status("https://example.test/new"), "stale")

    def test_recently_seen_job_not_marked_stale(self):
        # upsert_job sets last_seen_at = now, so a fresh row is inside the
        # window and must survive.
        self.db.upsert_job(_make_job("fp_fresh", "https://example.test/fresh"))
        marked = self.db.mark_stale(hours=168)
        self.assertEqual(marked, 0)
        self.assertEqual(self._status("https://example.test/fresh"), "new")


class TestTouchSeenMany(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def _set_status(self, url: str, status: str):
        self.db.conn.execute(
            "UPDATE jobs SET status = ? WHERE url = ?", (status, url)
        )
        self.db.conn.commit()

    def _status(self, url: str) -> str:
        return self.db.conn.execute(
            "SELECT status FROM jobs WHERE url = ?", (url,)
        ).fetchone()["status"]

    def test_touch_refreshes_last_seen_and_prevents_stale(self):
        url = "https://example.test/known"
        self.db.upsert_job(_make_job("fp_known", url))
        # Age it past the stale cutoff...
        old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        self.db.conn.execute("UPDATE jobs SET last_seen_at = ?", (old,))
        self.db.conn.commit()

        # ...then touch it as the scraper would on re-seeing the listing.
        self.db.touch_seen_many([url])

        marked = self.db.mark_stale(hours=168)
        self.assertEqual(marked, 0)
        status = self.db.conn.execute(
            "SELECT status FROM jobs WHERE url = ?", (url,)
        ).fetchone()["status"]
        self.assertEqual(status, "new")

    def test_empty_list_is_noop(self):
        # Guard clause: no rows, no error.
        self.db.touch_seen_many([])  # should not raise

    def test_touches_only_listed_urls(self):
        self.db.upsert_job(_make_job("fp_a", "https://example.test/a"))
        self.db.upsert_job(_make_job("fp_b", "https://example.test/b"))
        old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        self.db.conn.execute("UPDATE jobs SET last_seen_at = ?", (old,))
        self.db.conn.commit()

        self.db.touch_seen_many(["https://example.test/a"])

        # Only /a was refreshed, so only /b should go stale.
        self.db.mark_stale(hours=168)
        rows = {
            r["url"]: r["status"]
            for r in self.db.conn.execute("SELECT url, status FROM jobs").fetchall()
        }
        self.assertEqual(rows["https://example.test/a"], "new")
        self.assertEqual(rows["https://example.test/b"], "stale")

    def test_touch_reactivates_stale_job(self):
        url = "https://example.test/reappeared"
        self.db.upsert_job(_make_job("fp_reappeared", url))
        self._set_status(url, "stale")

        self.db.touch_seen_many([url])

        status = self.db.conn.execute(
            "SELECT status FROM jobs WHERE url = ?", (url,)
        ).fetchone()["status"]
        self.assertEqual(status, "new")

    def test_touch_preserves_curated_status(self):
        url = "https://example.test/applied"
        self.db.upsert_job(_make_job("fp_applied", url))
        self._set_status(url, "applied")

        self.db.touch_seen_many([url])

        status = self.db.conn.execute(
            "SELECT status FROM jobs WHERE url = ?", (url,)
        ).fetchone()["status"]
        self.assertEqual(status, "applied")

    def test_touch_backfills_source_for_legacy_row(self):
        url = "https://example.test/source"
        self.db.upsert_job(_make_job("fp_source", url))
        self.db.conn.execute("UPDATE jobs SET source = NULL WHERE url = ?", (url,))
        self.db.conn.commit()

        self.db.touch_seen_many([url], source="jobs.ac.uk")

        source = self.db.conn.execute(
            "SELECT source FROM jobs WHERE url = ?", (url,)
        ).fetchone()["source"]
        self.assertEqual(source, "jobs.ac.uk")

    def test_mark_stale_only_checks_successful_sources(self):
        self.db.upsert_job(_make_job("fp_a", "https://example.test/a"))
        self.db.upsert_job(_make_job("fp_b", "https://example.test/b"))
        self.db.conn.execute(
            "UPDATE jobs SET source = CASE url "
            "WHEN 'https://example.test/a' THEN 'healthy' "
            "ELSE 'failed' END"
        )
        self.db.conn.commit()
        old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        self.db.conn.execute("UPDATE jobs SET last_seen_at = ?", (old,))
        self.db.conn.commit()

        marked = self.db.mark_stale(hours=168, sources=["healthy"])

        rows = {
            r["url"]: r["status"]
            for r in self.db.conn.execute("SELECT url, status FROM jobs").fetchall()
        }
        self.assertEqual(marked, 1)
        self.assertEqual(rows["https://example.test/a"], "stale")
        self.assertEqual(rows["https://example.test/b"], "new")

    def test_mark_stale_with_no_successful_sources_is_noop(self):
        self.db.upsert_job(_make_job("fp_a", "https://example.test/a"))
        old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        self.db.conn.execute("UPDATE jobs SET last_seen_at = ?", (old,))
        self.db.conn.commit()

        marked = self.db.mark_stale(hours=168, sources=[])

        self.assertEqual(marked, 0)
        self.assertEqual(self._status("https://example.test/a"), "new")


if __name__ == "__main__":
    unittest.main()
