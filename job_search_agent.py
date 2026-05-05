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
    "filters": {"minimum_score": 30},
    "sources": [
        {"name": "jobs.ac.uk", "url": "https://www.jobs.ac.uk/search/senior-management", "selector": ".j-search-result__title a"},
        {"name": "THE UniJobs", "url": "https://www.timeshighereducation.com/unijobs/listings/united-kingdom/", "selector": ".job-results__title a"},
        {"name": "Peridot Partners", "url": "https://www.peridotpartners.co.uk/job-role/education-executive-roles/", "selector": ".card-title a"}
    ],
    "weights": {
        "senior_leadership_bonus": 45, # PVC, Registrar, CEO
        "permanent_signal": 25, # Stability marker
        "expertise_signal": 20, # PSF, Fellowship, NTFS
        "scotland_remote_bonus": 15, # Geography match
        "high_value_salary": 15, # £85k+
        "exclusion_penalty": -50 # Irrelevant roles
    }
}

CHARITY_CONFIG = {
    "label": "Charity CEO & Board",
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "filters": {"minimum_score": 25},
    "sources": [
        {"name": "CharityJob", "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive", "selector": "h3 a"},
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"}
    ],
    "weights": {
        "senior_leadership_bonus": 50,
        "permanent_signal": 20
    }
}

PROFILES = {"he": HE_CONFIG, "charity": CHARITY_CONFIG}

# --- TOOLS ---
def extract_employer(title: str, description: str, source_name: str) -> str:
    """Attempts to find the actual institution name"""
    patterns = [
        r"(University of [A-Za-z\s]+)",
        r"([A-Za-z\s]+ University)",
        r"([A-Za-z\s]+ College)",
        r"([A-Za-z\s]+ Trust)",
        r"([A-Za-z\s]+ Council)"
    ]
    for p in patterns:
        match = re.search(p, title) or re.search(p, description[:500])
        if match: return match.group(1).strip()
    return source_name

def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    full_text = f"{job.title} {job.description}".lower()
    job.match_reasons = []

    # 1. Leadership & Seniority
    leadership = ["pro-vice-chancellor", "pvc", "registrar", "chief executive", "ceo", "director of", "dean"]
    if any(t in job.title.lower() for t in leadership):
        job.score += w["senior_leadership_bonus"]
        job.match_reasons.append("Executive Leadership")

    # 2. Stability vs High-Level Interim
    if "permanent" in full_text or "substantive" in full_text:
        job.score += w.get("permanent_signal", 25)
        job.match_reasons.append("Permanent")
    elif any(t in full_text for t in ["interim", "fixed-term"]):
        # Only penalize if it's NOT a high-level role
        if job.score < w["senior_leadership_bonus"]:
            job.score -= 20
            job.match_reasons.append("Temporary (Low Priority)")
        else:
            job.match_reasons.append("Strategic Interim")

    # 3. Advance HE Expertise
    expertise = {"psf": "PSF", "ntfs": "NTFS", "fellowship": "Fellowship", "standards framework": "Frameworks"}
    for key, label in expertise.items():
        if key in full_text:
            job.score += w["expertise_signal"]
            job.match_reasons.append(label)

    # 4. Geography (Scotland/Remote)
    if any(t in full_text for t in ["scotland", "edinburgh", "glasgow", "st andrews", "remote", "hybrid"]):
        job.score += w.get("scotland_remote_bonus", 15)
        job.match_reasons.append("Location Match")

    # 5. Exclusions
    if any(t in full_text for t in ["software", "nurse", "warehouse", "developer", "technician"]):
        job.score += w["exclusion_penalty"]
        job.match_reasons.append("Irrelevant Sector")

    return job

