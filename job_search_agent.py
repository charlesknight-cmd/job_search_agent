import asyncio
import hashlib
import html
import logging
import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# --- SYSTEM SETTINGS ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 30
MAX_DESCRIPTION_CHARS = 5000
REPORT_ROW_LIMIT = 200
MAX_RETRIES = 2
RETRY_BACKOFF = 3.0  # seconds

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("job_search.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


@dataclass
class Job:
    source: str
    title: str
    employer: str
    url: str
    description: str = ""
    score: float = 0.0
    match_reasons: List[str] = field(default_factory=list)
    fetched_at: str = ""
    fingerprint: str = ""


# --- CONFIGURATION ---
HE_CONFIG = {
    "label": "Higher Education Leadership",
    "db_path": "jobs_he.db",
    "output_prefix": "",
    "filters": {"minimum_score": 25},
    "sources": [
        {"name": "jobs.ac.uk", "url": "https://www.jobs.ac.uk/search/senior-management", "selector": ".j-search-result__title a"},
        {"name": "THE UniJobs", "url": "https://www.timeshighereducation.com/unijobs/listings/united-kingdom/", "selector": ".job-results__title a"},
        {"name": "Peridot Partners", "url": "https://www.peridotpartners.co.uk/job-role/education-executive-roles/", "selector": ".card-title a"},
        # Odgers Berndtson excluded: vacancies are JavaScript-rendered, not in HTML source
        # Perrett Laver: selector targets vacancy links by href pattern — verify on first run
        {"name": "Perrett Laver", "url": "https://candidates.perrettlaver.com/vacancies/", "selector": "a[href*='/vacancies/']"},
    ],
    "weights": {
        "executive_bonus": 50,
        "permanent_signal": 25,
        "expertise_signal": 20,
        "geography_bonus": 20,
        "exclusion_penalty": -60,
    },
    "exec_titles": [
        "pro-vice-chancellor", "pvc", "registrar", "principal", "provost",
        "vice-principal", "chief executive", "ceo", "dean", "vice-chancellor",
        "director of education", "director of student", "director of academic",
        "director of quality", "director of learning", "director of teaching",
    ],
    "title_gate": [
        "director", "pvc", "dean", "ceo", "chief", "registrar",
        "head of", "principal", "provost", "vice-chancellor",
    ],
    "exclusion_terms": [
        "software", "nurse", "warehouse", "developer", "technician",
        "estates", "facilities", "catering", "porter", "security",
    ],
}

CHARITY_CONFIG = {
    "label": "Charity Leadership",
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "filters": {"minimum_score": 20},
    "sources": [
        {"name": "CharityJob CEO", "url": "https://www.charityjob.co.uk/chief-executive-officer-jobs", "selector": "h3 a"},
        {"name": "CharityJob Director", "url": "https://www.charityjob.co.uk/jobs?keywords=director+of+education+policy+programmes&category=director", "selector": "h3 a"},
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"},
        {"name": "NFP People", "url": "https://careers.nfp-people.co.uk/jobs/", "selector": "a[href*='/job/']"},
        {"name": "NFP Consulting", "url": "https://nfpconsulting.co.uk/jobs", "selector": ".job-title a"},
        {"name": "Harris Hill", "url": "https://www.harrishill.co.uk/jobs/?category=chief-executive", "selector": ".job-listing__title a"},
        {"name": "Harris Hill Director", "url": "https://www.harrishill.co.uk/jobs/?category=director", "selector": ".job-listing__title a"},
        {"name": "Third Sector Jobs", "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/", "selector": "h3 a"},
    ],
    "weights": {
        "executive_bonus": 50,
        "director_bonus": 30,
        "permanent_signal": 20,
        "expertise_signal": 15,
        "sector_fit_bonus": 15,
        "exclusion_penalty": -60,
    },
    "exec_titles": [
        "chief executive", "ceo", "executive director", "director general",
        "chief executive officer", "head of organisation", "managing director",
    ],
    "director_titles": [
        "director of education", "director of learning", "director of policy",
        "director of programmes", "director of partnerships", "director of impact",
        "director of strategy", "director of development", "director of governance",
        "director of quality", "head of education", "head of policy",
        "head of programmes", "head of learning", "head of partnerships",
    ],
    "title_gate": [
        "chief executive", "ceo", "executive director", "director general",
        "director of", "head of", "managing director", "chief operating",
    ],
    "exclusion_terms": [
        "software", "developer", "engineer", "technician", "warehouse",
        "nurse", "social worker", "counsellor", "therapist", "support worker",
        "community fundraiser", "events coordinator", "marketing officer",
        "communications officer", "fundraising manager",
    ],
}

SECTOR_BODIES_CONFIG = {
    "label": "Sector Bodies & Professional Associations",
    "db_path": "sector_bodies_jobs.db",
    "output_prefix": "sector_",
    "filters": {"minimum_score": 20},
    "sources": [
        {"name": "jobs.ac.uk Professional", "url": "https://www.jobs.ac.uk/search/director", "selector": ".j-search-result__title a"},
        {"name": "CharityJob Policy", "url": "https://www.charityjob.co.uk/jobs?keywords=director+policy+education+sector&category=policy-public-affairs", "selector": "h3 a"},
        {"name": "CharityJob Education", "url": "https://www.charityjob.co.uk/jobs?keywords=director+education+learning&category=education", "selector": "h3 a"},
        {"name": "NFP People", "url": "https://careers.nfp-people.co.uk/jobs/", "selector": "a[href*='/job/']"},
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"},
        {"name": "Guardian Jobs Education", "url": "https://jobs.theguardian.com/jobs/education/senior-executive/", "selector": ".js-job-title a"},
    ],
    "weights": {
        "executive_bonus": 50,
        "director_bonus": 35,
        "permanent_signal": 20,
        "expertise_signal": 20,
        "sector_fit_bonus": 20,
        "exclusion_penalty": -60,
    },
    "exec_titles": [
        "chief executive", "ceo", "executive director", "director general",
        "chief executive officer", "managing director", "registrar",
    ],
    "director_titles": [
        "director of education", "director of learning", "director of policy",
        "director of quality", "director of standards", "director of accreditation",
        "director of programmes", "director of partnerships", "director of impact",
        "director of strategy", "director of development", "director of governance",
        "director of assessment", "director of research", "director of membership",
        "head of education", "head of policy", "head of quality",
        "head of learning", "head of standards", "head of accreditation",
        "head of partnerships", "head of programmes",
    ],
    "title_gate": [
        "chief executive", "ceo", "director", "head of", "registrar",
        "managing director", "secretary general",
    ],
    "exclusion_terms": [
        "software", "developer", "engineer", "technician", "warehouse",
        "nurse", "social worker", "counsellor", "therapist", "support worker",
        "sales director", "commercial director", "finance director",
        "it director", "digital director", "hr director",
    ],
}

PROFILES = {"he": HE_CONFIG, "charity": CHARITY_CONFIG, "sector": SECTOR_BODIES_CONFIG}


# --- DESCRIPTION EXTRACTION ---
def extract_description(page_html: str) -> str:
    """
    Extract meaningful job description text from a detail page.
    Tries semantic/common job-content containers first, falls back to <body>.
    Strips nav, header, footer, and script noise before falling back.
    """
    # Parameter is named ``page_html`` (not ``html``) so it does not shadow the
    # standard-library ``html`` module imported at the top of this file.
    soup = BeautifulSoup(page_html, "lxml")

    # Remove boilerplate elements unconditionally
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()

    # Ordered list of containers most likely to hold job description content
    candidate_selectors = [
        "main",
        "article",
        '[class*="job-description"]',
        '[class*="job-detail"]',
        '[class*="vacancy-detail"]',
        '[class*="role-detail"]',
        '[id*="job-description"]',
        '[id*="job-detail"]',
        ".content",
        "#content",
    ]

    for sel in candidate_selectors:
        node = soup.select_one(sel)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 200:  # Ignore containers that matched but are nearly empty
                return text[:MAX_DESCRIPTION_CHARS]

    # Full-body fallback
    return soup.get_text(" ", strip=True)[:MAX_DESCRIPTION_CHARS]


# --- EMPLOYER EXTRACTION ---
def extract_employer(title: str, description: str, source_name: str) -> str:
    """
    Try to pull a real employer name from title/description.
    Falls back to source_name only if nothing found.
    Named sector bodies are matched as literals; institutions via capture groups.
    """
    # Literal matches for well-known sector bodies (no capture group needed)
    literal_names = [
        "Advance HE", "Jisc", "UCAS", "OfS", "QAA", "HESA",
    ]
    for name in literal_names:
        if name.lower() in title.lower() or name.lower() in description[:800].lower():
            return name

    # Capture-group patterns for institution names
    patterns = [
        r"(University of [A-Z][A-Za-z\s&']+?)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? University)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? College)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Institute)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Trust)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Charity)(?:\s*[,\|\-]|\s{2}|$)",
    ]
    for text in [title, description[:800]]:
        for p in patterns:
            match = re.search(p, text)
            if match:
                result = match.group(1).strip().rstrip(",|-").strip()
                if len(result) > 3:
                    return result

    return source_name


# --- SCORING ---
def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    full_text = f"{job.title} {job.description}".lower()
    job.score = 0.0
    job.match_reasons = []

    # 1. Exclusions first
    if any(t in full_text for t in config["exclusion_terms"]):
        job.score += w["exclusion_penalty"]
        job.match_reasons.append("Excluded")
        return job

    # 2. Executive title
    if any(t in job.title.lower() for t in config["exec_titles"]):
        job.score += w["executive_bonus"]
        job.match_reasons.append("Executive Level")
    elif w.get("director_bonus") and any(t in job.title.lower() for t in config.get("director_titles", [])):
        job.score += w["director_bonus"]
        job.match_reasons.append("Director Level")

    # 3. Permanent vs interim
    senior_match = "Executive Level" in job.match_reasons or "Director Level" in job.match_reasons
    # Use word-boundary regex so "non-permanent" / "non-substantive" don't trip
    # the +permanent_signal bonus via simple substring match.
    permanent_re = re.compile(r"(?<![\w-])(?:permanent|substantive)\b", re.IGNORECASE)
    if permanent_re.search(full_text):
        job.score += w["permanent_signal"]
        job.match_reasons.append("Permanent")
    elif any(t in full_text for t in ["interim", "fixed-term", "fixed term"]):
        if senior_match:
            job.match_reasons.append("Strategic Interim")
        else:
            job.score -= 15
            job.match_reasons.append("Short-term Contract")

    # 4. Sector expertise
    expertise_map = {
        "psf": "PSF",
        "ntfs": "NTFS",
        "fellowship": "Fellowship",
        "teaching excellence framework": "TEF",
        "tef": "TEF",
        "ref ": "REF",
        "accreditation": "Accreditation",
        "quality assurance": "Quality Assurance",
        "ai in education": "AI in Education",
        "artificial intelligence": "AI Signal",
        "benchmarking": "Benchmarking",
        "governance": "Governance",
    }
    matched_expertise = [label for key, label in expertise_map.items() if key in full_text]
    if matched_expertise:
        job.score += w["expertise_signal"]
        job.match_reasons.append(matched_expertise[0])  # Report the first match; score awarded once

    # 5. Geography (HE profile only)
    if w.get("geography_bonus"):
        if any(t in full_text for t in ["scotland", "edinburgh", "glasgow", "st andrews", "dundee", "aberdeen", "stirling", "remote", "hybrid"]):
            job.score += w["geography_bonus"]
            job.match_reasons.append("Geographic Match")

    # 6. Sector fit
    if w.get("sector_fit_bonus"):
        sector_terms = [
            "education charity", "educational charity", "learning charity",
            "social mobility", "widening participation", "access to education",
            "skills and employment", "edtech", "lifelong learning",
            "higher education", "further education", "policy", "advocacy",
            "leadership development", "professional development",
        ]
        if any(t in full_text for t in sector_terms):
            job.score += w["sector_fit_bonus"]
            job.match_reasons.append("Sector Fit")

    return job


# --- DATABASE ---
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.conn.close()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                fingerprint TEXT PRIMARY KEY,
                title TEXT,
                employer TEXT,
                url TEXT,
                description TEXT,
                score REAL,
                reasons TEXT,
                fetched_at TEXT,
                first_seen_at TEXT,
                status TEXT DEFAULT 'new'
            )
        """)
        cursor = self.conn.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in cursor.fetchall()]
        if "reasons" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN reasons TEXT")
        if "status" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
        self.conn.commit()

    def find_canonical(self, title: str, url: str) -> bool:
        # Dedupe by URL only. Title alone is unsafe — two genuinely different
        # roles called "Director of Education" at different employers must NOT
        # collapse into one record. ``title`` is kept in the signature for
        # future heuristics (e.g. fuzzy reposting detection).
        del title
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE url = ?", (url,)
        ).fetchone() is not None

    def upsert_job(self, job: Job):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO jobs (fingerprint, title, employer, url, description, score, reasons, fetched_at, first_seen_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
            ON CONFLICT(fingerprint) DO UPDATE SET
                score = excluded.score,
                reasons = excluded.reasons,
                fetched_at = excluded.fetched_at
        """, (
            job.fingerprint, job.title, job.employer, job.url,
            job.description, job.score, ", ".join(job.match_reasons),
            job.fetched_at, now
        ))
        self.conn.commit()

    def get_recent(self, hours: int, min_score: float) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute(
            "SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC LIMIT ?",
            (cutoff, min_score, REPORT_ROW_LIMIT)
        ).fetchall()


