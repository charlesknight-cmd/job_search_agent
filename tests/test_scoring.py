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
    extract_salary,
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

    def test_edtech_director_not_excluded_by_software_mention(self):
        # Regression: the bare "software" exclusion previously killed any
        # legitimate EdTech leadership role whose description mentioned
        # software. Director-of-Digital-Learning roles should now score.
        job = score_job(
            _make_job(
                "Director of Digital Learning",
                "Permanent senior role leading our learning platform "
                "and educational software strategy across the institution",
            ),
            HE_CONFIG,
        )
        self.assertNotIn("Excluded", job.match_reasons)
        self.assertGreater(job.score, HE_CONFIG["filters"]["minimum_score"])

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

    def test_director_bonus_for_he(self):
        # HE profile previously had no director_bonus, so adjacent senior
        # titles like "Director of Innovation" only scored on permanent/
        # expertise signals and could fall under the 25-point threshold.
        # Note: titles like "Director of Student Experience" already match
        # the exec_titles substring "director of student" and earn the
        # bigger Executive Level bonus — director_titles catches the gap
        # for *adjacent* roles (innovation, knowledge exchange, etc.).
        job = score_job(
            _make_job(
                "Director of Innovation",
                "Permanent senior leadership role driving knowledge "
                "exchange and enterprise activity across the institution",
            ),
            HE_CONFIG,
        )
        self.assertIn("Director Level", job.match_reasons)
        self.assertNotIn("Executive Level", job.match_reasons)
        self.assertGreaterEqual(
            job.score, HE_CONFIG["weights"]["director_bonus"]
        )


class TestExpertiseSignals(unittest.TestCase):
    """CV-derived expertise terms should trigger the expertise bonus."""

    def _scored(self, description: str, config=HE_CONFIG):
        return score_job(
            _make_job("Director of Education", description), config
        )

    def test_micro_credentials_signal(self):
        job = self._scored(
            "Designing micro-credentials and stackable qualifications "
            "for the international market"
        )
        self.assertIn("Micro-credentials", job.match_reasons)

    def test_student_outcomes_signal(self):
        job = self._scored(
            "Improving student outcomes and graduate outcomes through "
            "evidence-based learning analytics"
        )
        # First match wins for the reported reason; just assert a CV-relevant
        # expertise tag landed.
        cv_tags = {
            "Student Outcomes",
            "Graduate Outcomes",
            "Learning Analytics",
        }
        self.assertTrue(cv_tags & set(job.match_reasons))

    def test_tne_signal_does_not_false_positive_on_witness(self):
        # The literal substring "tne" appears inside "witness" / "fitness".
        # Word-boundary spacing in the expertise_map should avoid this.
        job = self._scored(
            "Fitness for purpose review with a witness statement on file"
        )
        self.assertNotIn("TNE", job.match_reasons)

    def test_tne_signal_when_actually_present(self):
        job = self._scored(
            "Leading our transnational education (TNE) partnerships "
            "across MENA and APAC"
        )
        self.assertIn("TNE", job.match_reasons)

    def test_ofs_signal_does_not_false_positive_on_ofsted(self):
        # "ofsted" contains the literal substring "ofs" — without spacing
        # guards, every FE/schools-adjacent role mentioning Ofsted would
        # falsely trip the OfS expertise signal.
        job = self._scored(
            "Working alongside Ofsted on inspection-readiness improvements"
        )
        self.assertNotIn("OfS", job.match_reasons)

    def test_ofs_signal_when_actually_present(self):
        job = self._scored(
            "Engagement with the Office for Students and OfS B3 metrics"
        )
        match_reasons = set(job.match_reasons)
        self.assertTrue({"OfS", "B3 Metrics"} & match_reasons)


class TestTitleGateAndScoring(unittest.TestCase):
    def test_score_starts_at_zero(self):
        # Empty description, generic title — should produce a non-negative,
        # bounded score and not raise.
        job = score_job(_make_job("Vice-Chancellor", ""), HE_CONFIG)
        self.assertIsInstance(job.score, float)


