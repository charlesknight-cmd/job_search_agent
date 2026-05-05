import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

# --- SYSTEM SETTINGS ---
# Fix for 403 Forbidden: Mimics a standard Chrome browser[cite: 1]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Fix for Timeouts: 30 seconds for slow institutional servers[cite: 1]
TIMEOUT = 30 
PRESCORE_THRESHOLD = 8

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("job_search.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

@dataclass
class Source:
    name: str
    kind: str 
    url: str
    selector: Optional[str] = None

@dataclass
class Job:
    source: str
    source_kind: str
    title: str
    employer: str
    location: str
    url: str
    description: str = ""
    salary_text: str = ""
    remote_status: str = ""
    score: float = 0.0
    matched_terms: str = ""
    fetched_at: str = ""
    fingerprint: str = ""

# --- SEARCH PROFILES (HE & CHARITY)[cite: 1] ---
HE_CONFIG = {
    "label": "Higher Education",
    "db_path": "jobs_he.db",
    "output_prefix": "",
    "sources": [
        {"name": "jobs.ac.uk Senior Management", "kind": "generic_html", "url": "https://www.jobs.ac.uk/search/senior-management", "selector": ".j-search-result__title a"},
        {"name": "THE UniJobs UK", "kind": "generic_html", "url": "https://www.timeshighereducation.com/unijobs/listings/united-kingdom/", "selector": ".job-results__title a"},
        {"name": "University of Oxford Careers", "kind": "generic_html", "url": "https://www.jobs.ox.ac.uk/"},
        {"name": "ULA - University Leadership", "kind": "generic_html", "url": "https://www.ulassociates.co.uk/jobs/"},
        {"name": "Peridot Partners HE", "kind": "generic_html", "url": "https://www.peridotpartners.co.uk/jobs/"},
        {"name": "Odgers Berndtson HE", "kind": "generic_html", "url": "https://www.odgersberndtson.com/en-gb/opportunities"},
    ],
    "filters": {
        "include_keywords": ["education", "higher education", "student success", "dean", "director", "governance", "interim", "transformation"],
        "exclude_keywords": ["software engineer", "nurse", "warehouse"],
        "minimum_score": 25,
    },
    "weights": {
        "senior_title": 20, "he_signal": 18, "education_signal": 15, "strategy_signal": 12,
        "uk_signal": 8, "remote_hybrid_signal": 6, "salary_signal": 5, "exclude_penalty": -30
    }
}

CHARITY_CONFIG = {
    "label": "Charity CEO",
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "sources": [
        {"name": "CharityJob", "kind": "generic_html", "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive"},
        {"name": "Third Sector Jobs", "kind": "generic_html", "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/"},
        {"name": "Prospectus CEO", "kind": "generic_html", "url": "https://www.prospectus.co.uk/jobs/"},
        {"name": "Charity People", "kind": "generic_html", "url": "https://www.charitypeople.co.uk/jobs"},
        {"name": "Board Appointments UK", "kind": "generic_html", "url": "https://boardappointments.co.uk/vacancies/"},
    ],
    "filters": {
        "include_keywords": ["chief executive", "ceo", "executive director", "charity", "impact", "trustee"],
        "exclude_keywords": ["software engineer", "nurse"],
        "minimum_score": 20,
    },
    "weights": {
        "senior_title": 25, "sector_signal": 20, "governance_signal": 12, "strategy_signal": 10,
        "uk_signal": 8, "remote_hybrid_signal": 6, "salary_signal": 5, "exclude_penalty": -30
    }
}

PROFILES = {"he": HE_CONFIG, "charity": CHARITY_CONFIG}

# --- DATABASE ENGINE ---
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                fingerprint TEXT PRIMARY KEY, source TEXT, title TEXT, employer TEXT, 
                url TEXT, description TEXT, score REAL, matched_terms TEXT, 
                fetched_at TEXT, first_seen_at TEXT
            )
        """)
        self.conn.commit()

    def find_canonical(self, title: str, employer: str) -> Optional[str]:
        cur = self.conn.execute("SELECT fingerprint FROM jobs WHERE title = ? AND employer = ?", (title, employer))
        row = cur.fetchone()
        return row[0] if row else None

    def upsert_job(self, job: Job, first_seen_at: str):
        self.conn.execute("""
            INSERT INTO jobs (fingerprint, source, title, employer, url, description, score, matched_terms, fetched_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET score=excluded.score, fetched_at=excluded.fetched_at
        """, (job.fingerprint, job.source, job.title, job.employer, job.url, job.description, job.score, job.matched_terms, job.fetched_at, first_seen_at))
        self.conn.commit()

    def get_new_jobs(self, hours: int, min_score: float) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC", (cutoff, min_score)).fetchall()

    def get_weekly_top(self, days: int, min_score: float) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return self.conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC LIMIT 10", (cutoff, min_score)).fetchall()

    def close(self): self.conn.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

# --- SCORING & SCRAPING ---
def prescore_job(job: Job, config: Dict) -> float:
    w = config["weights"]
    text = f"{job.title} {job.employer}".lower()
    score = 0.0
    if any(t in text for t in ["director", "ceo", "head of", "dean"]): score += w["senior_title"]
    return score

def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    all_t = f"{job.title} {job.description} {job.employer}".lower()
    score, matched = 0.0, []
    for term, pts, lbl in [("director", w["senior_title"], "senior"), ("university", w.get("he_signal", 0), "he")]:
        if term in all_t: score += pts; matched.append(lbl)
    job.score, job.matched_terms = round(score, 1), ", ".join(matched)
    return job

async def fetch_job_details(client: httpx.AsyncClient, job: Job) -> Job:
    try:
        # Polite delay to prevent rate-limiting[cite: 1]
        await asyncio.sleep(1.5) 
        r = await client.get(job.url, timeout=TIMEOUT)
        job.description = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:3000]
    except Exception as e:
        logger.error(f"Failed details for {job.url}: {e}")
    return job

# --- REPORTING ---
def generate_html_report(rows: List[sqlite3.Row], title: str, filename: str):
    cards = []
    for r in rows:
        cards.append(f"""
            <div style="border:1px solid #ddd; padding:15px; margin-bottom:10px; border-radius:5px; font-family: sans-serif;">
                <h3 style="margin-top:0; color: #003366;">{r['title']}</h3>
                <p><strong>{r['employer']}</strong> | Score: {r['score']}</p>
                <p style="color: #444;">{r['description'][:250]}...</p>
                <a href="{r['url']}" style="display: inline-block; padding: 8px 12px; background-color: #0056b3; color: white; text-decoration: none; border-radius: 4px;">View Opening</a>
            </div>""")
    
    html_content = "".join(cards) if cards else "<p>No new highly-relevant jobs discovered in this cycle.</p>"
    html = f"<html><body style='padding: 20px;'><h1>{title}</h1>{html_content}</body></html>"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)

# --- RUNNER ---
async def process_all(profile_name: str, weekly_mode: bool):
    config = PROFILES[profile_name]
    logger.info(f"Running {config['label']} (Weekly={weekly_mode})")
    
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        with Database(config["db_path"]) as db:
            for src_data in config["sources"]:
                src = Source(**src_data)
                try:
                    r = await client.get(src.url, timeout=TIMEOUT)
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.select(src.selector or "a[href]"):
                        title = a.get_text(" ").strip()
                        href = a.get("href", "")
                        if href and len(title) > 5:
                            url = urljoin(src.url, href)
                            job = Job(source=src.name, source_kind="html", title=title, employer=src.name, location="UK", url=url, fetched_at=datetime.now(timezone.utc).isoformat())
                            job.fingerprint = hashlib.sha256(f"{title}{url}".encode()).hexdigest()
                            
                            # Check if already seen[cite: 1, 2]
                            if db.find_canonical(title, src.name):
                                continue

                            if prescore_job(job, config) >= PRESCORE_THRESHOLD:
                                job = await fetch_job_details(client, job)
                                job = score_job(job, config)
                                if job.score >= config["filters"]["minimum_score"]:
                                    db.upsert_job(job, datetime.now(timezone.utc).isoformat())
                except Exception as e:
                    logger.error(f"Error scraping {src.name}: {e}")

            if weekly_mode:
                jobs = db.get_weekly_top(7, config["filters"]["minimum_score"])
                fname = f"{config['output_prefix']}weekly_digest.html"
                generate_html_report(jobs, f"Weekly {config['label']} Digest", fname)
            else:
                jobs = db.get_new_jobs(24, config["filters"]["minimum_score"])
                fname = f"{config['output_prefix']}new_jobs_report.html"
                generate_html_report(jobs, f"Daily {config['label']} Update", fname)

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    # Supports "both" for the automated workflow
    p.add_argument("--profile", choices=["he", "charity", "both"], default="he")
    p.add_argument("--weekly", action="store_true")
    args = p.parse_args()
    
    if args.profile == "both":
        asyncio.run(process_all("he", args.weekly))
        asyncio.run(process_all("charity", args.weekly))
    else:
        asyncio.run(process_all(args.profile, args.weekly))
