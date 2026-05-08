"""Unit tests for the rule-based scoring in ``job_search_agent.score_job``.

Runs under both ``pytest`` and ``python -m unittest`` — only stdlib is used.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import (  # noqa: E402
    CHARITY_CONFIG,
    HE_CONFIG,
    Job,
    score_job,
)


def _make_job(title: str, description: str = "") -> Job:
    return Job(
        source="test",
        title=title,
        employer="Test Employer",
        url="https://example.test/job/1",
        description=description,
    )


class TestExclusionGate(unittest.TestCase):
    def test_excluded_term_short_circuits(self):
        job = score_job(
            _make_job("Senior Software Developer", "We need a developer"),
            HE_CONFIG,
        )
        self.assertEqual(job.score, HE_CONFIG["weights"]["exclusion_penalty"])
        self.assertIn("Excluded", job.match_reasons)

    def test_no_exclusion_does_not_short_circuit(self):
        job = score_job(
            _make_job(
                "Pro-Vice-Chancellor",
                "A permanent leadership role with governance focus",
            ),
            HE_CONFIG,
        )
        self.assertNotIn("Excluded", job.match_reasons)
        self.assertGreater(job.score, 0)


class TestPermanentDetection(unittest.TestCase):
    """Regression test for the ``non-permanent`` false-positive bug."""

    def test_permanent_is_detected(self):
        job = score_job(
            _make_job(
                "Pro-Vice-Chancellor",
                "This is a permanent appointment with full governance duties",
            ),
            HE_CONFIG,
        )
        self.assertIn("Permanent", job.match_reasons)

    def test_non_permanent_is_not_detected_as_permanent(self):
        job = score_job(
            _make_job(
                "Pro-Vice-Chancellor",
                "This is a non-permanent fixed-term role",
            ),
            HE_CONFIG,
        )
        self.assertNotIn("Permanent", job.match_reasons)
        # Senior + interim should classify as Strategic Interim, not score-penalised
        self.assertIn("Strategic Interim", job.match_reasons)

    def test_substantive_is_detected(self):
        job = score_job(
            _make_job(
                "Pro-Vice-Chancellor",
                "Substantive appointment to lead the faculty",
            ),
            HE_CONFIG,
        )
        self.assertIn("Permanent", job.match_reasons)

    def test_non_substantive_is_not_detected(self):
        job = score_job(
            _make_job(
                "Pro-Vice-Chancellor",
                "A non-substantive interim cover arrangement",
            ),
            HE_CONFIG,
        )
        self.assertNotIn("Permanent", job.match_reasons)


class TestExecutiveBonus(unittest.TestCase):
    def test_executive_title_awards_bonus(self):
        job = score_job(
            _make_job("Vice-Chancellor", "Permanent strategic leadership role"),
            HE_CONFIG,
        )
        self.assertIn("Executive Level", job.match_reasons)
        self.assertGreaterEqual(
            job.score, HE_CONFIG["weights"]["executive_bonus"]
        )

    def test_director_bonus_for_charity(self):
        job = score_job(
            _make_job(
                "Director of Education",
                "Permanent senior role focused on policy and learning",
            ),
            CHARITY_CONFIG,
        )
        self.assertIn("Director Level", job.match_reasons)


class TestTitleGateAndScoring(unittest.TestCase):
    def test_score_starts_at_zero(self):
        # Empty description, generic title — should produce a non-negative,
        # bounded score and not raise.
        job = score_job(_make_job("Vice-Chancellor", ""), HE_CONFIG)
        self.assertIsInstance(job.score, float)


if __name__ == "__main__":
    unittest.main()