class TestSalaryExtraction(unittest.TestCase):
    """``extract_salary`` returns the highest plausible GBP figure (in pounds)."""

    def test_k_suffix(self):
        self.assertEqual(extract_salary("Salary: £75k"), 75000)

    def test_k_suffix_uppercase_with_space(self):
        self.assertEqual(extract_salary("Salary: £ 75K"), 75000)

    def test_comma_form(self):
        self.assertEqual(extract_salary("Salary: £75,000 plus benefits"), 75000)

    def test_circa_form(self):
        self.assertEqual(extract_salary("circa £65,000"), 65000)

    def test_band_picks_highest(self):
        # "£70k-£80k" — both endpoints match; we want the top of the band so a
        # senior role isn't mis-scored on its floor.
        self.assertEqual(extract_salary("Salary range £70k-£80k"), 80000)

    def test_band_with_comma_form(self):
        self.assertEqual(
            extract_salary("Salary range £60,000-£75,000"), 75000
        )

    def test_voucher_amount_rejected(self):
        # "£25 voucher" should not be treated as a salary signal.
        self.assertIsNone(extract_salary("Receive a £25 gift voucher"))

    def test_no_salary_returns_none(self):
        self.assertIsNone(extract_salary("Permanent senior leadership role"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(extract_salary(""))

    def test_kg_does_not_match(self):
        # "75kg" should not trip the £75k branch via the unit suffix.
        self.assertIsNone(extract_salary("Capacity for £25kg of equipment"))

    def test_bare_digit_form(self):
        # Some listings use "£75000" without comma or k suffix.
        self.assertEqual(extract_salary("Salary: £75000 per annum"), 75000)


class TestSalaryScoring(unittest.TestCase):
    """Salary signal contributes a bonus or penalty around the configured thresholds."""

    def _score_with_desc(self, description: str, config=HE_CONFIG) -> Job:
        return score_job(
            _make_job("Director of Innovation", description), config
        )

    def test_salary_at_or_above_target_awards_bonus(self):
        job = self._score_with_desc(
            "Permanent senior leadership role. Salary: £75,000 per annum."
        )
        self.assertIn("Salary Match", job.match_reasons)
        self.assertNotIn("Below Target", job.match_reasons)

    def test_salary_below_floor_penalised(self):
        job = self._score_with_desc(
            "Permanent senior leadership role. Salary: £45,000 per annum."
        )
        self.assertIn("Below Target", job.match_reasons)
        self.assertNotIn("Salary Match", job.match_reasons)

    def test_salary_between_floor_and_target_neutral(self):
        # £62k sits between floor (60k) and target (65k) — neither bonus
        # nor penalty.
        job = self._score_with_desc(
            "Permanent senior leadership role. Salary: £62,000 per annum."
        )
        self.assertNotIn("Salary Match", job.match_reasons)
        self.assertNotIn("Below Target", job.match_reasons)

    def test_no_salary_in_description_neutral(self):
        # No stated salary — most exec-search listings — should not add or
        # subtract anything.
        job = self._score_with_desc(
            "Permanent senior leadership role driving knowledge exchange."
        )
        self.assertNotIn("Salary Match", job.match_reasons)
        self.assertNotIn("Below Target", job.match_reasons)

    def test_salary_at_target_boundary_awards_bonus(self):
        # Boundary: a salary exactly at the target (£65,000) earns the bonus.
        job = self._score_with_desc(
            "Permanent senior leadership role. Salary: £65,000."
        )
        self.assertIn("Salary Match", job.match_reasons)

    def test_salary_at_floor_boundary_neutral(self):
        # Boundary: a salary exactly at the floor (£60,000) is *not* below it,
        # so no penalty — but it's also not at target, so no bonus.
        job = self._score_with_desc(
            "Permanent senior leadership role. Salary: £60,000."
        )
        self.assertNotIn("Salary Match", job.match_reasons)
        self.assertNotIn("Below Target", job.match_reasons)


if __name__ == "__main__":
    unittest.main()
