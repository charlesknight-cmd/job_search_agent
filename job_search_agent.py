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

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("job_search.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Fix for 403 Forbidden: Mimics a standard Chrome browser
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 30  # Increased for slow institutional servers
PRESCORE_THRESHOLD = 8

@dataclass
class Source:
    name: str
    kind: str  # generic_html | greenhouse | ashby | lever
    url: str
    selector: Optional[str] = None  # NEW: Smart selector to target specific job links
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

# --- LLM VERIFICATION STUB ---
async def verify_job_relevance(job: Job) -> bool:
    """
    Placeholder for an LLM call to verify if the job matches 
    high-level consultancy or leadership criteria.
    """
    # Example logic: if len(job.description) > 500: return True
    return True

class AsyncBaseScraper:
    def __init__(self, src: Source, client: httpx.AsyncClient):
        self.src, self.client = src, client
    
    async def fetch(self) -> List[Job]:
        raise NotImplementedError
    
    def absolute_url(self, rel: str) -> str:
        return urljoin(self.src.url, rel)
    
    def text(self, val: Optional[str]) -> str:
        return re.sub(r"\s+", " ", val or "").strip()
    
    def make_fingerprint(self, title: str, emp: str, url: str) -> str:
        # Deduplication logic remains consistent
        return hashlib.sha256(f"{title.lower()}|{emp.lower()}|{url}".encode()).hexdigest()

class SmartHtmlScraper(AsyncBaseScraper):
    """
    Updated Scraper using CSS selectors to reduce 'noise' and footer links.
    """
    async def fetch(self) -> List[Job]:
        try:
            r = await self.client.get(self.src.url, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            jobs: List[Job] = []
            
            # Use specific selector if provided, else fallback to broad search
            selector = self.src.selector or "a[href]"
            for a in soup.select(selector):
                href, text = a.get("href", ""), self.text(a.get_text(" "))
                if href and text and any(k in f"{text} {href}".lower() for k in ["job", "vacan", "career", "role"]):
                    url = self.absolute_url(href)
                    jobs.append(Job(
                        source=self.src.name, 
                        source_kind=self.src.kind, 
                        title=text, 
                        employer=self.src.name,
                        url=url, 
                        fetched_at=datetime.now(timezone.utc).isoformat(),
                        fingerprint=self.make_fingerprint(text, self.src.name, url)
                    ))
            return jobs
        except Exception as e:
            logger.error(f"Failed to fetch {self.src.name}: {e}")
            return []

# --- DATABASE & SCORING LOGIC ---
# (Keep your existing Database class, prescore_job, and score_job functions here)

async def process_source(src: Source, client: httpx.AsyncClient, db: Any, config: Dict):
    """Handles a single source asynchronously."""
    scraper = SmartHtmlScraper(src, client)
    jobs = await scraper.fetch()
    stored, skipped = 0, 0
    
    for job in jobs:
        # Quick filter before detail fetch
        from __main__ import prescore_job, score_job, extract_job_detail # Assuming these are in the same script
        
        if prescore_job(job, config) < PRESCORE_THRESHOLD:
            skipped += 1
            continue
            
        # Polite delay before detail fetch to prevent rate-limiting
        await asyncio.sleep(1.0)
        
        # Detail fetching would also be moved to an async function in a full implementation
        # For brevity, calling your existing score logic
        job = score_job(job, config) # In a real async app, make detail fetch async too
        
        if job.score >= config["filters"]["minimum_score"]:
            can = db.find_canonical(job.title, job.employer)
            if can and can != job.fingerprint:
                db.merge_into_canonical(can, job)
            else:
                db.upsert_job(job, datetime.now(timezone.utc).isoformat())
                stored += 1
                
    logger.info(f"{src.name}: {len(jobs)} found, {skipped} skipped, {stored} stored")

async def run_async(config: Dict):
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, limits=limits) as client:
        from __main__ import Database # Assuming Database class is present
        with Database(config["db_path"]) as db:
            tasks = [process_source(Source(**s), client, db, config) for s in config["sources"]]
            await asyncio.gather(*tasks)

if __name__ == "__main__":
    # Example usage
    # asyncio.run(run_async(CONFIG))
    pass
