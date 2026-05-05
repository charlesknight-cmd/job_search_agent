import re
import sqlite3
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; JobSearchAgent/1.0; +https://example.com)"
TIMEOUT = 20
DB_PATH = "jobs.db"

# Minimum prescore (title + employer only) before fetching a detail page.
# Avoids hammering sites with HTTP requests for obviously irrelevant listings.
PRESCORE_THRESHOLD = 8


@dataclass
class Source:
    name: str
    kind: str  # generic_html | greenhouse | ashby | lever
    url: str
    enabled: bool = True


@dataclass
class Job:
    source: str
    source_kind: str
    title: str
    employer: str
    location: str
    url: str
    posted_text: str = ""
    description: str = ""
    employment_type: str = ""
    salary_text: str = ""
    remote_status: str = ""
    closing_date: str = ""
    score: float = 0.0
    matched_terms: str = ""
    fetched_at: str = ""
    fingerprint: str = ""


CONFIG = {
    "sources": [
        {"name": "jobs.ac.uk Senior Management", "kind": "generic_html", "url": "https://www.jobs.ac.uk/search/senior-management"},
        {"name": "jobs.ac.uk University Jobs", "kind": "generic_html", "url": "https://www.jobs.ac.uk/categories/university-jobs/1"},
        {"name": "THE UniJobs UK", "kind": "generic_html", "url": "https://www.timeshighereducation.com/unijobs/listings/united-kingdom/"},
        {"name": "Perrett Laver Search", "kind": "generic_html", "url": "https://plusportal.perrettlaver.com/Search"},
        {"name": "Minerva Current Opportunities", "kind": "generic_html", "url": "https://www.minervasearch.com/current-opportunities/"},
        {"name": "University of Oxford Careers", "kind": "generic_html", "url": "https://jobs.ox.ac.uk/"},
        {"name": "University of Cambridge Jobs", "kind": "generic_html", "url": "https://www.jobs.cam.ac.uk/"},
        {"name": "UCL Vacancies", "kind": "generic_html", "url": "https://www.ucl.ac.uk/work-at-ucl/search-ucl-jobs"},
        {"name": "University of Manchester Jobs", "kind": "generic_html", "url": "https://www.jobs.manchester.ac.uk/"},
        {"name": "University of Nottingham Jobs", "kind": "generic_html", "url": "https://jobs.nottingham.ac.uk/"},
        {"name": "University of Edinburgh Jobs", "kind": "generic_html", "url": "https://elxw.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"},
        {"name": "King's College London Jobs", "kind": "generic_html", "url": "https://jobs.kcl.ac.uk/"},
        {"name": "Jisc Careers", "kind": "generic_html", "url": "https://www.jisc.ac.uk/careers"},
        {"name": "Advance HE Careers", "kind": "generic_html", "url": "https://www.advance-he.ac.uk/about-us/work-us"},
        {"name": "Times Higher Education Careers", "kind": "lever", "url": "https://jobs.lever.co/timeshighereducation"},
        {"name": "Instructure Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/instructure"},
        {"name": "Coursera Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/coursera"},
        {"name": "Multiverse Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/multiverse"},
        {"name": "Faculty Careers", "kind": "ashby", "url": "https://jobs.ashbyhq.com/faculty"},
        # HE-specialist executive search & sector jobs
        {"name": "GatenbySanderson", "kind": "generic_html", "url": "https://www.gatenbysanderson.com/our-roles/"},
        {"name": "Anderson Quigley", "kind": "generic_html", "url": "https://andersonquigley.com/jobs/"},
        {"name": "Saxton Bampfylde", "kind": "generic_html", "url": "https://www.saxbam.com/appointments/"},
        {"name": "WonkHE Jobs", "kind": "generic_html", "url": "https://wonkhe.com/jobs/"},
    ],
    "filters": {
        "include_keywords": [
            "education", "higher education", "student success", "student experience",
            "learning and teaching", "teaching and learning", "educational excellence",
            "academic quality", "academic development", "digital education", "edtech",
            "leadership development", "organisational development", "portfolio", "quality assurance",
            "dean", "pro vice-chancellor", "vice provost", "provost", "director", "associate director", "head of",
            "teaching excellence", "student outcomes", "curriculum", "transformation", "governance"
        ],
        "exclude_keywords": [
            "software engineer", "sales development", "account executive", "nurse", "warehouse"
        ],
        "preferred_locations": ["united kingdom", "remote", "hybrid", "london", "scotland", "england", "wales"],
        "minimum_score": 25,
    },
    "weights": {
        "senior_title": 20,
        "he_signal": 18,
        "education_signal": 15,
        "strategy_signal": 12,
        "digital_signal": 8,
        "uk_signal": 8,
        "remote_hybrid_signal": 6,
        "salary_signal": 5,        # fallback when salary present but unparseable
        "salary_60k_plus": 10,
        "salary_70k_plus": 15,
        "salary_80k_plus": 20,
        "salary_90k_plus": 25,
        "salary_below_60k": -10,   # deprioritise known low-salary roles
        "exclude_penalty": -30,
    },
    "db_path": "jobs_he.db",
    "output_prefix": "",
    "label": "Higher Education",
}


