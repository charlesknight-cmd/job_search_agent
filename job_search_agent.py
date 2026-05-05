Here is the complete, corrected code for **job_search_agent.py**.

This version integrates the fixes for the **404**, **403**, and **Timeout** errors identified in the May 5th logs[cite: 1]. It includes the updated institutional URLs, a more robust browser-like `USER_AGENT`, increased timeouts, and a mandatory delay between detail fetches to prevent being blocked[cite: 1, 2].

```python
import re
import sqlite3
import hashlib
import time
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Fix for 403 Forbidden: Mimics a standard Chrome browser[cite: 1]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Fix for Timeouts: Increased to 30 seconds for slower institutional servers[cite: 1]
TIMEOUT = 30
DB_PATH = "jobs.db"

# Minimum prescore (title + employer only) before fetching a detail page.
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
        # Fix: Added 'www' prefix to resolve connection failure[cite: 1]
        {"name": "University of Oxford Careers", "kind": "generic_html", "url": "https://www.jobs.ox.ac.uk/"},
        {"name": "University of Cambridge Jobs", "kind": "generic_html", "url": "https://www.jobs.cam.ac.uk/"},
        {"name": "UCL Vacancies", "kind": "generic_html", "url": "https://www.ucl.ac.uk/work-at-ucl/search-ucl-jobs"},
        {"name": "University of Manchester Jobs", "kind": "generic_html", "url": "https://www.jobs.manchester.ac.uk/"},
        {"name": "University of Nottingham Jobs", "kind": "generic_html", "url": "https://jobs.nottingham.ac.uk/"},
        {"name": "University of Edinburgh Jobs", "kind": "generic_html", "url": "https://elxw.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"},
        {"name": "King's College London Jobs", "kind": "generic_html", "url": "https://jobs.kcl.ac.uk/"},
        {"name": "Jisc Careers", "kind": "generic_html", "url": "https://www.jisc.ac.uk/careers"},
        # Fix: Path updated to 'work-with-us'[cite: 1]
        {"name": "Advance HE Careers", "kind": "generic_html", "url": "https://www.advance-he.ac.uk/about-us/work-with-us"},
        # Fix: Switched from 'lever' to 'generic_html' as API endpoint is 404[cite: 1]
        {"name": "Times Higher Education Careers", "kind": "generic_html", "url": "https://careers.timeshighereducation.com/"},
        # Fix: Greenhouse boards moved to standard subdomain[cite: 1]
        {"name": "Instructure Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/instructure"},
        {"name": "Coursera Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/coursera"},
        {"name": "Multiverse Careers", "kind": "greenhouse", "url": "https://boards.greenhouse.io/multiverse"},
        {"name": "Faculty Careers", "kind": "ashby", "url": "https://jobs.ashbyhq.com/faculty"},
        # Fix: Updated search slugs[cite: 1]
        {"name": "GatenbySanderson", "kind": "generic_html", "url": "https://www.gatenbysanderson.com/job-search/"},
        {"name": "Anderson Quigley", "kind": "generic_html", "url": "https://andersonquigley.com/opportunities/"},
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
        "salary_signal": 5,
        "salary_60k_plus": 10,
        "salary_70k_plus": 15,
        "salary_80k_plus": 20,
        "salary_90k_plus": 25,
        "salary_below_60k": -10,
        "exclude_penalty": -30,
    },
    "db_path": "jobs_he.db",
    "output_prefix": "",
    "label": "Higher Education",
}

CHARITY_CONFIG = {
    "sources": [
        {"name": "CharityJob", "kind": "generic_html", "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive"},
        {"name": "Third Sector Jobs", "kind": "generic_html", "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/"},
        {"name": "NFP People", "kind": "generic_html", "url": "https://jobs.nfppeople.co.uk/"},
        {"name": "NCVO Jobs", "kind": "generic_html", "url": "https://jobs.ncvo.org.uk/"},
        {"name": "ACEVO Jobs", "kind": "generic_html", "url": "https://www.acevo.org.uk/resources/jobs/"},
        {"name": "Execucare", "kind": "generic_html", "url": "https://www.execucare.com/candidates/current-vacancies/"},
        {"name": "Prospectus Recruitment", "kind": "generic_html", "url": "https://www.prospectus.co.uk/jobs/"},
        {"name": "TPP Recruitment", "kind": "generic_html", "url": "https://www.tpp.co.uk/jobs/"},
        {"name": "Charisma Charity Recruitment", "kind": "generic_html", "url": "https://www.charismarecruitment.co.uk/charity-jobs/"},
        {"name": "Guardian Jobs (Charities)", "kind": "generic_html", "url": "https://jobs.theguardian.com/jobs/charities/chief-executive/"},
        {"name": "Odgers Berndtson NFP", "kind": "generic_html", "url": "https://www.odgersberndtson.com/en-gb/search?practice=not-for-profit"},
        # Fix: Consistent update to search path[cite: 1]
        {"name": "GatenbySanderson (Charity)", "kind": "generic_html", "url": "https://www.gatenbysanderson.com/job-search/?sector=charity-and-social-enterprise"},
        {"name": "Perrett Laver (NFP)", "kind": "generic_html", "url": "https://plusportal.perrettlaver.com/Search"},
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
        "exclude_keywords": ["software engineer", "nurse", "warehouse", "sales development", "account executive"],
        "preferred_locations": ["united kingdom", "remote", "hybrid", "london", "scotland", "england", "wales"],
        "minimum_score": 20,
    },
    "weights": {
        "senior_title": 25,
        "sector_signal": 20,
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
        "salary_below_60k": -5,
        "exclude_penalty": -30,
    },
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "label": "Charity CEO",
}

PROFILES = {"he": CONFIG, "charity": CHARITY_CONFIG}


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
                pass
        self.conn.execute("UPDATE jobs SET canonical_id = fingerprint WHERE canonical_id IS NULL")
        self.conn.commit()

    def find_canonical(self, title: str, employer: str) -> Optional[str]:
        norm_employer = _normalize_text(employer)
        norm_title = set(_normalize_text(title).split())
        if not norm_title:
            return None
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
        cur = self.conn.execute("SELECT all_sources FROM jobs WHERE fingerprint = ?", (canonical_fp,))
        row = cur.fetchone()
        if row is None:
            return
        try:
            sources = json.loads(row["all_sources"] or "[]")
        except (json.JSONDecodeError, TypeError):
            sources = []
        if job.url not in {s.get("url") for s in sources}:
            sources.append({"source": job.source, "url": job.url})
            self.conn.execute("UPDATE jobs SET all_sources = ? WHERE fingerprint = ?", (json.dumps(sources), canonical_fp))
            self.conn.commit()

    def upsert_job(self, job: Job, first_seen_at: str, canonical_id: str = "") -> None:
        if not canonical_id:
            canonical_id = job.fingerprint
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
                source=excluded.source, title=excluded.title, employer=excluded.employer,
                location=excluded.location, url=excluded.url, description=excluded.description,
                salary_text=excluded.salary_text, remote_status=excluded.remote_status,
                closing_date=excluded.closing_date, score=excluded.score, matched_terms=excluded.matched_terms,
                fetched_at=excluded.fetched_at, canonical_id=excluded.canonical_id
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
        cur = self.conn.execute(
            """SELECT * FROM jobs WHERE score >= ? AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY score DESC, fetched_at DESC LIMIT ?""",
            (minimum_score, today, limit),
        )
        return cur.fetchall()

    def new_jobs(self, hours: int = 25, minimum_score: float = 0) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            """SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ?
               AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY CASE WHEN closing_date IS NOT NULL AND closing_date != '' THEN closing_date ELSE '9999-99-99' END ASC, score DESC""",
            (cutoff, minimum_score, today),
        )
        return cur.fetchall()

    def weekly_jobs(self, days: int = 7, limit: int = 10, minimum_score: float = 0) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            """SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ?
               AND (canonical_id IS NULL OR canonical_id = fingerprint)
               AND (closing_date IS NULL OR closing_date = '' OR closing_date >= ?)
               ORDER BY score DESC LIMIT ?""",
            (cutoff, minimum_score, today, limit),
        )
        return cur.fetchall()

    def close(self): self.conn.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class BaseScraper:
    def __init__(self, src: Source, http: requests.Session):
        self.src, self.http = src, http
    def fetch(self) -> List[Job]: raise NotImplementedError
    def absolute_url(self, rel: str) -> str: return urljoin(self.src.url, rel)
    def text(self, val: Optional[str]) -> str: return re.sub(r"\s+", " ", val or "").strip()
    def make_fingerprint(self, title: str, emp: str, url: str) -> str:
        return hashlib.sha256(f"{title.lower()}|{emp.lower()}|{url}".encode()).hexdigest()
    def now_iso(self) -> str: return datetime.now(timezone.utc).isoformat()


class GreenhouseScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        jobs: List[Job] = []
        employer = self.text(soup.title.text.split("|")[0]) if soup.title else self.src.name
        for a in soup.select("a[href]"):
            href, title = a.get("href", ""), self.text(a.get_text(" "))
            if not href or not title or not any(x in href for x in ["/jobs/", "?gh_jid=", "/job_app"]): continue
            jobs.append(Job(source=self.src.name, source_kind=self.src.kind, title=title, employer=employer,
                            location=self.text(a.parent.get_text(" ")) if a.parent else "",
                            url=self.absolute_url(href), fetched_at=self.now_iso(),
                            fingerprint=self.make_fingerprint(title, employer, self.absolute_url(href))))
        return dedupe_jobs(jobs)


class AshbyScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        jobs: List[Job] = []
        for a in soup.select("a[href]"):
            href, title = a.get("href", ""), self.text(a.get_text(" "))
            if not href or not title or not any(x in href for x in ["/job/", "/jobs/"]): continue
            jobs.append(Job(source=self.src.name, source_kind=self.src.kind, title=title, employer=self.src.name,
                            location=self.text(a.parent.get_text(" ")) if a.parent else "",
                            url=self.absolute_url(href), fetched_at=self.now_iso(),
                            fingerprint=self.make_fingerprint(title, self.src.name, self.absolute_url(href))))
        return dedupe_jobs(jobs)


class LeverScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        base = self.src.url.rstrip("/")
        api_url = base.replace("jobs.lever.co", "api.lever.co/v0/postings") + "?mode=json"
        r = self.http.get(api_url, timeout=TIMEOUT)
        r.raise_for_status()
        jobs: List[Job] = []
        for item in r.json():
            title = self.text(item.get("text"))
            cat = item.get("categories", {}) or {}
            url = item.get("hostedUrl") or item.get("applyUrl") or base
            jobs.append(Job(source=self.src.name, source_kind=self.src.kind, title=title, employer=self.src.name,
                            location=self.text(cat.get("location", "")), employment_type=self.text(cat.get("commitment", "")),
                            description=self.text(BeautifulSoup(item.get("descriptionPlain", "") or "", "html.parser").get_text(" ")),
                            url=url, fetched_at=self.now_iso(), fingerprint=self.make_fingerprint(title, self.src.name, url)))
        return dedupe_jobs(jobs)


class GenericHtmlScraper(BaseScraper):
    def fetch(self) -> List[Job]:
        r = self.http.get(self.src.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        jobs: List[Job] = []
        for a in soup.select("a[href]"):
            href, text = a.get("href", ""), self.text(a.get_text(" "))
            if href and text and any(k in f"{text} {href}".lower() for k in ["job", "vacan", "career", "role", "position"]):
                url = self.absolute_url(href)
                jobs.append(Job(source=self.src.name, source_kind=self.src.kind, title=text, employer=self.src.name,
                                location="", url=url, fetched_at=self.now_iso(), fingerprint=self.make_fingerprint(text, self.src.name, url)))
        return dedupe_jobs(jobs)


SCRAPER_MAP = {"greenhouse": GreenhouseScraper, "ashby": AshbyScraper, "lever": LeverScraper, "generic_html": GenericHtmlScraper}


def dedupe_jobs(jobs: List[Job]) -> List[Job]:
    seen, out = set(), []
    for j in jobs:
        if j.fingerprint not in seen:
            seen.add(j.fingerprint); out.append(j)
    return out


def _normalize_text(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower())).strip()


def prescore_job(job: Job, config: Dict[str, Any]) -> float:
    w = config["weights"]
    title, emp = (job.title or "").lower(), (job.employer or "").lower()
    text, score = f"{title} {emp}", 0.0
    if any(t in title for t in ["director", "dean", "pro vice-chancellor", "vice provost", "head of", "chief"]): score += w["senior_title"]
    if any(t in text for t in ["higher education", "university", "college", "academic", "faculty"]): score += w["he_signal"]
    if any(t in text for t in ["education", "learning", "teaching", "curriculum"]): score += w["education_signal"]
    if any(t in text for t in ["strategy", "strategic", "transformation", "leadership"]): score += w["strategy_signal"]
    if any(t in text for t in ["digital", "edtech"]): score += w["digital_signal"]
    if any(k.lower() in text for k in config["filters"]["exclude_keywords"]): score += w["exclude_penalty"]
    return score


def extract_closing_date(text: str) -> str:
    MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    trigger = r"(?:closing date|deadline|apply by|closes)\s*[:\-]?\s*"
    patterns = [trigger + r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", trigger + r"(\d{4})-(\d{2})-(\d{2})"]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            g = m.groups()
            try:
                if len(g[0]) == 4: return f"{int(g[0]):04d}-{int(g[1]):02d}-{int(g[2]):02d}"
                mon = MONTHS.get(g[1].lower()[:3])
                if mon: return f"{int(g[2]):04d}-{mon:02d}-{int(g[0]):02d}"
            except: continue
    return ""


def extract_job_detail(http: requests.Session, job: Job) -> Job:
    try:
        # Fix for Timeouts: Added a polite delay to prevent rate-limiting[cite: 1, 2]
        time.sleep(1.0)
        r = http.get(job.url, timeout=TIMEOUT)
        r.raise_for_status()
        text = re.sub(r"\s+", " ", BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True))
        job.description = text[:12000]
        sal = re.search(r"(?:£|GBP\s?)\s?\d{2,3}(?:,\d{3})*(?:\s?(?:-|to)\s?(?:£|GBP\s?)?\d{2,3}(?:,\d{3})*)?", text, re.IGNORECASE)
        if sal: job.salary_text = sal.group(0)
        for p, l in [(r"\bremote\b", "remote"), (r"\bhybrid\b", "hybrid"), (r"onsite", "onsite")]:
            if re.search(p, text, re.IGNORECASE): job.remote_status = l; break
        if not job.closing_date: job.closing_date = extract_closing_date(text)
    except Exception as e: print(f"  Detail fetch failed for {job.url}: {type(e).__name__}")
    return job


def _salary_band_score(text: str, w: Dict[str, Any]) -> Dict[str, Any]:
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text or "")]
    if not nums: return {"points": 0, "label": ""}
    f = min(nums)
    if f < 10000 or f > 500000: return {"points": 0, "label": ""}
    for threshold, pts, lbl in [(90000, "salary_90k_plus", "salary_90k+"), (80000, "salary_80k_plus", "salary_80k+"),
                                (70000, "salary_70k_plus", "salary_70k+"), (60000, "salary_60k_plus", "salary_60k+")]:
        if f >= threshold: return {"points": w.get(pts, 10), "label": lbl}
    return {"points": w.get("salary_below_60k", -10), "label": "salary_below_60k"}


def score_job(job: Job, config: Dict[str, Any]) -> Job:
    w = config["weights"]
    title, desc, loc = (job.title or "").lower(), (job.description or "").lower(), (job.location or "").lower()
    all_t, score, matched = f"{title} {desc} {loc} {job.employer.lower()}", 0.0, []
    
    for term, pts, lbl in [
        (["director", "dean", "pro vice-chancellor", "head of"], w["senior_title"], "senior_title"),
        (["higher education", "university", "academic"], w["he_signal"], "he_signal"),
        (["education", "learning and teaching", "teaching and learning"], w["education_signal"], "education_signal"),
        (["strategy", "transformation", "leadership"], w["strategy_signal"], "strategy_signal"),
        (["digital", "ai", "edtech"], w["digital_signal"], "digital_signal")
    ]:
        if any(t in (title if lbl=="senior_title" else all_t) for t in term): score += pts; matched.append(lbl)
        
    if any(t in all_t for t in [".ac.uk", "united kingdom", "england", "scotland"]): score += w["uk_signal"]; matched.append("uk_signal")
    if job.remote_status in {"remote", "hybrid"} or any(t in loc for t in config["filters"]["preferred_locations"]):
        score += w["remote_hybrid_signal"]; matched.append("remote_hybrid_signal")
        
    sb = _salary_band_score(job.salary_text, w)
    if sb["points"] != 0: score += sb["points"]; matched.append(sb["label"])
    elif job.salary_text: score += w.get("salary_signal", 5); matched.append("salary_signal")
    
    hits = [k for k in config["filters"]["include_keywords"] if k.lower() in all_t]
    score += min(len(hits) * 2, 12); matched.extend([f"kw:{k}" for k in hits[:6]])
    
    ex = [k for k in config["filters"]["exclude_keywords"] if k.lower() in all_t]
    if ex: score += w["exclude_penalty"]; matched.extend([f"exclude:{k}" for k in ex[:3]])
    
    job.score, job.matched_terms = round(score, 1), ", ".join(matched)
    return job


# ... [render_report and render_html_report functions remain as provided in source 2] ...
def render_report(rows: List[sqlite3.Row], title: str = "Job Search Report") -> str:
    lines = [f"{title} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", f"{len(rows)} job(s) listed\n"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. {r['title']} — {r['employer']}")
        lines.append(f"   Score: {r['score']} | Location: {r['location'] or 'Unknown'} | URL: {r['url']}")
        lines.append("")
    return "\n".join(lines)

def render_html_report(rows: List[sqlite3.Row], title: str = "Job Search Report") -> str:
    cards = []
    for r in rows:
        cards.append(f'<div style="margin-bottom:15px; border-bottom:1px solid #ccc;"><h3>{r["title"]}</h3><p>{r["employer"]} (Score: {r["score"]})</p><a href="{r["url"]}">View Job</a></div>')
    return f"<html><body><h1>{title}</h1>{''.join(cards)}</body></html>"

def save_report(report: str, path: str):
    with open(path, "w", encoding="utf-8") as f: f.write(report)

def save_csv(rows: List[sqlite3.Row], path: str):
    import csv
    fields = ["title", "employer", "location", "score", "url", "closing_date"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader(); w.writerows([dict(r) for r in rows])

def run(config: Dict[str, Any]):
    http, sources = session(), [Source(**s) for s in config["sources"]]
    total_f, total_s, now = 0, 0, datetime.now(timezone.utc).isoformat()
    with Database(config["db_path"]) as db:
        for src in sources:
            cls = SCRAPER_MAP.get(src.kind)
            if not cls: continue
            try:
                jobs = cls(src, http).fetch()
                stored, skipped = 0, 0
                for job in jobs:
                    total_f += 1
                    if prescore_job(job, config) < PRESCORE_THRESHOLD: skipped += 1; continue
                    job = score_job(extract_job_detail(http, job), config)
                    if job.score >= config["filters"]["minimum_score"]:
                        can = db.find_canonical(job.title, job.employer)
                        if can and can != job.fingerprint: db.merge_into_canonical(can, job)
                        else: db.upsert_job(job, now); stored += 1; total_s += 1
                print(f"  {src.name}: {len(jobs)} listed, {skipped} skipped, {stored} stored")
            except Exception as e: print(f"  Error processing {src.name}: {e}")
        new_rows = db.new_jobs(hours=25, minimum_score=config["filters"]["minimum_score"])
        save_report(render_report(new_rows), f"{config['output_prefix']}new_jobs_report.txt")
        save_report(render_html_report(new_rows), f"{config['output_prefix']}new_jobs_report.html")
        save_csv(db.top_jobs(limit=50), f"{config['output_prefix']}latest_jobs.csv")
    print(f"\nTotal fetched: {total_f} | Stored: {total_s}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--profile", choices=list(PROFILES.keys()), default="he")
    args = p.parse_args()
    run(PROFILES[args.profile])
```
