"""Unit tests for the per-source funnel panel and report scaffolding.

Runs under both ``pytest`` and ``python -m unittest`` — only stdlib is used.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import (  # noqa: E402
    SourceStats,
    _render_source_panel,
    generate_html_report,
)


class TestSourcePanel(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        # No stats → no panel rendered (back-compat with callers that don't
        # pass source_stats).
        self.assertEqual(_render_source_panel([]), "")

    def test_healthy_source_shows_counts(self):
        html = _render_source_panel([
            SourceStats(name="jobs.ac.uk", links_matched=12, candidates=4, new_scored=1),
        ])
        self.assertIn("jobs.ac.uk", html)
        self.assertIn(">12<", html)
        self.assertIn(">4<", html)
        self.assertIn(">1<", html)
        self.assertNotIn("selector dry", html)
        self.assertNotIn("fetch failed", html)

    def test_dry_selector_is_flagged(self):
        # 0 links → "selector dry?" warning highlights candidates for removal.
        html = _render_source_panel([
            SourceStats(name="Saxton Bampfylde", links_matched=0),
        ])
        self.assertIn("selector dry", html)

    def test_listing_fetch_failure_is_flagged(self):
        html = _render_source_panel([
            SourceStats(name="Veredus", listing_failed=True),
        ])
        self.assertIn("fetch failed", html)

    def test_source_name_is_html_escaped(self):
        # Defence in depth — source names come from YAML the user controls,
        # but the panel still escapes them so a maliciously crafted YAML
        # cannot inject script into the rendered email.
        html = _render_source_panel([
            SourceStats(name="<script>alert(1)</script>", links_matched=1),
        ])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestGenerateReportSmoke(unittest.TestCase):
    """End-to-end check that generate_html_report wires panel + window label."""

    def test_writes_panel_and_window_label(self):
        stats = [
            SourceStats(name="jobs.ac.uk", links_matched=10, candidates=3, new_scored=2),
            SourceStats(name="Saxton Bampfylde", links_matched=0),
        ]
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="r+") as f:
            path = f.name
        try:
            generate_html_report(
                rows=[],  # no roles — exercises the "empty content" branch
                title="Daily HE Report",
                filename=path,
                source_stats=stats,
                window_hours=72,
            )
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            # Panel rendered with both sources visible
            self.assertIn("jobs.ac.uk", content)
            self.assertIn("Saxton Bampfylde", content)
            self.assertIn("selector dry", content)
            # Window label rendered (72h presents as "3 day(s)")
            self.assertIn("3 day(s)", content)
            # Empty roles → suitability-threshold message
            self.assertIn("No opportunities met the suitability threshold.", content)
        finally:
            os.unlink(path)

    def test_back_compat_without_optional_args(self):
        # Existing call sites should keep working without source_stats /
        # window_hours — both are optional kwargs.
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="r+") as f:
            path = f.name
        try:
            generate_html_report(rows=[], title="t", filename=path)
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            # No panel, no window label, but the page renders.
            self.assertNotIn("Per-source funnel", content)
            self.assertNotIn("window: last", content)
        finally:
            os.unlink(path)


class TestProfileDailyWindow(unittest.TestCase):
    """HE should use a 72h daily window; charity/sector default to 24h."""

    def test_he_profile_has_72h_window(self):
        from job_search_agent import HE_CONFIG
        self.assertEqual(HE_CONFIG["filters"].get("daily_window_hours"), 72)

    def test_charity_profile_defaults_to_24h(self):
        # Charity profile intentionally omits daily_window_hours; the code
        # path defaults to 24h to preserve the current cadence.
        from job_search_agent import CHARITY_CONFIG
        self.assertNotIn("daily_window_hours", CHARITY_CONFIG["filters"])


if __name__ == "__main__":
    unittest.main()
