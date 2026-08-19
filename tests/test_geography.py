"""Unit tests for the geography gate and the cross-board report dedup.

Both guard defects found in the August 2026 audit: overseas roles topping a
UK-focused report, and one vacancy appearing two to four times because each
job board carries its own URL for it.

Runs under both ``pytest`` and ``python -m unittest`` — only stdlib is used.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bs4 import BeautifulSoup  # noqa: E402

from job_search_agent import (  # noqa: E402
    HE_CONFIG,
    dedupe_report_rows,
    extract_card_location,
    is_overseas_posting,
)


def _posting(country):
    """Minimal JSON-LD posting carrying just an addressCountry."""
    return {"jobLocation": {"address": {"addressCountry": country}}}


class TestIsOverseasPosting(unittest.TestCase):
    def test_uk_country_names_are_not_overseas(self):
        for country in [
            "United Kingdom", "GREAT BRITAIN", "England", "Scotland",
            "Wales", "Northern Ireland",
        ]:
            with self.subTest(country=country):
                self.assertFalse(is_overseas_posting(_posting(country)))

    def test_uk_country_codes_are_not_overseas(self):
        for code in ["UK", "GB", "gbr", "U.K."]:
            with self.subTest(code=code):
                self.assertFalse(is_overseas_posting(_posting(code)))

    def test_foreign_countries_are_overseas(self):
        for country in ["New Zealand", "Bahrain", "China", "Hong Kong", "Australia"]:
            with self.subTest(country=country):
                self.assertTrue(is_overseas_posting(_posting(country)))

    def test_ukraine_is_not_mistaken_for_the_uk(self):
        # Regression: a substring test for "uk" treats Ukraine as British.
        self.assertTrue(is_overseas_posting(_posting("Ukraine")))

    def test_gabon_is_not_mistaken_for_great_britain(self):
        # Regression: a substring test for "gb" would match "Gabon".
        self.assertTrue(is_overseas_posting(_posting("Gabon")))

    def test_new_south_wales_is_not_mistaken_for_wales(self):
        # Regression: "Wales" is a substring of "New South Wales".
        self.assertTrue(is_overseas_posting(_posting("New South Wales")))

    def test_missing_country_is_treated_as_uk(self):
        # One-sided by design: most sources publish no JSON-LD at all and are
        # UK-only firms, so only an explicit foreign country drops a role.
        self.assertFalse(is_overseas_posting({}))
        self.assertFalse(is_overseas_posting({"jobLocation": {}}))
        self.assertFalse(is_overseas_posting({"jobLocation": {"address": {}}}))

    def test_list_of_locations_uses_first_stated_country(self):
        posting = {"jobLocation": [{"address": {"addressCountry": "Bahrain"}}]}
        self.assertTrue(is_overseas_posting(posting))


class TestExtractCardLocation(unittest.TestCase):
    # jobs.ac.uk renders the location as an unclassed div reading "Location: X"
    # inside the result card, so it has to be matched on its label text.
    CARD = """
    <div class="j-search-result__result">
      <div class="j-search-result__text"><a href="/job/X1/dean">Dean</a></div>
      <div class="j-search-result__employer">University of Auckland, New Zealand</div>
      <div>Location:
            Auckland
      </div>
      <div class="j-search-result__info">Salary: Not Specified</div>
    </div>
    """

    def _link(self, markup):
        return BeautifulSoup(markup, "html.parser").select_one("a")

    def test_reads_location_from_card(self):
        self.assertEqual(extract_card_location(self._link(self.CARD)), "Auckland")

    def test_returns_none_without_a_card(self):
        link = self._link('<div><a href="/job/X1/dean">Dean</a></div>')
        self.assertIsNone(extract_card_location(link))

    def test_returns_none_when_card_has_no_location(self):
        markup = """
        <div class="j-search-result__result">
          <div class="j-search-result__text"><a href="/job/X1/dean">Dean</a></div>
        </div>
        """
        self.assertIsNone(extract_card_location(self._link(markup)))


class TestProfileLocationExclude(unittest.TestCase):
    def test_he_profile_catches_observed_overseas_advertisers(self):
        # These four all advertised senior roles on jobs.ac.uk during the audit.
        terms = HE_CONFIG["location_exclude"]
        for context in [
            "University of Auckland, New Zealand Auckland",
            "British University of Bahrain Saar",
            "The Hang Seng University of Hong Kong",
            "Xi'an Jiaotong - Liverpool University China",
        ]:
            with self.subTest(context=context):
                self.assertTrue(any(t in context.lower() for t in terms))

    def test_uk_employers_are_not_excluded(self):
        terms = HE_CONFIG["location_exclude"]
        for context in [
            "University of Sussex Brighton",
            "Queen's University Belfast Northern Ireland",
            "University of Wales Cardiff",
            "Robert Gordon University Aberdeen, Scotland",
        ]:
            with self.subTest(context=context):
                self.assertFalse(any(t in context.lower() for t in terms))


class _Row(dict):
    """Stand-in for a ``sqlite3.Row`` — indexable and exposes ``keys()``."""

    def __getitem__(self, key):
        return dict.get(self, key)


def _row(title, employer, score):
    return _Row(title=title, employer=employer, score=score)


class TestDedupeReportRows(unittest.TestCase):
    def test_collapses_one_vacancy_listed_on_three_boards(self):
        # The exact shapes the three boards produce for a single Sussex role.
        rows = [
            _row("UNIVERSITY OF SUSSEX: Deputy Vice-Chancellor and Provost",
                 "University of Sussex", 95),
            _row("Deputy Vice-Chancellor and Provost", "University of Sussex", 70),
            _row("Higher Education Deputy Vice-Chancellor and Provost, "
                 "University of Sussex", "University of Sussex", 70),
        ]
        kept = dedupe_report_rows(rows)
        self.assertEqual(len(kept), 1)
        # Highest-scoring row wins, so the richest description survives.
        self.assertEqual(kept[0]["score"], 95)

    def test_keeps_different_roles_at_the_same_employer(self):
        rows = [
            _row("Director of Finance", "Middlesex University", 50),
            _row("Director of Estates", "Middlesex University", 50),
            _row("Academic Registrar", "Middlesex University", 70),
        ]
        self.assertEqual(len(dedupe_report_rows(rows)), 3)

    def test_keeps_same_title_at_different_employers(self):
        rows = [
            _row("Executive Dean", "London South Bank University", 50),
            _row("Executive Dean", "King's College London", 70),
        ]
        self.assertEqual(len(dedupe_report_rows(rows)), 2)

    def test_short_title_does_not_swallow_longer_ones(self):
        # "Dean" is a token subset of every dean title at an employer, so the
        # MIN_MERGE_TOKENS guard has to keep it from absorbing distinct roles.
        rows = [
            _row("Executive Dean of Science", "University of Leeds", 60),
            _row("Dean", "University of Leeds", 40),
        ]
        self.assertEqual(len(dedupe_report_rows(rows)), 2)

    def test_preserves_score_ordering(self):
        rows = [
            _row("Academic Registrar", "A University", 70),
            _row("Director of Finance", "B University", 50),
            _row("Provost", "C University", 90),
        ]
        scores = [r["score"] for r in dedupe_report_rows(rows)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_input(self):
        self.assertEqual(dedupe_report_rows([]), [])


if __name__ == "__main__":
    unittest.main()
