import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

# --- SYSTEM SETTINGS ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 30 
PRESCORE_THRESHOLD = 8

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("job_search.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

@dataclass
class Job:
    source: str
    source_kind: str
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
    "label": "Higher Education",
    "db_path": "jobs_he.db",
    "output_prefix": "",
    "filters": {"minimum_score": 25},
    "sources": [
        {"name": "jobs.ac.uk Senior Management", "kind": "generic_html", "url": "https://www.jobs.ac.uk/search/senior-management", "selector": ".j-search-result__title a"},
        {"name": "THE UniJobs UK", "kind": "generic_html", "url": "https://www.timeshighereducation.com/unijobs/listings/united-kingdom/", "selector": ".job-results__title a"},
        {"name": "ULA - University Leadership", "kind": "generic_html", "url": "https://www.ulassociates.co.uk/jobs/"},
        {"name": "Peridot Partners HE", "kind": "generic_html", "url": "https://www.peridotpartners.co.uk/jobs/"},
        {"name": "Odgers Berndtson HE", "kind": "generic_html", "url": "https://www.odgersberndtson.com/en-gb/opportunities"},
    ],
    "weights": {
        "senior_auto_pass": 50, # For CEO/Director titles
        "expertise_signal": 20, # PSF, Fellowship, NTFS
        "consultancy_signal": 15, # Interim, Transformation
        "salary_bonus": 10, # £75k+
        "flexibility_bonus": 5, # Hybrid/Remote
    }
}

CHARITY_CONFIG = {
    "label": "Charity CEO",
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "filters": {"minimum_score": 20},
    "sources": [
        {"name": "CharityJob", "kind": "generic_html", "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive"},
        {"name": "Third Sector Jobs", "kind": "generic_html", "url": "https://jobs.thirdsector.co.uk/jobs/chief-executive/"},
        {"name": "Prospectus CEO", "kind": "generic_html", "url": "https://www.prospectus.co.uk/jobs/"},
    ],
    "weights": {
        "senior_auto_pass": 50,
        "governance_signal": 15,
        "strategy_signal": 10,
    }
}

PROFILES = {"he": HE_CONFIG, "charity": CHARITY_CONFIG}

