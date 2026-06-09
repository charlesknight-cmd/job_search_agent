"""Unit tests for schema.org ``JobPosting`` JSON-LD parsing."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from job_search_agent import parse_jobposting_jsonld  # noqa: E402


# A representative jobs.ac.uk-style detail page: the real employer lives in the
# JSON-LD ``hiringOrganization.name``, while the visible title would otherwise
# defeat the regex heuristic and fall back to the source name.
JOBS_AC_UK_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Head of Student Recruitment and Admissions",
  "hiringOrganization": {"@type": "Organization", "name": "University of Leeds"},
  "baseSalary": {
    "@type": "MonetaryAmount",
    "currency": "GBP",
    "value": {"@type": "QuantitativeValue", "minValue": 65000, "maxValue": 75000, "unitText": "YEAR"}
  },
  "jobLocation": {
    "@type": "Place",
    "address": {"@type": "PostalAddress", "addressLocality": "Leeds", "addressRegion": "West Yorkshire", "addressCountry": "GB"}
  },
  "validThrough": "2026-07-31T23:59:59Z",
  "employmentType": "FULL_TIME"
}
</script>
</head><body><h1>Head of Student Recruitment and Admissions</h1></body></html>
"""

# A page using the ``@graph`` wrapper with the JobPosting alongside other nodes.
GRAPH_PAGE = """
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Some Board"},
  {"@type": "JobPosting", "title": "Dean of Science",
   "hiringOrganization": {"@type": "Organization", "name": "Imperial College London"},
   "baseSalary": {"@type": "MonetaryAmount", "value": {"value": "90,000", "unitText": "YEAR"}}}
]}
</script>
"""

# An hourly-rate posting — the figure must NOT be treated as a salary.
HOURLY_PAGE = """
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Casual Lecturer",
 "hiringOrganization": {"name": "Open University"},
 "baseSalary": {"@type": "MonetaryAmount", "value": {"value": 45, "unitText": "HOUR"}}}
</script>
"""


class TestJsonLdParsing(unittest.TestCase):
    def test_extracts_real_employer(self):
        out = parse_jobposting_jsonld(JOBS_AC_UK_PAGE)
        self.assertEqual(out["employer"], "University of Leeds")

    def test_extracts_top_of_salary_band(self):
        out = parse_jobposting_jsonld(JOBS_AC_UK_PAGE)
        self.assertEqual(out["salary"], 75000)

    def test_extracts_location(self):
        out = parse_jobposting_jsonld(JOBS_AC_UK_PAGE)
        self.assertEqual(out["location"], "Leeds, West Yorkshire, GB")

    def test_extracts_employment_type_and_valid_through(self):
        out = parse_jobposting_jsonld(JOBS_AC_UK_PAGE)
        self.assertEqual(out["employment_type"], "FULL_TIME")
        self.assertEqual(out["valid_through"], "2026-07-31T23:59:59Z")

    def test_graph_wrapper_and_string_salary(self):
        out = parse_jobposting_jsonld(GRAPH_PAGE)
        self.assertEqual(out["employer"], "Imperial College London")
        self.assertEqual(out["salary"], 90000)

    def test_hourly_rate_is_not_a_salary(self):
        out = parse_jobposting_jsonld(HOURLY_PAGE)
        self.assertEqual(out["employer"], "Open University")
        self.assertNotIn("salary", out)

    def test_no_jsonld_returns_empty(self):
        self.assertEqual(parse_jobposting_jsonld("<html><body>No data</body></html>"), {})

    def test_malformed_jsonld_is_ignored(self):
        page = '<script type="application/ld+json">{not valid json,,}</script>'
        self.assertEqual(parse_jobposting_jsonld(page), {})

    def test_non_jobposting_jsonld_returns_empty(self):
        page = '<script type="application/ld+json">{"@type": "Organization", "name": "X"}</script>'
        self.assertEqual(parse_jobposting_jsonld(page), {})


if __name__ == "__main__":
    unittest.main()