# --- HTTP WITH RETRY ---
RETRY_AFTER_CAP = 60.0  # seconds — never honour a Retry-After value above this


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header value (delta-seconds only).

    Returns ``None`` if the header is missing, blank, or not a non-negative
    number. HTTP-date form is intentionally not supported — the variance
    between server clock and runner clock is not worth the complexity here.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP)


async def fetch_with_retry(client: httpx.AsyncClient, url: str) -> Optional[httpx.Response]:
    """Fetch a URL with simple retry on transient errors (429, 503, network failures).

    Returns the response only if the status is 2xx. 4xx (other than 429) is treated as a
    permanent failure and returns None — the caller should not parse a 404 page as content.
    Honours the ``Retry-After`` header when present (capped at RETRY_AFTER_CAP).
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(url, timeout=TIMEOUT)
            if resp.status_code in (429, 503) and attempt < MAX_RETRIES:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if retry_after is not None:
                    wait = retry_after + random.uniform(0, 0.5)
                else:
                    # Backoff with jitter to avoid thundering-herd retries against rate limiters
                    wait = RETRY_BACKOFF * (attempt + 1) + random.uniform(0, 1.0)
                logger.warning(f"HTTP {resp.status_code} on {url} — retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            if 200 <= resp.status_code < 300:
                return resp
            logger.warning(f"HTTP {resp.status_code} on {url} — skipping")
            return None
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * (attempt + 1) + random.uniform(0, 1.0)
                logger.warning(f"Network error on {url}: {e} — retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Failed after {MAX_RETRIES + 1} attempts: {url}: {e}")
                return None
    return None


# --- REPORTING ---
def generate_html_report(rows: list, title: str, filename: str):
    status_colours = {
        "new": "#e3f2fd",
        "interested": "#e8f5e9",
        "applied": "#fff3e0",
        "rejected": "#fce4ec",
    }

    cards = []
    for r in rows:
        status = r["status"] or "new"
        status_bg = status_colours.get(status, "#f5f5f5")
        reasons_html = " ".join(
            f"<span style='background:#e3f2fd; padding:3px 8px; border-radius:4px; "
            f"margin-right:5px; border:1px solid #90caf9; font-size:0.8em; color:#0d47a1;'>"
            f"{html.escape(res.strip())}</span>"
            for res in (r["reasons"] or "").split(",") if res.strip()
        )

        url = r["url"] or ""
        # Only allow http(s) URLs in the View Opening link to prevent javascript: injection
        scheme = urlparse(url).scheme.lower()
        safe_url = html.escape(url, quote=True) if scheme in ("http", "https") else "#"

        full_description = r["description"] or ""
        description_excerpt = full_description[:400]
        # Only show the ellipsis if we actually truncated the description.
        ellipsis = "..." if len(full_description) > 400 else ""

        score_value = r["score"] if r["score"] is not None else 0
        score_display = (
            int(score_value) if float(score_value).is_integer() else round(score_value, 1)
        )

        cards.append(f"""
            <div style="border:1px solid #e0e0e0; padding:20px; margin-bottom:20px;
                        border-radius:12px; background:{status_bg};
                        box-shadow:0 2px 4px rgba(0,0,0,0.05); font-family:sans-serif;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <h3 style="margin:0 0 6px 0; color:#1a237e;">{html.escape(r['title'] or '')}</h3>
                    <span style="font-size:0.8em; background:#1a237e; color:white;
                                 padding:3px 8px; border-radius:4px; white-space:nowrap;">
                        Score: {score_display}
                    </span>
                </div>
                <p style="margin:0 0 10px 0;"><strong>{html.escape(r['employer'] or '')}</strong></p>
                <div style="margin-bottom:12px;">{reasons_html}</div>
                <p style="color:#424242; font-size:0.9em; line-height:1.5; margin-bottom:15px;">
                    {html.escape(description_excerpt)}{ellipsis}
                </p>
                <div style="display:flex; gap:10px; align-items:center;">
                    <a href="{safe_url}" target="_blank" rel="noopener noreferrer"
                       style="background:#1a237e; color:white; padding:10px 20px;
                              text-decoration:none; border-radius:6px; font-weight:bold;">
                        View Opening
                    </a>
                    <span style="font-size:0.8em; color:#666;">
                        First seen: {html.escape((r['first_seen_at'] or '')[:10])}
                        &nbsp;|&nbsp; Status: <strong>{html.escape(status)}</strong>
                    </span>
                </div>
            </div>""")

    content = "".join(cards) if cards else (
        "<p style='font-family:sans-serif;'>No opportunities met the suitability threshold.</p>"
    )
    safe_title = html.escape(title)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"""<html>
