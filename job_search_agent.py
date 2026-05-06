import asyncio
import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

# --- SYSTEM SETTINGS ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 30

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
    # Titles that qualify for executive_bonus — specific enough to avoid noise
    "exec_titles": [
        "pro-vice-chancellor", "pvc", "registrar", "principal", "provost",
        "vice-principal", "chief executive", "ceo", "dean", "vice-chancellor",
        "director of education", "director of student", "director of academic",
        "director of quality", "director of learning", "director of teaching",
    ],
    # Title gate for deciding whether to fetch detail page
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
        # CEO/executive roles — direct category pages
        {"name": "CharityJob CEO", "url": "https://www.charityjob.co.uk/chief-executive-officer-jobs", "selector": "h3 a"},
        {"name": "CharityJob Director", "url": "https://www.charityjob.co.uk/jobs?keywords=director+of+education+policy+programmes&category=director", "selector": "h3 a"},
        # Specialist charity exec recruiters
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"},
        {"name": "NFP People", "url": "https://careers.nfp-people.co.uk/jobs/", "selector": "a[href*='/job/']"},
        {"name": "NFP Consulting", "url": "https://nfpconsulting.co.uk/jobs", "selector": ".job-title a"},
        {"name": "Harris Hill", "url": "https://www.harrishill.co.uk/jobs/?category=chief-executive", "selector": ".job-listing__title a"},
        {"name": "Harris Hill Director", "url": "https://www.harrishill.co.uk/jobs/?category=director", "selector": ".job-listing__title a"},
        {"name": "Third Sector Jobs", "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/", "selector": "h3 a"},
    ],
    "weights": {
        "executive_bonus": 50,       # CEO/ED/Director General
        "director_bonus": 30,        # Director of Education/Policy/Programmes
        "permanent_signal": 20,
        "expertise_signal": 15,
        "sector_fit_bonus": 15,      # Education/social mobility/policy orgs
        "exclusion_penalty": -60,
    },
    "exec_titles": [
        "chief executive", "ceo", "executive director", "director general",
        "chief executive officer", "head of organisation", "managing director",
    ],
    # Broader director titles that suit Charles's background
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