# --- TOOLS ---
def extract_salary(text: str) -> Optional[int]:
    match = re.search(r"£(\d{1,3}(?:,\d{3})+)", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None

def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    full_text = f"{job.title} {job.description}".lower()
    job.match_reasons = []

    # 1. Seniority Auto-Pass
    if any(t in job.title.lower() for t in ["ceo", "chief executive", "director of educational excellence"]):
        job.score += w["senior_auto_pass"]
        job.match_reasons.append("High Seniority Title")

    # 2. Expertise & Frameworks
    expertise_terms = {
        "professional standards framework": "PSF Expertise",
        "psf": "PSF Expertise",
        "fellowship": "Fellowship Program",
        "ntfs": "NTFS/Teaching Excellence",
        "national teaching fellowship": "NTFS/Teaching Excellence",
        "accreditation": "Accreditation Work"
    }
    for term, reason in expertise_terms.items():
        if term in full_text:
            job.score += w.get("expertise_signal", 15)
            if reason not in job.match_reasons: job.match_reasons.append(reason)

    # 3. Consultancy & Interim Leads
    for term in ["interim", "contract", "consultancy", "transformation"]:
        if term in full_text:
            job.score += w.get("consultancy_signal", 10)
            job.match_reasons.append("Consultancy/Interim Opportunity")
            break

    # 4. Salary Detection
    salary = extract_salary(full_text)
    if salary and salary >= 75000:
        job.score += w.get("salary_bonus", 10)
        job.match_reasons.append(f"High Value (£{salary:,})")
    elif "competitive" in full_text or "grade 10" in full_text:
        job.score += 5
        job.match_reasons.append("Competitive/Senior Pay Grade")

    # 5. Flexibility
    if any(t in full_text for t in ["remote", "hybrid", "flexible working"]):
        job.score += w.get("flexibility_bonus", 5)
        job.match_reasons.append("Flexible/Hybrid")

    return job

# --- DATABASE & REPORTING ---
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE IF NOT EXISTS jobs (fingerprint TEXT PRIMARY KEY, title TEXT, employer TEXT, url TEXT, description TEXT, score REAL, reasons TEXT, fetched_at TEXT, first_seen_at TEXT)")

    def find_canonical(self, title: str, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE title = ? OR url = ?", (title, url)).fetchone() is not None

    def upsert_job(self, job: Job):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET score=excluded.score", 
                         (job.fingerprint, job.title, job.employer, job.url, job.description, job.score, ", ".join(job.match_reasons), job.fetched_at, now))
        self.conn.commit()

    def get_recent(self, hours: int, min_score: float):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC", (cutoff, min_score)).fetchall()

def generate_html_report(rows: List[sqlite3.Row], title: str, filename: str):
    cards = []
    for r in rows:
        reasons = [f"<span style='background:#e1f5fe; padding:2px 6px; border-radius:3px; margin-right:5px;'>{res}</span>" for res in r['reasons'].split(", ")]
        cards.append(f"""
            <div style="border:1px solid #ddd; padding:15px; margin-bottom:15px; border-radius:8px; font-family: Arial, sans-serif;">
                <h3 style="margin:0 0 10px 0; color: #003366;">{r['title']}</h3>
                <p><strong>{r['employer']}</strong> | Score: {r['score']}</p>
                <div style="margin-bottom:10px; font-size: 0.85em;">{' '.join(reasons)}</div>
                <p style="color: #555;">{r['description'][:300]}...</p>
                <a href="{r['url']}" style="background:#0056b3; color:white; padding:8px 15px; text-decoration:none; border-radius:5px; display:inline-block;">View Opportunity</a>
            </div>""")
    
    body = "".join(cards) if cards else "<p>No matches met the suitability threshold in this cycle.</p>"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"<html><body style='max-width:800px; margin:auto; padding:20px;'><h2>{title}</h2>{body}</body></html>")

# --- CORE LOGIC ---
async def process_profile(profile_key: str, weekly: bool):
    cfg = PROFILES[profile_key]
    db = Database(cfg["db_path"])
    
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        for src in cfg["sources"]:
            try:
                resp = await client.get(src["url"], timeout=TIMEOUT)
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.select(src.get("selector", "a[href]")):
                    title, url = a.get_text(" ").strip(), urljoin(src["url"], a.get("href", ""))
                    if len(title) < 10 or db.find_canonical(title, url): continue
                    
                    # Stage 1: Fast Prescore
                    if any(t in title.lower() for t in ["director", "head", "dean", "ceo", "chief"]):
                        await asyncio.sleep(1)
                        detail_resp = await client.get(url, timeout=TIMEOUT)
                        job = Job(source=src["name"], source_kind="html", title=title, employer=src["name"], url=url, fetched_at=datetime.now(timezone.utc).isoformat())
                        job.description = BeautifulSoup(detail_resp.text, "html.parser").get_text(" ", strip=True)[:4000]
                        job.fingerprint = hashlib.sha256(f"{title}{url}".encode()).hexdigest()
                        
                        job = score_job(job, cfg)
                        if job.score >= cfg["filters"]["minimum_score"]:
                            db.upsert_job(job)
            except Exception as e:
                logger.error(f"Error on {src['name']}: {e}")

    recent_jobs = db.get_recent(168 if weekly else 24, cfg["filters"]["minimum_score"])
    fname = f"{cfg['output_prefix']}{'weekly_digest' if weekly else 'new_jobs_report'}.html"
    generate_html_report(recent_jobs, f"{'Weekly' if weekly else 'Daily'} {cfg['label']} Report", fname)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["he", "charity", "both"], default="he")
    parser.add_argument("--weekly", action="store_true")
    args = parser.parse_args()

    if args.profile == "both":
        asyncio.run(process_profile("he", args.weekly))
        asyncio.run(process_profile("charity", args.weekly))
    else:
        asyncio.run(process_profile(args.profile, args.weekly))