CHARITY_CONFIG = {
    "sources": [
        {"name": "CharityJob", "kind": "generic_html",
         "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive"},
        {"name": "Third Sector Jobs", "kind": "generic_html",
         "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/"},
        {"name": "NFP People", "kind": "generic_html",
         "url": "https://jobs.nfppeople.co.uk/"},
        {"name": "NCVO Jobs", "kind": "generic_html",
         "url": "https://jobs.ncvo.org.uk/"},
        {"name": "ACEVO Jobs", "kind": "generic_html",
         "url": "https://www.acevo.org.uk/resources/jobs/"},
        {"name": "Execucare", "kind": "generic_html",
         "url": "https://www.execucare.com/candidates/current-vacancies/"},
        {"name": "Prospectus Recruitment", "kind": "generic_html",
         "url": "https://www.prospectus.co.uk/jobs/"},
        {"name": "TPP Recruitment", "kind": "generic_html",
         "url": "https://www.tpp.co.uk/jobs/"},
        {"name": "Charisma Charity Recruitment", "kind": "generic_html",
         "url": "https://www.charismarecruitment.co.uk/charity-jobs/"},
        {"name": "Guardian Jobs (Charities)", "kind": "generic_html",
         "url": "https://jobs.theguardian.com/jobs/charities/chief-executive/"},
        {"name": "Odgers Berndtson NFP", "kind": "generic_html",
         "url": "https://www.odgersberndtson.com/en-gb/search?practice=not-for-profit"},
        {"name": "GatenbySanderson (Charity)", "kind": "generic_html",
         "url": "https://www.gatenbysanderson.com/our-roles/?sector=charity-and-social-enterprise"},
        {"name": "Perrett Laver (NFP)", "kind": "generic_html",
         "url": "https://plusportal.perrettlaver.com/Search"},
    ],
    "filters": {
        "include_keywords": [
            "chief executive", "ceo", "executive director", "director general",
            "charity", "charities", "charitable", "voluntary sector", "third sector",
            "not-for-profit", "nfp", "ngo", "social enterprise", "civil society",
            "fundraising", "beneficiaries", "mission-driven", "impact",
            "trustee", "board", "governance", "strategic leadership",
            "community", "health", "housing", "arts", "education",
        ],
        "exclude_keywords": [
            "software engineer", "nurse", "warehouse", "sales development",
            "account executive", "account manager",
        ],
        "preferred_locations": ["united kingdom", "remote", "hybrid", "london",
                                "scotland", "england", "wales"],
        "minimum_score": 20,   # slightly lower — charity roles less keyword-dense
    },
    "weights": {
        "senior_title": 25,     # CEO/chief exec is a stronger signal here
        "sector_signal": 20,    # charity/voluntary sector match
        "governance_signal": 12,
        "strategy_signal": 10,
        "digital_signal": 5,
        "uk_signal": 8,
        "remote_hybrid_signal": 6,
        "salary_signal": 5,
        "salary_60k_plus": 10,
        "salary_70k_plus": 15,
        "salary_80k_plus": 20,
        "salary_90k_plus": 25,
        "salary_below_60k": -5,   # smaller penalty — many charity roles omit salary
        "exclude_penalty": -30,
    },
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "label": "Charity CEO",
}


PROFILES = {
    "he": CONFIG,
    "charity": CHARITY_CONFIG,
}


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


