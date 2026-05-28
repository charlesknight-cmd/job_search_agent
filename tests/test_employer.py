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

    def test_regex_does_not_spill_to_sentence(self):
        # Regression: "University of X is seeking..." previously captured the entire sentence.
        self.assertEqual(
            extract_employer("University of Manchester is seeking a new PVC", "", "jobs.ac.uk"),
            "University of Manchester",
        )

    def test_regex_preceding_capitalized_words(self):
        # Regression: preceding capitalized words (e.g. "At") were captured because of lazy matching.
        self.assertEqual(
            extract_employer("At our campus Manchester Metropolitan University is hosting an event", "", "jobs.ac.uk"),
            "Manchester Metropolitan University",
        )

    def test_complex_university_name(self):
        # Complex multi-preposition university name
        self.assertEqual(
            extract_employer("A role at the University of the West of England in Bristol", "", "jobs.ac.uk"),
            "University of the West of England",
        )

    def test_trailing_geography_name(self):
        # Trailing location word after the suffix keyword
        self.assertEqual(
            extract_employer("Imperial College London has a vacancy", "", "jobs.ac.uk"),
            "Imperial College London",
        )


if __name__ == "__main__":
    unittest.main()
