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
    "label": "Charity CEO & Board",
    "db_path": "charity_jobs.db",
    "output_prefix": "charity_",
    "filters": {"minimum_score": 20},
    "sources": [
        {"name": "CharityJob", "url": "https://www.charityjob.co.uk/jobs?keywords=chief+executive+ceo&category=chief-executive", "selector": "h3 a"},
        {"name": "Prospectus", "url": "https://www.prospectus.co.uk/jobs/", "selector": ".job-title a"},
    ],
    "weights": {
        "executive_bonus": 50,
        "permanent_signal": 20,
        "expertise_signal": 10,
        "geography_bonus": 15,
        "exclusion_penalty": -60,
    },
    "exec_titles": [
        "chief executive", "ceo", "executive director", "chief operating officer",
        "director general",
    ],
    "title_gate": ["chief", "ceo", "executive director", "director general"],
    "exclusion_terms": ["software", "nurse", "warehouse", "developer", "technician"],
}

PROFILES = {"he": HE_CONFIG, "charity": CHARITY_CONFIG}


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

    # 2. Executive title — specific list to avoid "Director of IT" false positives
    if any(t in job.title.lower() for t in config["exec_titles"]):
        job.score += w["executive_bonus"]
        job.match_reasons.append("Executive Level")

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

    # 5. Geography — Scotland / remote
    if any(t in full_text for t in ["scotland", "edinburgh", "glasgow", "st andrews", "dundee", "aberdeen", "stirling", "remote", "hybrid"]):
        job.score += w["geography_bonus"]
        job.match_reasons.append("Geographic Match")

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
    p.add_argument("--profile", choices=["he", "charity", "both"], default="he",
                   help="Which search profile to run")
    p.add_argument("--weekly", action="store_true",
                   help="Generate a 7-day digest instead of 24-hour update")
    args = p.parse_args()

    if args.profile == "both":
        asyncio.run(process_profile("he", args.weekly))
        asyncio.run(process_profile("charity", args.weekly))
    else:
        asyncio.run(process_profile(args.profile, args.weekly))