class Database:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._migrate()

    def _init_db(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                fingerprint TEXT PRIMARY KEY,
                source TEXT,
                source_kind TEXT,
                title TEXT,
                employer TEXT,
                location TEXT,
                url TEXT,
                posted_text TEXT,
                description TEXT,
                employment_type TEXT,
                salary_text TEXT,
                remote_status TEXT,
                score REAL,
                matched_terms TEXT,
                fetched_at TEXT,
                first_seen_at TEXT,
                closing_date TEXT,
                canonical_id TEXT,
                all_sources TEXT
            )
            """
        )
        self.conn.commit()

    def _migrate(self):
        """Add new columns to existing databases without losing data."""
        for col, typedef in [
            ("first_seen_at", "TEXT"),
            ("closing_date", "TEXT"),
            ("canonical_id", "TEXT"),
            ("all_sources", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Back-fill canonical_id = fingerprint for any pre-migration rows
        self.conn.execute(
            "UPDATE jobs SET canonical_id = fingerprint WHERE canonical_id IS NULL"
        )
        self.conn.commit()

    def find_canonical(self, title: str, employer: str) -> Optional[str]:
        """
        Return the fingerprint of an existing job that looks like the same role,
        or None if no match found.  Matching rule: same normalised employer AND
        Jaccard word-overlap on title ≥ 0.70.
        """
        norm_employer = _normalize_text(employer)
        norm_title = set(_normalize_text(title).split())
        if not norm_title:
            return None

        # Pull candidate canonical rows with the same employer
        cur = self.conn.execute(
            "SELECT fingerprint, title FROM jobs WHERE canonical_id = fingerprint AND lower(employer) LIKE ?",
            (f"%{norm_employer[:30]}%",),
        )
        for row in cur.fetchall():
            candidate_words = set(_normalize_text(row["title"]).split())
            if not candidate_words:
                continue
            overlap = len(norm_title & candidate_words) / len(norm_title | candidate_words)
            if overlap >= 0.70:
                return row["fingerprint"]
        return None

    def merge_into_canonical(self, canonical_fp: str, job: Job) -> None:
        """
        Fold a duplicate job into the canonical record:
        - Update all_sources JSON to include the new source+url
        - Keep the canonical row's fingerprint; ignore the duplicate's fingerprint
        """
        import json
        cur = self.conn.execute(
            "SELECT all_sources FROM jobs WHERE fingerprint = ?", (canonical_fp,)
        )
        row = cur.fetchone()
        if row is None:
            return
        try:
            sources = json.loads(row["all_sources"] or "[]")
        except (json.JSONDecodeError, TypeError):
            sources = []

        # Add new source only if URL not already listed
        existing_urls = {s.get("url") for s in sources}
        if job.url not in existing_urls:
            sources.append({"source": job.source, "url": job.url})
            self.conn.execute(
                "UPDATE jobs SET all_sources = ? WHERE fingerprint = ?",
                (json.dumps(sources), canonical_fp),
            )
            self.conn.commit()

    def upsert_job(self, job: Job, first_seen_at: str, canonical_id: str = "") -> None:
        import json
        if not canonical_id:
            canonical_id = job.fingerprint
        # Seed all_sources with the job's own source
        all_sources = json.dumps([{"source": job.source, "url": job.url}])

        self.conn.execute(
            """
            INSERT INTO jobs (
                fingerprint, source, source_kind, title, employer, location, url,
                posted_text, description, employment_type, salary_text, remote_status,
                closing_date, score, matched_terms, fetched_at, first_seen_at,
                canonical_id, all_sources
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                source=excluded.source,
                source_kind=excluded.source_kind,
                title=excluded.title,
                employer=excluded.employer,
                location=excluded.location,
                url=excluded.url,
                posted_text=excluded.posted_text,
                description=excluded.description,
                employment_type=excluded.employment_type,
                salary_text=excluded.salary_text,
                remote_status=excluded.remote_status,
                closing_date=excluded.closing_date,
                score=excluded.score,
                matched_terms=excluded.matched_terms,
                fetched_at=excluded.fetched_at,
                canonical_id=excluded.canonical_id
            """,
            (
                job.fingerprint, job.source, job.source_kind, job.title, job.employer,
                job.location, job.url, job.posted_text, job.description,
                job.employment_type, job.salary_text, job.remote_status,
                job.closing_date, job.score, job.matched_terms, job.fetched_at,
                first_seen_at, canonical_id, all_sources,
            ),
        )
        self.conn.commit()

    def top_jobs(self, limit: int = 50, minimum_score: float = 0) -> List[sqlite3.Row]:
        today = datetime.now(timezone.utc).date().isoformat()
        # Only return canonical rows (canonical_id = fingerprint) to avoid duplicates
        cur = self.conn.execute(
            """SELECT * FROM jobs
               WHERE score >= ?
               AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY score DESC, fetched_at DESC LIMIT ?""",
            (minimum_score, today, limit),
        )
        return cur.fetchall()

    def new_jobs(self, hours: int = 25, minimum_score: float = 0) -> List[sqlite3.Row]:
        """Return jobs first seen within the last `hours` hours, above minimum_score,
        excluding expired roles. Only canonical rows returned (no duplicates)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            """SELECT * FROM jobs
               WHERE first_seen_at >= ? AND score >= ?
               AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY
                 CASE WHEN closing_date IS NOT NULL AND closing_date != ''
                      THEN closing_date ELSE '9999-99-99' END ASC,
                 score DESC""",
            (cutoff, minimum_score, today),
        )
        return cur.fetchall()

    def weekly_jobs(self, days: int = 7, limit: int = 10, minimum_score: float = 0) -> list:
        """Top-scoring canonical jobs first seen within the past `days` days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            """SELECT * FROM jobs
               WHERE first_seen_at >= ? AND score >= ?
               AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY score DESC
               LIMIT ?""",
            (cutoff, minimum_score, today, limit),
        )
        return cur.fetchall()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class BaseScraper:
    def __init__(self, src: Source, http: requests.Session):
        self.src = src
        self.http = http

    def fetch(self) -> List[Job]:
        raise NotImplementedError

    def absolute_url(self, maybe_relative: str) -> str:
        return urljoin(self.src.url, maybe_relative)

    def text(self, value: Optional[str]) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    def make_fingerprint(self, title: str, employer: str, url: str) -> str:
        raw = f"{title.lower()}|{employer.lower()}|{url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class GreenhouseScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        jobs: List[Job] = []

        employer = self.text(soup.title.text.split("|")[0]) if soup.title else self.src.name
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            title = self.text(a.get_text(" "))
            if not href or not title:
                continue
            if "/jobs/" not in href and "?gh_jid=" not in href and "/job_app" not in href:
                continue
            parent_text = self.text(a.parent.get_text(" ")) if a.parent else ""
            jobs.append(Job(
                source=self.src.name,
                source_kind=self.src.kind,
                title=title,
                employer=employer,
                location=parent_text,
                url=self.absolute_url(href),
                fetched_at=self.now_iso(),
                fingerprint=self.make_fingerprint(title, employer, self.absolute_url(href)),
            ))
        return dedupe_jobs(jobs)


class AshbyScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        employer = self.src.name
        jobs: List[Job] = []

        for a in soup.select("a[href]"):
            href = a.get("href", "")
            title = self.text(a.get_text(" "))
            if not href or not title:
                continue
            if "/job/" not in href and "/jobs/" not in href:
                continue
            block = self.text(a.parent.get_text(" ")) if a.parent else ""
            jobs.append(Job(
                source=self.src.name,
                source_kind=self.src.kind,
                title=title,
                employer=employer,
                location=block,
                url=self.absolute_url(href),
                fetched_at=self.now_iso(),
                fingerprint=self.make_fingerprint(title, employer, self.absolute_url(href)),
            ))
        return dedupe_jobs(jobs)


class LeverScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        base = self.src.url.rstrip("/")
        api_url = base.replace("jobs.lever.co", "api.lever.co/v0/postings") + "?mode=json"
        r = self.http.get(api_url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        employer = self.src.name
        jobs: List[Job] = []
        for item in data:
            title = self.text(item.get("text"))
            categories = item.get("categories", {}) or {}
            location = self.text(categories.get("location", ""))
            commitment = self.text(categories.get("commitment", ""))
            desc = BeautifulSoup(item.get("descriptionPlain", "") or "", "html.parser").get_text(" ")
            url = item.get("hostedUrl") or item.get("applyUrl") or base
            jobs.append(Job(
                source=self.src.name,
                source_kind=self.src.kind,
                title=title,
                employer=employer,
                location=location,
                url=url,
                employment_type=commitment,
                description=self.text(desc),
                fetched_at=self.now_iso(),
                fingerprint=self.make_fingerprint(title, employer, url),
            ))
        return dedupe_jobs(jobs)


class GenericHtmlScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        employer = self.src.name
        jobs: List[Job] = []

        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = self.text(a.get_text(" "))
            if not href or not text:
                continue
            hay = f"{text} {href}".lower()
            if not any(k in hay for k in ["job", "vacan", "career", "role", "position"]):
                continue
            url = self.absolute_url(href)
            jobs.append(Job(
                source=self.src.name,
                source_kind=self.src.kind,
                title=text,
                employer=employer,
                location="",
                url=url,
                fetched_at=self.now_iso(),
                fingerprint=self.make_fingerprint(text, employer, url),
            ))
        return dedupe_jobs(jobs)


SCRAPER_MAP = {
    "greenhouse": GreenhouseScraper,
    "ashby": AshbyScraper,
    "lever": LeverScraper,
    "generic_html": GenericHtmlScraper,
}


def dedupe_jobs(jobs: List[Job]) -> List[Job]:
    seen = set()
    out = []
    for job in jobs:
        if job.fingerprint in seen:
            continue
        seen.add(job.fingerprint)
        out.append(job)
    return out


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def prescore_job(job: Job, config: Dict[str, Any]) -> float:
    """
    Lightweight score using title and employer only — no detail page fetch needed.
    Used to decide whether a job is worth the extra HTTP request.
    """
    weights = config["weights"]
    exclude_keywords = [k.lower() for k in config["filters"]["exclude_keywords"]]

    title = (job.title or "").lower()
    employer = (job.employer or "").lower()
    text = f"{title} {employer}"

    score = 0.0

    senior_terms = ["director", "dean", "pro vice-chancellor", "vice provost", "head of", "chief", "associate director"]
    if any(t in title for t in senior_terms):
        score += weights["senior_title"]

    he_terms = ["higher education", "university", "college", "academic", "faculty", "student experience", "student success"]
    if any(t in text for t in he_terms):
        score += weights["he_signal"]

    education_terms = ["education", "learning", "teaching", "curriculum"]
    if any(t in text for t in education_terms):
        score += weights["education_signal"]

    strategy_terms = ["strategy", "strategic", "transformation", "leadership"]
    if any(t in text for t in strategy_terms):
        score += weights["strategy_signal"]

    digital_terms = ["digital", "edtech", "online learning"]
    if any(t in text for t in digital_terms):
        score += weights["digital_signal"]

    if any(t in text for t in exclude_keywords):
        score += weights["exclude_penalty"]

    return score


def extract_closing_date(text: str) -> str:
    """
    Attempt to extract a closing/deadline date from job description text.
    Returns an ISO date string (YYYY-MM-DD) or empty string if not found.
    """
    MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    trigger = (
        r"(?:closing date|close date|application deadline|applications close|"
        r"deadline for applications|apply by|closes|deadline)\s*[:\-]?\s*"
    )

    patterns = [
        # "15 May 2026" or "15th May 2026"
        trigger + r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
        # "May 15, 2026" or "May 15 2026"
        trigger + r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        # "15/05/2026" or "15-05-2026"
        trigger + r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})",
        # ISO: "2026-05-15"
        trigger + r"(\d{4})-(\d{2})-(\d{2})",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups) == 3:
                a, b, c = groups
                # ISO pattern
                if len(a) == 4:
                    return f"{int(a):04d}-{int(b):02d}-{int(c):02d}"
                # DD/MM/YYYY
                if a.isdigit() and b.isdigit():
                    return f"{int(c):04d}-{int(b):02d}-{int(a):02d}"
                # DD Month YYYY
                if a.isdigit() and b.isalpha():
                    month = MONTHS.get(b.lower())
                    if month:
                        return f"{int(c):04d}-{month:02d}-{int(a):02d}"
                # Month DD YYYY
                if a.isalpha() and b.isdigit():
                    month = MONTHS.get(a.lower())
                    if month:
                        return f"{int(c):04d}-{month:02d}-{int(b):02d}"
        except (ValueError, TypeError):
            continue
    return ""


def extract_job_detail(http: requests.Session, job: Job) -> Job:
    try:
        r = http.get(job.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        job.description = text[:12000]

        salary_match = re.search(
            r"(?:£|GBP\s?)\s?\d{2,3}(?:,\d{3})*(?:\s?(?:-|to)\s?(?:£|GBP\s?)?\d{2,3}(?:,\d{3})*)?",
            text,
            re.IGNORECASE,
        )
        if salary_match:
            job.salary_text = salary_match.group(0)

        remote_patterns = [
            (r"\bremote\b", "remote"),
            (r"\bhybrid\b", "hybrid"),
            (r"on[- ]site|onsite", "onsite"),
        ]
        for pattern, label in remote_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                job.remote_status = label
                break

        if not job.location:
            loc_match = re.search(r"Location\s*[:\-]?\s*([^|]{1,80})", text, re.IGNORECASE)
            if loc_match:
                job.location = loc_match.group(1).strip()

        if not job.closing_date:
            job.closing_date = extract_closing_date(text)

    except Exception as exc:
        print(f"  Detail fetch failed for {job.url}: {exc}")
    return job


def _salary_band_score(salary_text: str, weights: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract the lower salary figure and return bonus points + label.
    Tiers (based on £60k target):
      below £60k  →  -10  (known low salary, deprioritise)
      £60k–£69k   →  +10
      £70k–£79k   →  +15
      £80k–£89k   →  +20
      £90k+       →  +25
      unparseable →   0   (caller falls back to flat salary_signal)
    """
    if not salary_text:
        return {"points": 0, "label": ""}

    # Pull all numbers from the salary string and take the lowest (the floor)
    nums = re.findall(r"\d[\d,]*", salary_text)
    if not nums:
        return {"points": 0, "label": ""}

    values = []
    for n in nums:
        try:
            values.append(int(n.replace(",", "")))
        except ValueError:
            pass

    if not values:
        return {"points": 0, "label": ""}

    floor = min(values)

    # Ignore implausible values (hourly rates, etc.)
    if floor < 10_000 or floor > 500_000:
        return {"points": 0, "label": ""}

    if floor >= 90_000:
        return {"points": weights.get("salary_90k_plus", 25), "label": "salary_90k+"}
    if floor >= 80_000:
        return {"points": weights.get("salary_80k_plus", 20), "label": "salary_80k+"}
    if floor >= 70_000:
        return {"points": weights.get("salary_70k_plus", 15), "label": "salary_70k+"}
    if floor >= 60_000:
        return {"points": weights.get("salary_60k_plus", 10), "label": "salary_60k+"}
    # Known salary but below £60k
    return {"points": weights.get("salary_below_60k", -10), "label": "salary_below_60k"}