# Professional associations, awarding bodies, sector agencies, social enterprises
SECTOR_BODIES_CONFIG = {
    "label": "Sector Bodies & Professional Associations",
    "db_path": "sector_bodies_jobs.db",
    "output_prefix": "sector_",
    "filters": {"minimum_score": 20},
    "sources": [
        # jobs.ac.uk catches many professional/sector body roles alongside HE
        {"name": "jobs.ac.uk Professional", "url": "https://www.jobs.ac.uk/search/director", "selector": ".j-search-result__title a"},
        # CharityJob also lists professional associations and awarding bodies
        {"name": "CharityJob Policy", "url": "https://www.charityjob.co.uk/jobs?keywords=director+policy+education+sector&category=policy-public-affairs", "selector": "h3 a"},
        {"name": "CharityJob Education", "url": "https://www.charityjob.co.uk/jobs?keywords=director+education+learning&category=education", "selector": "h3 a"},
        # NFP People and Prospectus both cover social enterprises and sector bodies
        {"name": "NFP People", "url": "https://careers.nfp-people.co.uk/jobs/", "selector": "a[href*='/job/']"},
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"},
        # Guardian Jobs covers NDPBs and arms-length bodies well
        {"name": "Guardian Jobs Education", "url": "https://jobs.theguardian.com/jobs/education/senior-executive/", "selector": ".js-job-title a"},
    ],
    "weights": {
        "executive_bonus": 50,       # CEO/ED/Director General of sector body
        "director_bonus": 35,        # Director-level at sector body — high relevance
        "permanent_signal": 20,
        "expertise_signal": 20,      # Higher weight — expertise is the whole proposition
        "sector_fit_bonus": 20,      # HE/FE/skills/policy organisations
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


# --- EMPLOYER EXTRACTION ---
def extract_employer(title: str, description: str, source_name: str) -> str:
    """
    Try to pull a real employer name from title/description.
    Falls back to source_name only if nothing found.
    """
    patterns = [
        r"(University of [A-Z][A-Za-z\s&']+?)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? University)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? College)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Institute)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Trust)(?:\s*[,\|\-]|\s{2}|$)",
        r"([A-Z][A-Za-z\s&']+? Charity)(?:\s*[,\|\-]|\s{2}|$)",
        r"Advance HE",
        r"Jisc",
        r"UCAS",
        r"OfS",
        r"QAA",
        r"HESA",
    ]
    # Search title first, then first 800 chars of description
    for text in [title, description[:800]]:
        for p in patterns:
            match = re.search(p, text)
            if match:
                result = match.group(0).strip().rstrip(",|-").strip()
                if len(result) > 3:
                    return result
    return source_name


# --- SCORING ---
def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    full_text = f"{job.title} {job.description}".lower()
    job.score = 0.0
    job.match_reasons = []

    # 1. Exclusions first — avoids wasting score on irrelevant roles
    if any(t in full_text for t in config["exclusion_terms"]):
        job.score += w["exclusion_penalty"]
        job.match_reasons.append("Excluded")
        return job  # No point scoring further

    # 2. Executive title
    if any(t in job.title.lower() for t in config["exec_titles"]):
        job.score += w["executive_bonus"]
        job.match_reasons.append("Executive Level")
    # Director-level titles (charity/sector profiles only)
    elif w.get("director_bonus") and any(t in job.title.lower() for t in config.get("director_titles", [])):
        job.score += w["director_bonus"]
        job.match_reasons.append("Director Level")

    # 3. Permanent vs interim
    if "permanent" in full_text or "substantive" in full_text:
        job.score += w["permanent_signal"]
        job.match_reasons.append("Permanent")
    elif any(t in full_text for t in ["interim", "fixed-term", "fixed term"]):
        # Only penalise if not already executive-level
        if "Executive Level" not in job.match_reasons:
            job.score -= 15
            job.match_reasons.append("Short-term Contract")
        else:
            job.match_reasons.append("Strategic Interim")

    # 4. Sector expertise relevant to Charles's background
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
    for key, label in expertise_map.items():
        if key in full_text:
            job.score += w["expertise_signal"]
            job.match_reasons.append(label)
            break  # Only award once to avoid stacking

    # 5. Geography — Scotland / remote (HE profile only; charity config omits this weight)
    if w.get("geography_bonus"):
        if any(t in full_text for t in ["scotland", "edinburgh", "glasgow", "st andrews", "dundee", "aberdeen", "stirling", "remote", "hybrid"]):
            job.score += w["geography_bonus"]
            job.match_reasons.append("Geographic Match")

    # 6. Sector fit — education/policy charities most relevant to Charles's background
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
        # Non-destructive migrations
        cursor = self.conn.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in cursor.fetchall()]
        if "reasons" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN reasons TEXT")
        if "status" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
        self.conn.commit()

    def find_canonical(self, title: str, url: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE title = ? OR url = ?", (title, url)
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
                -- first_seen_at deliberately NOT updated to preserve original discovery date
        """, (
            job.fingerprint, job.title, job.employer, job.url,
            job.description, job.score, ", ".join(job.match_reasons),
            job.fetched_at, now
        ))
        self.conn.commit()

    def get_recent(self, hours: int, min_score: float):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute(
            "SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC",
            (cutoff, min_score)
        ).fetchall()


# --- REPORTING ---
def generate_html_report(rows, title: str, filename: str):
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
            f"{res.strip()}</span>"
            for res in (r["reasons"] or "").split(",") if res.strip()
        )
        cards.append(f"""
            <div style="border:1px solid #e0e0e0; padding:20px; margin-bottom:20px;
                        border-radius:12px; background:{status_bg};
                        box-shadow:0 2px 4px rgba(0,0,0,0.05); font-family:sans-serif;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <h3 style="margin:0 0 6px 0; color:#1a237e;">{r['title']}</h3>
                    <span style="font-size:0.8em; background:#1a237e; color:white;
                                 padding:3px 8px; border-radius:4px; white-space:nowrap;">
                        Score: {r['score']}
                    </span>
                </div>
                <p style="margin:0 0 10px 0;"><strong>{r['employer']}</strong></p>
                <div style="margin-bottom:12px;">{reasons_html}</div>
                <p style="color:#424242; font-size:0.9em; line-height:1.5; margin-bottom:15px;">
                    {(r['description'] or '')[:400]}...
                </p>
                <div style="display:flex; gap:10px; align-items:center;">
                    <a href="{r['url']}" target="_blank"
                       style="background:#1a237e; color:white; padding:10px 20px;
                              text-decoration:none; border-radius:6px; font-weight:bold;">
                        View Opening
                    </a>
                    <span style="font-size:0.8em; color:#666;">
                        First seen: {(r['first_seen_at'] or '')[:10]}
                        &nbsp;|&nbsp; Status: <strong>{status}</strong>
                    </span>
                </div>
            </div>""")

    content = "".join(cards) if cards else (
        "<p style='font-family:sans-serif;'>No opportunities met the suitability threshold.</p>"
    )
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"""<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body style='max-width:860px; margin:auto; padding:40px; background:#f8f9fa;'>
    <h2 style='font-family:sans-serif; color:#1a237e;
               border-bottom:2px solid #1a237e; padding-bottom:10px;'>
        {title}
    </h2>
    <p style='font-family:sans-serif; color:#666; font-size:0.9em;'>
        Generated: {datetime.now().strftime('%d %B %Y %H:%M')}
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
                # Polite delay between source pages
                if i > 0:
                    await asyncio.sleep(2.0)
                try:
                    resp = await client.get(src["url"], timeout=TIMEOUT)
                    soup = BeautifulSoup(resp.text, "html.parser")

                    for a in soup.select(src["selector"]):
                        raw_title = a.get_text(" ").strip()
                        href = a.get("href", "")
                        if not href or len(raw_title) < 10:
                            continue
                        url = urljoin(src["url"], href)

                        if db.find_canonical(raw_title, url):
                            continue

                        # Title gate — only fetch detail pages for plausibly senior roles
                        if not any(t in raw_title.lower() for t in cfg["title_gate"]):
                            continue

                        await asyncio.sleep(1.5)
                        try:
                            det = await client.get(url, timeout=TIMEOUT)
                        except Exception as e:
                            logger.warning(f"Detail fetch failed for {url}: {e}")
                            continue

                        job = Job(
                            source=src["name"],
                            title=raw_title,
                            employer=src["name"],  # placeholder, overwritten below
                            url=url,
                            fetched_at=datetime.now(timezone.utc).isoformat(),
                        )
                        job.description = BeautifulSoup(
                            det.text, "html.parser"
                        ).get_text(" ", strip=True)[:5000]
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
