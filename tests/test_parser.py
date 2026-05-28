"""Unit tests for BeautifulSoup HTML parsing and description extraction in ``job_search_agent.py``."""
import unittest
from job_search_agent import extract_description


class TestDescriptionParser(unittest.TestCase):
    def test_extract_description_strips_boilerplate(self):
        html_content = """
        <html>
            <head><title>Job Title</title></head>
            <body>
                <nav><a href="/home">Home</a></nav>
                <header><h1>Welcome to Our Institution</h1></header>
                <main>
                    <div class="job-description">
                        <p>This is the actual job description text that we want to keep.</p>
                        <p>It has details about the senior leadership role.</p>
                    </div>
                </main>
                <aside>Related jobs</aside>
                <footer>&copy; 2026 University</footer>
            </body>
        </html>
        """
        desc = extract_description(html_content)
        self.assertIn("This is the actual job description text", desc)
        self.assertNotIn("Home", desc)
        self.assertNotIn("Welcome to Our Institution", desc)
        self.assertNotIn("Related jobs", desc)

    def test_extract_description_fallback_to_body(self):
        html_content = """
        <html>
            <body>
                <p>Simple description body text without containers.</p>
            </body>
        </html>
        """
        desc = extract_description(html_content)
        self.assertEqual(desc.strip(), "Simple description body text without containers.")


if __name__ == "__main__":
    unittest.main()