def score_job(job: Job, config: Dict[str, Any]) -> Job:
    weights = config["weights"]
    include_keywords = [k.lower() for k in config["filters"]["include_keywords"]]
    exclude_keywords = [k.lower() for k in config["filters"]["exclude_keywords"]]
    preferred_locations = [k.lower() for k in config["filters"]["preferred_locations"]]

    title = (job.title or "").lower()
    desc = (job.description or "").lower()
    loc = (job.location or "").lower()
    all_text = f"{title} {desc} {loc} {job.employer.lower()}"

    score = 0.0
    matched = []

    senior_terms = ["director", "dean", "pro vice-chancellor", "vice provost", "head of", "chief", "associate director"]
    if any(t in title for t in senior_terms):
        score += weights["senior_title"]
        matched.append("senior_title")

    he_terms = ["higher education", "university", "college", "academic", "faculty", "student experience", "student success"]
    if any(t in all_text for t in he_terms):
        score += weights["he_signal"]
        matched.append("he_signal")

    education_terms = ["education", "learning and teaching", "teaching and learning", "educational excellence", "academic quality", "curriculum"]
    if any(t in all_text for t in education_terms):
        score += weights["education_signal"]
        matched.append("education_signal")

    strategy_terms = ["strategy", "strategic", "institutional", "portfolio", "transformation", "leadership"]
    if any(t in all_text for t in strategy_terms):
        score += weights["strategy_signal"]
        matched.append("strategy_signal")

    digital_terms = ["digital", "ai", "artificial intelligence", "edtech", "online learning"]
    if any(t in all_text for t in digital_terms):
        score += weights["digital_signal"]
        matched.append("digital_signal")

    # Charity-specific signals (only fire if these weights are defined in config)
    if "sector_signal" in weights:
        sector_terms = ["charity", "charities", "charitable", "voluntary", "third sector",
                        "not-for-profit", "nfp", "ngo", "social enterprise", "civil society",
                        "beneficiar", "fundrais"]
        if any(t in all_text for t in sector_terms):
            score += weights["sector_signal"]
            matched.append("sector_signal")

    if "governance_signal" in weights:
        gov_terms = ["trustee", "board", "governance", "chief executive", "executive director",
                     "director general"]
        if any(t in all_text for t in gov_terms):
            score += weights["governance_signal"]
            matched.append("governance_signal")

    # Specific UK signals only - avoids false matches on "truck", "unique", etc.
    if any(t in all_text for t in [".ac.uk", "united kingdom", "england", "scotland", "wales", "northern ireland"]):
        score += weights["uk_signal"]
        matched.append("uk_signal")

    if job.remote_status in {"remote", "hybrid"} or any(t in loc for t in preferred_locations):
        score += weights["remote_hybrid_signal"]
        matched.append("remote_hybrid_signal")

    # Salary band scoring — extracts the lower figure and rewards £60k+ roles
    salary_band = _salary_band_score(job.salary_text, weights)
    if salary_band["points"] != 0:
        score += salary_band["points"]
        matched.append(salary_band["label"])
    elif job.salary_text:
        # Salary present but below threshold or unparseable — still record it
        score += weights.get("salary_signal", 5)
        matched.append("salary_signal")

    include_hits = [k for k in include_keywords if k in all_text]
    score += min(len(include_hits) * 2, 12)
    matched.extend([f"kw:{k}" for k in include_hits[:6]])

    exclude_hits = [k for k in exclude_keywords if k in all_text]
    if exclude_hits:
        score += weights["exclude_penalty"]
        matched.extend([f"exclude:{k}" for k in exclude_hits[:3]])

    job.score = round(score, 1)
    job.matched_terms = ", ".join(matched)
    return job


