"""Unit tests for ``job_search_agent.extract_employer``."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import extract_employer  # noqa: E402


class TestEmployerExtraction(unittest.TestCase):
    def test_literal_sector_body(self):
        self.assertEqual(
            extract_employer("Director of Policy", "Role at Advance HE", "src"),
            "Advance HE",
        )

    def test_university_pattern(self):
        self.assertEqual(
            extract_employer(
                "Pro-Vice-Chancellor, University of Manchester | Apply now",
                "",
                "jobs.ac.uk",
            ),
            "University of Manchester",
        )

    def test_falls_back_to_source_name(self):
        # No literal match, no capture-group match -> fall back to source.
        self.assertEqual(
            extract_employer("Random Role", "Some description", "fallback-source"),
            "fallback-source",
        )


if __name__ == "__main__":
    unittest.main()