# --- DATABASE ENGINE (NON-DESTRUCTIVE) ---
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                fingerprint TEXT PRIMARY KEY, title TEXT, employer TEXT, 
                url TEXT, description TEXT, score REAL, reasons TEXT, 
                fetched_at TEXT, first_seen_at TEXT
            )
        """)
        # Ensure 'reasons' column exists without dropping table
        cursor = self.conn.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in cursor.fetchall()]
        if "reasons" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN reasons TEXT")
        self.conn.commit()

    def find_canonical(self, title: str, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE title = ? OR url = ?", (title, url)).fetchone() is not None

    def upsert_job(self, job: Job):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO jobs (fingerprint, title, employer, url, description, score, reasons, fetched_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET score=excluded.score, reasons=excluded.reasons
        """, (job.fingerprint, job.title, job.employer, job.url, job.description, job.score, ", ".join(job.match_reasons), job.fetched_at, now))
        self.conn.commit()

    def get_recent(self, hours: int, min_score: float):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ? AND score >= ? ORDER BY score DESC", (cutoff, min_score)).fetchall()

def generate_html_report(rows, title, filename):
    cards = []
    for r in rows:
        reasons = [f"<span style='background:#e8f5e9; padding:2px 6px; border-radius:3px; margin-right:5px; border:1px solid #81c784; font-size:0.8em;'>{res}</span>" for res in (r['reasons'] or "").split(", ")]
        cards.append(f"""
            <div style="border:1px solid #ccc; padding:20px; margin-bottom:20px; border-radius:10px; background:white;">
                <h3 style="margin:0 0 10px 0; color:#1a237e;">{r['title']}</h3>
                <p><strong>{r['employer']}</strong> | Score: {r['score']}</p>
                <div style="margin-bottom:10px;">{' '.join(reasons)}</div>
                <p style="color:#666; font-size:0.9em;">{r['description'][:350]}...</p>
                <a href="{r['url']}" style="background:#1a237e; color:white; padding:10px 15px; text-decoration:none; border-radius:5px; display:inline-block;">View Detail</a>
            </div>""")
    
    content = "".join(cards) if cards else "<p>No high-priority leadership roles found in this cycle.</p>"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"<html><body style='max-width:800px; margin:auto; padding:40px; background:#fafafa; font-family:sans-serif;'><h2>{title}</h2>{content}</body></html>")

# --- RUNNER ---
async def process_profile(profile_key: str, weekly: bool):
    cfg = PROFILES[profile_key]
    db = Database(cfg["db_path"])
    
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        for src in cfg["sources"]:
            try:
                resp = await client.get(src["url"], timeout=TIMEOUT)
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.select(src["selector"]):
                    title, url = a.get_text(" ").strip(), urljoin(src["url"], a.get("href", ""))
                    if len(title) < 10 or db.find_canonical(title, url): continue
                    
                    # Prescore Title check
                    if any(t in title.lower() for t in ["director", "pvc", "dean", "ceo", "chief", "registrar", "head"]):
                        await asyncio.sleep(1.5)
                        det = await client.get(url, timeout=TIMEOUT)
                        job = Job(source=src["name"], title=title, employer=src["name"], url=url, fetched_at=datetime.now(timezone.utc).isoformat())
                        job.description = BeautifulSoup(det.text, "html.parser").get_text(" ", strip=True)[:4500]
                        job.employer = extract_employer(title, job.description, src["name"])
                        job.fingerprint = hashlib.sha256(f"{title}{url}".encode()).hexdigest()
                        
                        job = score_job(job, cfg)
                        if job.score >= cfg["filters"]["minimum_score"]:
                            db.upsert_job(job)
            except Exception as e:
                logger.error(f"Error on {src['name']}: {e}")

    recent = db.get_recent(168 if weekly else 24, cfg["filters"]["minimum_score"])
    fname = f"{cfg['output_prefix']}{'weekly_digest' if weekly else 'new_jobs_report'}.html"
    generate_html_report(recent, f"{'Weekly' if weekly else 'Daily'} Leadership Update", fname)

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--profile", choices=["he", "charity", "both"], default="he")
    p.add_argument("--weekly", action="store_true")
    args = p.parse_args()
    if args.profile == "both":
        asyncio.run(process_profile("he", args.weekly))
        asyncio.run(process_profile("charity", args.weekly))
    else:
        asyncio.run(process_profile(args.profile, args.weekly))