def render_report(rows: List[sqlite3.Row], title: str = "Job Search Report") -> str:
    lines = []
    lines.append(f"{title} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"{len(rows)} job(s) listed\n")
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. {r['title']} — {r['employer']}")
        lines.append(f"   Score: {r['score']} | Location: {r['location'] or 'Unknown'} | Remote: {r['remote_status'] or 'Unknown'}")
        if r['salary_text']:
            lines.append(f"   Salary: {r['salary_text']}")
        if r['closing_date']:
            lines.append(f"   Closes: {r['closing_date']}")
        lines.append(f"   Source: {r['source']} ({r['source_kind']})")
        lines.append(f"   Match: {r['matched_terms']}")
        lines.append(f"   URL: {r['url']}")
        lines.append("")
    if not rows:
        lines.append("No new matching jobs found.")
    return "\n".join(lines)


def render_html_report(rows: List[sqlite3.Row], title: str = "Job Search Report") -> str:
    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    remote_colours = {
        "remote":  ("#d1fae5", "#065f46"),
        "hybrid":  ("#dbeafe", "#1e40af"),
        "onsite":  ("#f3f4f6", "#374151"),
    }

    def badge(label: str) -> str:
        if not label:
            return ""
        bg, fg = remote_colours.get(label.lower(), ("#f3f4f6", "#374151"))
        return (
            f'<span style="background:{bg};color:{fg};padding:2px 8px;'
            f'border-radius:999px;font-size:11px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:.5px;">{label}</span>'
        )

    today = datetime.now().date()

    def closing_date_row(cd: str) -> str:
        if not cd:
            return ""
        try:
            d = datetime.strptime(cd, "%Y-%m-%d").date()
            days_left = (d - today).days
            label = d.strftime("%-d %B %Y")
            if days_left < 0:
                return ""  # expired — shouldn't appear but guard anyway
            elif days_left <= 3:
                colour = "#dc2626"  # red
                urgency = f" — <strong>CLOSES IN {days_left} DAY{'S' if days_left != 1 else ''}!</strong>"
            elif days_left <= 7:
                colour = "#d97706"  # amber
                urgency = f" — closes in {days_left} days"
            else:
                colour = "#374151"  # neutral
                urgency = ""
            return (
                f'<tr><td style="color:#6b7280;padding:2px 0;width:90px;">Closes</td>'
                f'<td style="color:{colour};font-weight:600;">{label}{urgency}</td></tr>'
            )
        except ValueError:
            return ""

    cards = []
    for r in rows:
        salary_row = ""
        if r["salary_text"]:
            salary_row = (
                f'<tr><td style="color:#6b7280;padding:2px 0;width:90px;">Salary</td>'
                f'<td style="font-weight:600;color:#059669;">{r["salary_text"]}</td></tr>'
            )
        deadline_row = closing_date_row(r["closing_date"] or "")

        # Build source list from all_sources JSON (may include duplicates from other boards)
        try:
            sources_list = json.loads(r["all_sources"] or "[]")
        except (json.JSONDecodeError, TypeError):
            sources_list = [{"source": r["source"], "url": r["url"]}]
        if not sources_list:
            sources_list = [{"source": r["source"], "url": r["url"]}]

        # Primary link = first source (the canonical one)
        primary_url = sources_list[0]["url"] if sources_list else r["url"]

        # Source chips — one per listing board
        source_chips = " ".join(
            f'<a href="{s["url"]}" style="display:inline-block;background:#f3f4f6;color:#374151;'
            f'border:1px solid #e5e7eb;border-radius:4px;padding:2px 8px;font-size:11px;'
            f'font-weight:600;text-decoration:none;margin-right:4px;margin-bottom:4px;">'
            f'{s["source"]}</a>'
            for s in sources_list
        )

        # "Also on N boards" label when deduped
        also_on = ""
        if len(sources_list) > 1:
            also_on = (
                f'<div style="margin-top:6px;font-size:11px;color:#6b7280;">'
                f'Listed on {len(sources_list)} boards &mdash; apply via any link above</div>'
            )

        # Multiple "View Job" buttons when deduped
        apply_buttons = " ".join(
            f'<a href="{s["url"]}" style="display:inline-block;background:#4f46e5;color:#ffffff;'
            f'padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;'
            f'text-decoration:none;margin-right:8px;margin-bottom:6px;">'
            f'{"View Job" if i == 0 else s["source"]} &rarr;</a>'
            for i, s in enumerate(sources_list)
        )

        cards.append(f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;
                    padding:20px 24px;margin-bottom:16px;">
          <div style="margin-bottom:12px;">
            <div style="margin-bottom:6px;">
              <span style="display:inline-block;background:#eef2ff;color:#4f46e5;
                           font-size:11px;font-weight:700;padding:2px 10px;
                           border-radius:999px;letter-spacing:.5px;">
                SCORE &nbsp;{r['score']}
              </span>
            </div>
            <a href="{primary_url}" style="font-size:17px;font-weight:700;color:#111827;
                                        text-decoration:none;line-height:1.3;">{r['title']}</a>
            <div style="color:#4b5563;font-size:14px;margin-top:3px;">{r['employer']}</div>
          </div>
          <table style="font-size:13px;border-collapse:collapse;width:100%;">
            <tr>
              <td style="color:#6b7280;padding:2px 0;width:90px;">Location</td>
              <td>{r['location'] or 'Unknown'} &nbsp; {badge(r['remote_status'])}</td>
            </tr>
            {salary_row}
            {deadline_row}
            <tr>
              <td style="color:#6b7280;padding:2px 0;vertical-align:top;">Sources</td>
              <td>{source_chips}{also_on}</td>
            </tr>
            <tr>
              <td style="color:#6b7280;padding:2px 0;">Signals</td>
              <td style="color:#6b7280;font-size:12px;">{r['matched_terms']}</td>
            </tr>
          </table>
          <div style="margin-top:14px;flex-wrap:wrap;">
            {apply_buttons}
          </div>
        </div>""")

    body = "\n".join(cards) if cards else (
        '<p style="color:#6b7280;font-size:15px;">No new matching jobs found since the last run.</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:32px auto;padding:0 16px;">

    <!-- Header -->
    <div style="background:#4f46e5;border-radius:8px 8px 0 0;padding:24px 28px;">
      <div style="color:#ffffff;font-size:20px;font-weight:700;">{title}</div>
      <div style="color:#c7d2fe;font-size:13px;margin-top:4px;">Generated {now}</div>
    </div>

    <!-- Count bar -->
    <div style="background:#eef2ff;padding:12px 28px;border-left:1px solid #e0e7ff;
                border-right:1px solid #e0e7ff;font-size:14px;color:#3730a3;font-weight:600;">
      {len(rows)} job{'s' if len(rows) != 1 else ''} found
    </div>

    <!-- Cards -->
    <div style="padding:20px 0;">
      {body}
    </div>

    <!-- Footer -->
    <div style="text-align:center;color:#9ca3af;font-size:12px;padding:16px 0 32px;">
      Sent by job-search-agent &bull; Runs daily at 06:15 and 15:45 UTC
    </div>

  </div>
</body>
</html>"""


def render_html_digest(rows, config: dict = None) -> str:
    """Compact weekly digest — top 10 jobs ranked by score."""
    import json
    now = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=6)).strftime("%-d %b")
    week_end = datetime.now().strftime("%-d %b %Y")
    minimum_score = (config or {}).get("filters", {}).get("minimum_score", 0)

    today = datetime.now().date()

    def deadline_badge(cd: str) -> str:
        if not cd:
            return ""
        try:
            d = datetime.strptime(cd, "%Y-%m-%d").date()
            days_left = (d - today).days
            if days_left < 0:
                return ""
            label = d.strftime("%-d %b")
            if days_left <= 3:
                colour, txt = "#dc2626", f"{label} (!)"
            elif days_left <= 7:
                colour, txt = "#d97706", label
            else:
                colour, txt = "#6b7280", label
            return (
                f'<span style="color:{colour};font-size:11px;font-weight:600;">'
                f'{txt}</span>'
            )
        except ValueError:
            return ""

    rows_html = ""
    for rank, r in enumerate(rows, start=1):
        try:
            sources_list = json.loads(r["all_sources"] or "[]")
        except Exception:
            sources_list = [{"source": r["source"], "url": r["url"]}]
        if not sources_list:
            sources_list = [{"source": r["source"], "url": r["url"]}]
        primary_url = sources_list[0]["url"]
        source_label = ", ".join(s["source"] for s in sources_list)

        dl = deadline_badge(r["closing_date"] or "")
        dl_cell = f'<td style="padding:8px 6px;white-space:nowrap;">{dl}</td>' if dl else '<td></td>'

        bg = "#fafafa" if rank % 2 == 0 else "#ffffff"
        rows_html += f"""
        <tr style="background:{bg};">
          <td style="padding:8px 10px;font-size:13px;font-weight:700;color:#4f46e5;
                     text-align:center;width:28px;">{rank}</td>
          <td style="padding:8px 6px;">
            <a href="{primary_url}" style="font-size:14px;font-weight:600;color:#111827;
               text-decoration:none;">{r['title']}</a>
            <div style="font-size:12px;color:#6b7280;margin-top:1px;">{r['employer']}</div>
          </td>
          <td style="padding:8px 6px;font-size:12px;color:#374151;white-space:nowrap;">
            {r['score']}
          </td>
          {dl_cell}
          <td style="padding:8px 6px;font-size:11px;color:#9ca3af;">{source_label}</td>
        </tr>"""

    if not rows_html:
        rows_html = """
        <tr><td colspan="5" style="padding:20px;text-align:center;color:#9ca3af;">
          No new jobs this week.
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:32px auto;padding:0 16px;">

    <div style="background:#4f46e5;border-radius:8px 8px 0 0;padding:20px 24px;">
      <div style="color:#ffffff;font-size:18px;font-weight:700;">
        Weekly Digest &mdash; Top {len(rows)} Jobs
      </div>
      <div style="color:#c7d2fe;font-size:12px;margin-top:3px;">
        {week_start} &ndash; {week_end}
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse;background:#ffffff;
                  border:1px solid #e5e7eb;border-top:none;">
      <thead>
        <tr style="background:#f3f4f6;border-bottom:2px solid #e5e7eb;">
          <th style="padding:8px 10px;font-size:11px;color:#6b7280;text-align:center;">#</th>
          <th style="padding:8px 6px;font-size:11px;color:#6b7280;text-align:left;">Role</th>
          <th style="padding:8px 6px;font-size:11px;color:#6b7280;">Score</th>
          <th style="padding:8px 6px;font-size:11px;color:#6b7280;">Closes</th>
          <th style="padding:8px 6px;font-size:11px;color:#6b7280;text-align:left;">Source</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>

    <div style="text-align:center;color:#9ca3af;font-size:11px;padding:16px 0 28px;">
      Sent by job-search-agent &bull; Weekly digest every Friday
    </div>

  </div>
</body>
</html>"""