<head><meta charset="utf-8"><title>{safe_title}</title></head>
<body style='max-width:860px; margin:auto; padding:40px; background:#f8f9fa;'>
    <h2 style='font-family:sans-serif; color:#1a237e;
               border-bottom:2px solid #1a237e; padding-bottom:10px;'>
        {safe_title}
    </h2>
    <p style='font-family:sans-serif; color:#666; font-size:0.9em;'>
        Generated: {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}
        &nbsp;|&nbsp; {len(rows)} role(s) listed
    </p>
    {content}
</body></html>""")


# --- EXECUTION ---
async def process_profile(profile_key: str, weekly: bool):
    cfg = PROFILES[profile_key]

    with Database(cfg["db_path"]) as db:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True
        ) as client:
            for i, src in enumerate(cfg["sources"]):
                if i > 0:
                    await asyncio.sleep(2.0)
                try:
                    resp = await fetch_with_retry(client, src["url"])
                    if resp is None:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    for a in soup.select(src["selector"]):
                        raw_title = a.get_text(" ").strip()
                        href = a.get("href", "")
                        if not href or len(raw_title) < 10:
                            continue
                        url = urljoin(src["url"], href)

                        if db.find_canonical(raw_title, url):
                            continue

                        if not any(t in raw_title.lower() for t in cfg["title_gate"]):
                            continue

                        await asyncio.sleep(1.5)
                        det = await fetch_with_retry(client, url)
                        if det is None:
                            continue

                        job = Job(
                            source=src["name"],
                            title=raw_title,
                            employer=src["name"],
                            url=url,
                            fetched_at=datetime.now(timezone.utc).isoformat(),
                        )
                        job.description = extract_description(det.text)
                        job.employer = extract_employer(raw_title, job.description, src["name"])
                        job.fingerprint = hashlib.sha256(
                            f"{raw_title}{url}".encode()
                        ).hexdigest()

                        job = score_job(job, cfg)
                        if job.score >= cfg["filters"]["minimum_score"]:
                            db.upsert_job(job)
                            logger.info(f"Saved: [{job.score}] {job.title} @ {job.employer}")

                except Exception as e:
                    logger.error(f"Error scraping {src['name']}: {e}")

        window_hours = 168 if weekly else 24
        recent = db.get_recent(window_hours, cfg["filters"]["minimum_score"])
        fname = f"{cfg['output_prefix']}{'weekly_digest' if weekly else 'new_jobs_report'}.html"
        generate_html_report(
            recent,
            f"{'Weekly' if weekly else 'Daily'} {cfg['label']} Report",
            fname
        )
        logger.info(f"Report written: {fname} ({len(recent)} roles)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Job search agent for HE leadership and charity CEO roles")
    p.add_argument("--profile", choices=["he", "charity", "sector", "all"], default="he",
                   help="Which search profile to run (all runs every profile)")
    p.add_argument("--weekly", action="store_true",
                   help="Generate a 7-day digest instead of 24-hour update")
    args = p.parse_args()

    if args.profile == "all":
        for profile_key in ["he", "charity", "sector"]:
            asyncio.run(process_profile(profile_key, args.weekly))
    else:
        asyncio.run(process_profile(args.profile, args.weekly))
