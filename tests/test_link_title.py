"""Unit tests for extract_link_title — the card-heading title fallback.

Covers the "stretched link" card layout (empty overlay anchor with the title
in a sibling heading, e.g. Dixon Walter) while making sure sources where the
anchor text *is* the title are unaffected.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bs4 import BeautifulSoup  # noqa: E402

from job_search_agent import extract_link_title  # noqa: E402


def _first_anchor(html: str):
    return BeautifulSoup(html, "html.parser").find("a")


class TestExtractLinkTitle(unittest.TestCase):
    def test_anchor_text_used_when_present(self):
        # Normal board: the title is the anchor's own text. The fallback must
        # not override it (even though there is a heading nearby).
        a = _first_anchor(
            """
            <article>
              <h4>Some Card Heading</h4>
              <a class="job" href="/job/1">Director of Education and Learning</a>
            </article>
            """
        )
        self.assertEqual(
            extract_link_title(a), "Director of Education and Learning"
        )

    def test_heading_fallback_when_anchor_empty(self):
        # Dixon Walter stretched-link layout: empty overlay anchor, title in a
        # sibling <h4>/<span> inside the card.
        a = _first_anchor(
            """
            <article class="opportunities">
              <a class="showcase__link-stretch"
                 href="https://www.dixonwalter.co.uk/opportunities/role/"></a>
              <div class="showcase__header">
                <h4 data-mh="opportunities-title">
                  <span>Deputy Director of Global Marketing and Channels</span>
                </h4>
              </div>
            </article>
            """
        )
        self.assertEqual(
            extract_link_title(a),
            "Deputy Director of Global Marketing and Channels",
        )

    def test_no_false_title_when_neither_exists(self):
        # Empty anchor and no heading in the card — must not invent a title.
        # The short anchor text is returned so the caller's len() < 10 guard
        # still drops the candidate.
        a = _first_anchor(
            """
            <div class="card">
              <a class="showcase__link-stretch" href="/job/2"></a>
              <p>No heading here, just body copy.</p>
            </div>
            """
        )
        self.assertEqual(extract_link_title(a), "")


if __name__ == "__main__":
    unittest.main()