def save_report(report: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)


def save_csv(rows: List[sqlite3.Row], path: str = "latest_jobs.csv") -> None:
    import csv
    fields = [
        "title", "employer", "location", "remote_status", "salary_text",
        "score", "source", "source_kind", "matched_terms", "url", "closing_date", "fetched_at", "first_seen_at"
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in fields})


def build_sources(config: Dict[str, Any]) -> List[Source]:
    return [Source(**src) for src in config["sources"] if src.get("enabled", True)]


def run(config: Dict[str, Any] = CONFIG) -> Tuple[int, int]:
    http = session()
    sources = build_sources(config)
    minimum_score = config["filters"]["minimum_score"]
    now = datetime.now(timezone.utc).isoformat()
    total_fetched = 0
    total_stored = 0
    new_rows = []

    db_path = config.get("db_path", DB_PATH)
    prefix = config.get("output_prefix", "")

    with Database(db_path) as db:
        for src in sources:
            scraper_cls = SCRAPER_MAP.get(src.kind)
            if not scraper_cls:
                print(f"Skipping unsupported source kind: {src.kind}")
                continue

            try:
                scraper = scraper_cls(src, http)
                jobs = scraper.fetch()
                stored = 0
                skipped = 0

                for job in jobs:
                    total_fetched += 1

                    # Step 1: prescore on title/employer only
                    ps = prescore_job(job, config)
                    if ps < PRESCORE_THRESHOLD:
                        skipped += 1
                        continue

                    # Step 2: fetch detail page and do full scoring
                    job = extract_job_detail(http, job)
                    job = score_job(job, config)
                    time.sleep(0.5)

                    if job.score >= minimum_score:
                        # Step 3: smart de-duplication
                        canonical_fp = db.find_canonical(job.title, job.employer)
                        if canonical_fp and canonical_fp != job.fingerprint:
                            db.merge_into_canonical(canonical_fp, job)
                            print(f"    ↳ Duplicate merged into canonical {canonical_fp[:8]}…")
                        else:
                            db.upsert_job(job, first_seen_at=now, canonical_id=job.fingerprint)
                            stored += 1
                            total_stored += 1

                print(f"  {src.name}: {len(jobs)} listed, {skipped} skipped by prescore, {stored} stored")

            except Exception as exc:
                print(f"  Error processing {src.name}: {exc}")

        label = config.get("label", "Jobs")
        # Full report (all-time top jobs)
        all_rows = db.top_jobs(limit=50, minimum_score=minimum_score)
        full_report = render_report(all_rows, title=f"Full {label} Report (All Time Top 50)")
        save_report(full_report, f"{prefix}latest_report.txt")
        save_csv(all_rows, f"{prefix}latest_jobs.csv")

        # New jobs report (last 25h)
        new_rows = db.new_jobs(hours=25, minimum_score=minimum_score)
        new_report = render_report(new_rows, title=f"New {label} Since Last Run")
        save_report(new_report, f"{prefix}new_jobs_report.txt")
        html_report = render_html_report(new_rows, title=f"New {label} Since Last Run")
        save_report(html_report, f"{prefix}new_jobs_report.html")

    print(f"\nTotal fetched: {total_fetched} | Stored: {total_stored} | New this run: {len(new_rows)}")
    return total_stored, len(new_rows)


