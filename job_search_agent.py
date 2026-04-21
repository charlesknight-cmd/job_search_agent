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
        "exclude_penalty": -30,
    }
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
                first_seen_at TEXT
            )
            """
        )
        self.conn.commit()

    def _migrate(self):
        """Add new columns to existing databases without losing data."""
        try:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN first_seen_at TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def upsert_job(self, job: Job, first_seen_at: str):
        self.conn.execute(
            """
            INSERT INTO jobs (
                fingerprint, source, source_kind, title, employer, location, url,
                posted_text, description, employment_type, salary_text, remote_status,
                score, matched_terms, fetched_at, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                score=excluded.score,
                matched_terms=excluded.matched_terms,
                fetched_at=excluded.fetched_at
            """,
            (
                job.fingerprint, job.source, job.source_kind, job.title, job.employer,
                job.location, job.url, job.posted_text, job.description,
                job.employment_type, job.salary_text, job.remote_status,
                job.score, job.matched_terms, job.fetched_at, first_seen_at,
            ),
        )
        self.conn.commit()

    def top_jobs(self, limit: int = 50, minimum_score: float = 0) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE score >= ? ORDER BY score DESC, fetched_at DESC LIMIT ?",
            (minimum_score, limit),
        )
        return cur.fetchall()

    def new_jobs(self, hours: int = 25, minimum_score: float = 0) -> List[sqlite3.Row]:
        """Return jobs first seen within the last `hours` hours, above minimum_score."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE first_seen_at >= ? AND score >= ?
            ORDER BY score DESC
            """,
            (cutoff, minimum_score),
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
    except Exception as exc:
        print(f"  Detail fetch failed for {job.url}: {exc}")
    return job


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

    # Specific UK signals only - avoids false matches on "truck", "unique", etc.
    if any(t in all_text for t in [".ac.uk", "united kingdom", "england", "scotland", "wales", "northern ireland"]):
        score += weights["uk_signal"]
        matched.append("uk_signal")

    if job.remote_status in {"remote", "hybrid"} or any(t in loc for t in preferred_locations):
        score += weights["remote_hybrid_signal"]
        matched.append("remote_hybrid_signal")

    if job.salary_text:
        score += weights["salary_signal"]
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
        lines.append(f"   Source: {r['source']} ({r['source_kind']})")
        lines.append(f"   Match: {r['matched_terms']}")
        lines.append(f"   URL: {r['url']}")
        lines.append("")
    if not rows:
        lines.append("No new matching jobs found.")
    return "\n".join(lines)


def save_report(report: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)


def save_csv(rows: List[sqlite3.Row], path: str = "latest_jobs.csv") -> None:
    import csv
    fields = [
        "title", "employer", "location", "remote_status", "salary_text",
        "score", "source", "source_kind", "matched_terms", "url", "fetched_at", "first_seen_at"
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

    with Database() as db:
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

                    # Step 1: prescore on title/employer only — skip detail fetch if irrelevant
                    ps = prescore_job(job, config)
                    if ps < PRESCORE_THRESHOLD:
                        skipped += 1
                        continue

                    # Step 2: fetch detail page and do full scoring
                    job = extract_job_detail(http, job)
                    job = score_job(job, config)
                    time.sleep(0.5)

                    if job.score >= minimum_score:
                        db.upsert_job(job, first_seen_at=now)
                        stored += 1
                        total_stored += 1

                print(f"  {src.name}: {len(jobs)} listed, {skipped} skipped by prescore, {stored} stored")

            except Exception as exc:
                print(f"  Error processing {src.name}: {exc}")

        # Full report (all-time top jobs)
        all_rows = db.top_jobs(limit=50, minimum_score=minimum_score)
        full_report = render_report(all_rows, title="Full Job Search Report (All Time Top 50)")
        save_report(full_report, "latest_report.txt")
        save_csv(all_rows, "latest_jobs.csv")

        # New jobs report (last 25h — covers both daily runs with overlap)
        new_rows = db.new_jobs(hours=25, minimum_score=minimum_score)
        new_report = render_report(new_rows, title="New Jobs Since Last Run")
        save_report(new_report, "new_jobs_report.txt")

    print(f"\nTotal fetched: {total_fetched} | Stored: {total_stored} | New this run: {len(new_rows)}")
    return total_stored, len(new_rows)


if __name__ == "__main__":
    run(CONFIG)
    print("Artifacts written: latest_report.txt, new_jobs_report.txt, latest_jobs.csv, jobs.db")