def run_weekly_digest(config: dict = CONFIG) -> int:
    """Generate and save the weekly digest (top 10 from the past 7 days)."""
    minimum_score = config["filters"]["minimum_score"]
    db_path = config.get("db_path", DB_PATH)
    prefix = config.get("output_prefix", "")
    with Database(db_path) as db:
        rows = db.weekly_jobs(days=7, limit=10, minimum_score=minimum_score)
        html = render_html_digest(rows, config=config)
        save_report(html, f"{prefix}weekly_digest.html")
    print(f"Weekly digest: {len(rows)} jobs → {prefix}weekly_digest.html")
    return len(rows)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Job Search Agent")
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Generate weekly digest instead of running the full scrape",
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default="he",
        help="Which search profile to run (default: he)",
    )
    args = parser.parse_args()
    cfg = PROFILES[args.profile]
    prefix = cfg.get("output_prefix", "")

    if args.weekly:
        run_weekly_digest(cfg)
        print(f"Artifact written: {prefix}weekly_digest.html")
    else:
        run(cfg)
        print(
            f"Artifacts written: {prefix}latest_report.txt, "
            f"{prefix}new_jobs_report.txt, {prefix}new_jobs_report.html, "
            f"{prefix}latest_jobs.csv, {cfg['db_path']}"
        )
