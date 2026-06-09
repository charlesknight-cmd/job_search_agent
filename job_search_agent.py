import asyncio
import hashlib
import html
import json
import logging
import random
import re
import sqlite3
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import httpx
import yaml
from bs4 import BeautifulSoup

# --- SYSTEM SETTINGS ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 30
MAX_DESCRIPTION_CHARS = 5000
REPORT_ROW_LIMIT = 200
MAX_RETRIES = 2
RETRY_BACKOFF = 3.0  # seconds
# Mark jobs not re-seen within this window as stale.
STALE_AFTER_HOURS = 168

logger = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO, log_file: str = "job_search.log") -> None:
    """Configure root logging for the CLI entry point.

    Called from ``__main__`` so importing the module (e.g. from tests) does
    not reconfigure logging or open a file handler.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    root.addHandler(fh)
    root.addHandler(sh)


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
    salary: Optional[int] = None  # Highest plausible GBP figure extracted, or None


@dataclass
class SourceStats:
    """Per-source funnel counts surfaced in the email footer.

    A source with ``links_matched == 0`` across consecutive runs is a strong
    signal that its selector is broken or the page is JS-rendered. A source
    with healthy ``links_matched`` but ``candidates == 0`` usually means the
    title gate is dropping everything (page is generic content, not vacancies).
    """
    name: str
    links_matched: int = 0
    candidates: int = 0
    new_scored: int = 0
    listing_failed: bool = False


# --- CONFIGURATION ---
# Search profiles live in YAML files under ``profiles/`` so non-Python users
# can edit search parameters without touching this module.
PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

REQUIRED_PROFILE_KEYS = {
    "label", "db_path", "output_prefix", "filters", "sources",
    "weights", "exec_titles", "title_gate", "exclusion_terms",
}


def load_profile(name: str, profiles_dir: Optional[Path] = None) -> Dict:
    """Load a search profile from ``profiles/<name>.yaml``.

    Raises FileNotFoundError if the profile file does not exist, and
    ValueError if required keys are missing — failing fast is better than
    scraping with a malformed config.
    """
    base = profiles_dir or PROFILES_DIR
    path = base / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Profile {name} did not parse to a mapping")
    missing = REQUIRED_PROFILE_KEYS - cfg.keys()
    if missing:
        raise ValueError(f"Profile {name} is missing required keys: {sorted(missing)}")

    # Floor-ordering sanity check: remote_salary_floor above the absolute
    # floor would silently invert the intended behaviour (a remote £85k role
    # would be penalised when the same onsite role would not). Catch at
    # load time rather than producing surprising empty reports.
    filters = cfg.get("filters", {})
    rsf = filters.get("remote_salary_floor")
    sf = filters.get("salary_floor")
    if rsf is not None and sf is not None and rsf > sf:
        raise ValueError(
            f"Profile {name}: remote_salary_floor ({rsf}) must be <= "
            f"salary_floor ({sf}). A remote floor above the absolute "
            f"floor would penalise remote roles more strictly than onsite."
        )
    return cfg


HE_CONFIG = load_profile("he")
CHARITY_CONFIG = load_profile("charity")
SECTOR_BODIES_CONFIG = load_profile("sector")
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
    soup = BeautifulSoup(page_html, "html.parser")

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


# --- SALARY EXTRACTION ---
# Three alternatives, each capturing the digits group:
#   £NNk / £NNNk     (suffix form, 2-3 digits + k/K)
#   £NN,NNN          (thousand-separator form)
#   £NNNN-£NNNNNNN   (bare 4-7 digit form, e.g. "£75000")
# The k-form's negative lookahead avoids matching units like "£75kg".
# The bare form's lookahead avoids partial matches inside larger numbers.
SALARY_RE = re.compile(
    r"£\s*(\d{2,3})\s*[kK](?![a-zA-Z])"
    r"|"
    r"£\s*(\d{1,3}(?:,\d{3})+)"
    r"|"
    r"£\s*(\d{4,7})(?!\d)"
)
# Reject anything below this once normalised to pounds. Set well above the
# incidental figures that pepper senior listings — relocation allowances,
# conference bursaries, stipends, vouchers ("£1,500 relocation", "£25 gift
# voucher") — so they don't masquerade as a salary signal. Otherwise a role
# that never stated pay would have its largest incidental amount treated as
# the salary, tripping a spurious "Below Target" penalty (the intent is that
# no stated salary stays neutral). No plausible senior salary sits below this.
MIN_PLAUSIBLE_SALARY = 20000


def extract_salary(text: str) -> Optional[int]:
    """Return the highest plausible GBP salary figure in ``text``, or ``None``.

    Picking the highest value (not the first) handles bands like
    "£60k-£85k" — we want the top of the band so a senior role isn't
    mis-scored on its floor. Returns ``None`` when nothing plausible is
    found, which the caller treats as a neutral signal (no bonus, no penalty).
    """
    if not text:
        return None
    best: Optional[int] = None
    for match in SALARY_RE.finditer(text):
        k_form, comma_form, bare_form = match.groups()
        if k_form is not None:
            amount = int(k_form) * 1000
        elif comma_form is not None:
            amount = int(comma_form.replace(",", ""))
        else:
            amount = int(bare_form)
        if amount < MIN_PLAUSIBLE_SALARY:
            continue
        if best is None or amount > best:
            best = amount
    return best


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
        r"\b(University of (?:the\s+)?[A-Z][a-zA-Z']*(?:\s+(?:of|the|upon|and)?\s*[A-Z][a-zA-Z']*)*)\b",
        r"\b([A-Z][a-zA-Z']*(?:\s+(?:[A-Z][a-zA-Z']*|of|and|the|for))*\s+(?:University|College|Institute|Trust|Charity)(?:\s+[A-Z][a-zA-Z']*)*)\b",
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
# Hot-path constants — compiled / built once at import time rather than on
# every score_job() call. Both are immutable; safe to share across calls.
PERMANENT_RE = re.compile(r"(?<![\w-])(?:permanent|substantive)\b", re.IGNORECASE)

# Detects remote / hybrid *working arrangements*. Naive substring match on
# bare "remote"/"hybrid" produces too many false positives — "hybrid learning",
# "hybrid course delivery", "hybrid event", "remote possibility", "remote
# location" are all common in HE descriptions for reasons unrelated to where
# the postholder sits. Each pattern below requires an arrangement-qualifying
# word ("working", "role", "first", etc.) so "hybrid course" does NOT match
# but "hybrid working" does.
REMOTE_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"fully\s+remote"
    r"|remote[\s-]first"
    r"|remote\s+work(?:ing)?"
    r"|remote\s+(?:role|position|opportunity|job|based|contract)"
    r"|hybrid\s+work(?:ing)?"
    r"|hybrid\s+(?:role|position|model|arrangement|approach|pattern)"
    r"|work[\s-]from[\s-]home"
    r"|wfh"
    r")\b",
    re.IGNORECASE,
)
# Negation guard: a positive match preceded by "not"/"no"/"without" within
# ~30 chars is treated as not-remote. Catches "this is not a remote role"
# and "no hybrid working available".
NEGATION_LOOKBACK_RE = re.compile(r"\b(?:not|no|without)\b[^.]{0,30}$", re.IGNORECASE)


def detect_remote_arrangement(text: str) -> bool:
    """Return True iff ``text`` describes a remote/hybrid working arrangement.

    Two-step check: a qualifier-specific phrase must match (e.g. "remote
    working", not bare "remote"), AND the 30 chars immediately preceding
    that match must not contain a negation word. This is strict by design:
    the cost of a false positive (penalty-free sub-floor salary) is higher
    than the cost of a false negative (a single role we don't recognise as
    remote, but which still scores correctly on its absolute salary).
    """
    for match in REMOTE_SIGNAL_RE.finditer(text):
        window = text[max(0, match.start() - 30):match.start()]
        if NEGATION_LOOKBACK_RE.search(window):
            continue
        return True
    return False

# Keys are matched as substrings against ``full_text`` (lowercased title +
# description). Short acronyms are wrapped in spaces to avoid matching inside
# unrelated words (e.g. " ofs " not "ofs", to keep "officers" / "offset" out;
# " tne " not "tne", to keep "witness" / "fitness" out).
EXPERTISE_MAP = {
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
    "student experience": "Student Experience",
    "student success": "Student Success",
    "student outcomes": "Student Outcomes",
    "learning analytics": "Learning Analytics",
    "micro-credential": "Micro-credentials",
    "microcredential": "Micro-credentials",
    "stackable": "Stackable Qualifications",
    "transnational": "TNE",
    " tne ": "TNE",
    "widening participation": "Widening Participation",
    "access and participation": "Widening Participation",
    " ofs ": "OfS",
    "office for students": "OfS",
    "b3 metric": "B3 Metrics",
    " b3 ": "B3 Metrics",
    "leadership development": "Leadership Development",
    "professional development": "Professional Development",
    "aurora": "Aurora",
    "athena swan": "Athena Swan",
    "edtech": "EdTech",
    "ed tech": "EdTech",
    "digital learning": "Digital Learning",
    "online learning": "Digital Learning",
    "learning gain": "Learning Gain",
    "employability": "Employability",
    "graduate outcomes": "Graduate Outcomes",
    "portfolio review": "Portfolio Review",
    "validation panel": "Validation",
    "lifelong learning": "Lifelong Learning",
    "knowledge exchange": "Knowledge Exchange",
    "edi ": "EDI",
    " edi,": "EDI",
    "equality, diversity": "EDI",
    "equality diversity": "EDI",
}
# Cap surfaced expertise tags so the report's chip list stays readable.
MAX_EXPERTISE_TAGS = 3


def score_job(job: Job, config: Dict) -> Job:
    w = config["weights"]
    full_text = f"{job.title} {job.description}".lower()
    # Computed once and reused for both geography_bonus and the remote
    # salary tradeoff so the two paths cannot drift.
    is_remote = detect_remote_arrangement(full_text)
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
    if PERMANENT_RE.search(full_text):
        job.score += w["permanent_signal"]
        job.match_reasons.append("Permanent")
    elif any(t in full_text for t in ["interim", "fixed-term", "fixed term"]):
        if senior_match:
            job.match_reasons.append("Strategic Interim")
        else:
            job.score -= 15
            job.match_reasons.append("Short-term Contract")

    # 4. Sector expertise — score the bonus once, surface up to MAX_EXPERTISE_TAGS
    # labels so the report's chip list reflects the role's full scope (e.g.
    # "Student Outcomes" + "Learning Analytics" + "EdTech") instead of one.
    # De-duplicate while preserving insertion order in case multiple keys map
    # to the same label (e.g. micro-credential / microcredential → Micro-credentials).
    matched_expertise = []
    seen_labels = set()
    for key, label in EXPERTISE_MAP.items():
        if key in full_text and label not in seen_labels:
            matched_expertise.append(label)
            seen_labels.add(label)
    if matched_expertise:
        job.score += w["expertise_signal"]
        job.match_reasons.extend(matched_expertise[:MAX_EXPERTISE_TAGS])

    # 5. Geography (HE profile only). The remote/hybrid portion delegates to
    # the shared is_remote computed once at the top — previously the inline
    # list held bare "remote"/"hybrid" substrings that would drift from the
    # salary-tradeoff detector.
    if w.get("geography_bonus"):
        uk_terms = ["scotland", "edinburgh", "glasgow", "st andrews",
                    "dundee", "aberdeen", "stirling"]
        if any(t in full_text for t in uk_terms) or is_remote:
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

    # 7. Salary signal
    # Bonus when a stated salary clears the target, penalty when it's clearly
    # below the floor, neutral otherwise (including when no salary is stated —
    # most senior search-firm listings omit it). Thresholds and bonus magnitude
    # are profile-tunable so charity/sector targets can differ from HE.
    filters = config.get("filters", {})
    salary_floor = filters.get("salary_floor", 60000)
    salary_target = filters.get("salary_target", 65000)
    # remote_salary_floor defaults to salary_floor when not set, so profiles
    # that haven't opted in keep the strict behaviour. Where it is set, a
    # role with remote/hybrid signals only gets penalised if it falls below
    # the lower bound — the standard "Below Target" tag still appears so the
    # report makes the floor in play visible to the reader.
    remote_salary_floor = filters.get("remote_salary_floor", salary_floor)
    salary_bonus = w.get("salary_bonus", 15)
    # Title is rarely where salary is stated, but include it cheaply for the
    # occasional "Director (£90k)" style listing.
    salary = extract_salary(f"{job.title} {job.description}")
    job.salary = salary
    effective_floor = remote_salary_floor if is_remote else salary_floor
    if salary is not None:
        if salary >= salary_target:
            job.score += salary_bonus
            job.match_reasons.append("Salary Match")
        elif salary < effective_floor:
            job.score -= salary_bonus
            job.match_reasons.append("Below Target")
        elif is_remote and salary < salary_floor:
            # In the remote-tradeoff band — above the remote floor but below
            # the absolute floor. Score is neutral, but surface the reason
            # so the reader sees why the role wasn't filtered out.
            job.match_reasons.append("Remote OK")

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
                source TEXT,
                title TEXT,
                employer TEXT,
                url TEXT,
                description TEXT,
                score REAL,
                reasons TEXT,
                fetched_at TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                status TEXT DEFAULT 'new',
                salary INTEGER
            )
        """)
        cursor = self.conn.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in cursor.fetchall()]
        if "reasons" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN reasons TEXT")
        if "source" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT")
        if "status" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
        if "last_seen_at" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN last_seen_at TEXT")
        if "salary" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary INTEGER")
        # find_canonical/touch_seen_many filter by url — without an index
        # SQLite scans the table on every lookup.
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_url_idx ON jobs(url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_source_idx ON jobs(source)")
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
            INSERT INTO jobs (fingerprint, source, title, employer, url, description, score, reasons, fetched_at, first_seen_at, last_seen_at, status, salary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                source = excluded.source,
                score = excluded.score,
                reasons = excluded.reasons,
                fetched_at = excluded.fetched_at,
                last_seen_at = excluded.last_seen_at,
                salary = excluded.salary
        """, (
            job.fingerprint, job.source, job.title, job.employer, job.url,
            job.description, job.score, ", ".join(job.match_reasons),
            job.fetched_at, now, now, job.salary
        ))
        self.conn.commit()

    def touch_seen_many(self, urls: List[str], source: Optional[str] = None) -> None:
        """Record that a batch of already-known URLs were seen on this run.

        Lets stale-marking distinguish jobs still listed on the source from
        jobs that have actually disappeared. Re-seeing a stale URL makes it
        ``new`` again, while curated statuses remain untouched. Batched into a
        single ``executemany`` + commit so a source re-seeing hundreds of known
        URLs pays one fsync, not one per URL — and so all DB *writes* stay on
        the serial caller side (see ``process_profile``).
        """
        if not urls:
            return
        now = datetime.now(timezone.utc).isoformat()
        if source is None:
            self.conn.executemany(
                "UPDATE jobs "
                "SET last_seen_at = ?, "
                "status = CASE WHEN status = 'stale' THEN 'new' ELSE status END "
                "WHERE url = ?",
                [(now, url) for url in urls],
            )
        else:
            self.conn.executemany(
                "UPDATE jobs "
                "SET last_seen_at = ?, "
                "source = COALESCE(source, ?), "
                "status = CASE WHEN status = 'stale' THEN 'new' ELSE status END "
                "WHERE url = ?",
                [(now, source, url) for url in urls],
            )
        self.conn.commit()

    def mark_stale(
        self,
        hours: int = STALE_AFTER_HOURS,
        sources: Optional[List[str]] = None,
    ) -> int:
        """Mark jobs not seen within ``hours`` as 'stale'. Returns affected row count.

        Curated statuses ('interested', 'applied') are preserved as well as
        'rejected' and 'stale': ``status`` doubles as the user's application
        pipeline, so a listing disappearing must not silently overwrite the
        fact that the user has already applied to or flagged the role.

        When ``sources`` is supplied, only jobs from successfully checked
        sources are eligible. That prevents a temporary source outage from
        making still-live vacancies look stale.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        params = [cutoff]
        source_filter = ""
        if sources is not None:
            unique_sources = sorted(set(sources))
            if not unique_sources:
                return 0
            placeholders = ", ".join("?" for _ in unique_sources)
            source_filter = f" AND source IN ({placeholders})"
            params.extend(unique_sources)
        cursor = self.conn.execute(
            "UPDATE jobs SET status = 'stale' "
            "WHERE status NOT IN ('stale', 'rejected', 'interested', 'applied') "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)"
            f"{source_filter}",
            params,
        )
        self.conn.commit()
        return cursor.rowcount

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
DESCRIPTION_EXCERPT_CHARS = 400


def _summarise_rows(rows: list) -> str:
    """One-line breakdown of the report contents for the header.

    Counts roles by the most informative tag present in their reasons. Tags
    are mutually exclusive in scoring (exec > director > other), so a role
    falls into exactly one bucket.
    """
    exec_count = director_count = other_count = 0
    for r in rows:
        reasons = (r["reasons"] or "").lower()
        if "executive level" in reasons:
            exec_count += 1
        elif "director level" in reasons:
            director_count += 1
        else:
            other_count += 1
    parts = []
    if exec_count:
        parts.append(f"{exec_count} Executive")
    if director_count:
        parts.append(f"{director_count} Director")
    if other_count:
        parts.append(f"{other_count} Other")
    return " · ".join(parts) if parts else "no roles"


def _render_source_panel(source_stats: List[SourceStats]) -> str:
    """Render the per-source funnel table for the email footer.

    Three columns per row: links matched on the listing page → candidates
    passing the title gate → new jobs that cleared the score threshold. Sources
    showing 0 links across consecutive runs are highlighted so the maintainer
    can quickly spot broken selectors (typically JS-rendered pages).
    """
    if not source_stats:
        return ""
    rows = []
    for s in source_stats:
        if s.listing_failed:
            status_cell = "<span style='color:#c62828;'>fetch failed</span>"
        elif s.links_matched == 0:
            status_cell = "<span style='color:#c62828;'>0 — selector dry?</span>"
        else:
            status_cell = f"{s.links_matched}"
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;'>{html.escape(s.name)}</td>"
            f"<td style='padding:4px 10px; text-align:right;'>{status_cell}</td>"
            f"<td style='padding:4px 10px; text-align:right;'>{s.candidates}</td>"
            f"<td style='padding:4px 10px; text-align:right;'>{s.new_scored}</td>"
            "</tr>"
        )
    return (
        "<div style='font-family:sans-serif; margin-top:30px; padding:15px;"
        " background:#ffffff; border:1px solid #e0e0e0; border-radius:8px;"
        " font-size:0.85em; color:#424242;'>"
        "<p style='margin:0 0 8px 0; font-weight:bold; color:#1a237e;'>"
        "Per-source funnel (links → gate-passed → new)</p>"
        "<table style='border-collapse:collapse; width:100%;'>"
        "<thead><tr style='background:#f5f5f5;'>"
        "<th style='padding:4px 10px; text-align:left;'>Source</th>"
        "<th style='padding:4px 10px; text-align:right;'>Links</th>"
        "<th style='padding:4px 10px; text-align:right;'>Candidates</th>"
        "<th style='padding:4px 10px; text-align:right;'>New</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def generate_html_report(
    rows: list,
    title: str,
    filename: str,
    source_stats: Optional[List[SourceStats]] = None,
    window_hours: Optional[int] = None,
):
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
        url_is_safe = scheme in ("http", "https")
        safe_url = html.escape(url, quote=True) if url_is_safe else "#"

        full_description = r["description"] or ""
        # textwrap.shorten preserves word boundaries and handles the ellipsis,
        # so descriptions never cut mid-word. Replace newlines first so the
        # collapsing-whitespace behaviour produces a clean single-line excerpt.
        description_excerpt = textwrap.shorten(
            (full_description or "").replace("\n", " "),
            width=DESCRIPTION_EXCERPT_CHARS,
            placeholder="…",
        )

        score_value = r["score"] if r["score"] is not None else 0
        score_display = (
            int(score_value) if float(score_value).is_integer() else round(score_value, 1)
        )

        # Title/score row uses a 2-cell table rather than flexbox. Outlook on
        # Windows renders email HTML through Word and ignores flexbox — the
        # table form is the long-standing email-HTML compatibility pattern.
        title_row = (
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            "border='0' style='border-collapse:collapse;'>"
            "<tr>"
            "<td style='vertical-align:top;'>"
            f"<h3 style='margin:0 0 6px 0; color:#1a237e;'>{html.escape(r['title'] or '')}</h3>"
            "</td>"
            "<td style='vertical-align:top; text-align:right; white-space:nowrap;'>"
            f"<span style='font-size:0.8em; background:#1a237e; color:white; "
            f"padding:3px 8px; border-radius:4px;'>Score: {score_display}</span>"
            "</td>"
            "</tr></table>"
        )

        if url_is_safe:
            cta_html = (
                f"<a href=\"{safe_url}\" target=\"_blank\" rel=\"noopener noreferrer\" "
                "style='background:#1a237e; color:white; padding:10px 20px; "
                "text-decoration:none; border-radius:6px; font-weight:bold; "
                "display:inline-block;'>View Opening</a>"
            )
        else:
            cta_html = (
                "<span style='background:#eeeeee; color:#888; padding:10px 20px; "
                "border-radius:6px; font-weight:bold; display:inline-block;'>"
                "Source link unavailable</span>"
            )

        # Salary chip: only rendered when the scraper extracted a plausible
        # GBP figure. The colour is keyed to the role's target — green at/above,
        # amber if below the floor we care about — so triage is visual at a
        # glance rather than requiring you to read the number. Roles tagged
        # "Remote OK" (sub-floor salary that scoring deliberately let through
        # because the role is remote/hybrid) render amber rather than pink so
        # the chip doesn't visually contradict the tag immediately next to it.
        salary_value = r["salary"] if "salary" in r.keys() else None
        remote_ok = "Remote OK" in (r["reasons"] or "")
        if salary_value:
            if salary_value >= 100000:
                salary_bg, salary_fg, salary_border = "#e8f5e9", "#1b5e20", "#a5d6a7"
            elif salary_value >= 80000 or remote_ok:
                salary_bg, salary_fg, salary_border = "#fff8e1", "#bf6700", "#ffd180"
            else:
                salary_bg, salary_fg, salary_border = "#fce4ec", "#880e4f", "#f48fb1"
            salary_html = (
                f"<span style='background:{salary_bg}; color:{salary_fg}; "
                f"padding:3px 10px; border-radius:4px; margin-left:8px; "
                f"border:1px solid {salary_border}; font-size:0.85em; "
                f"font-weight:bold;'>£{salary_value:,}</span>"
            )
        else:
            salary_html = ""

        cards.append(f"""
            <div style="border:1px solid #e0e0e0; padding:20px; margin-bottom:20px;
                        border-radius:12px; background:{status_bg};
                        box-shadow:0 2px 4px rgba(0,0,0,0.05); font-family:sans-serif;">
                {title_row}
                <p style="margin:0 0 10px 0;"><strong>{html.escape(r['employer'] or '')}</strong>{salary_html}</p>
                <div style="margin-bottom:12px;">{reasons_html}</div>
                <p style="color:#424242; font-size:0.9em; line-height:1.5; margin-bottom:15px;">
                    {html.escape(description_excerpt)}
                </p>
                <div>
                    {cta_html}
                    <span style="font-size:0.8em; color:#666; margin-left:10px;">
                        First seen: {html.escape((r['first_seen_at'] or '')[:10])}
                        &nbsp;|&nbsp; Status: <strong>{html.escape(status)}</strong>
                    </span>
                </div>
            </div>""")

    content = "".join(cards) if cards else (
        "<p style='font-family:sans-serif;'>No opportunities met the suitability threshold.</p>"
    )
    safe_title = html.escape(title)
    summary_line = _summarise_rows(rows)
    window_label = ""
    if window_hours:
        if window_hours >= 24 and window_hours % 24 == 0:
            window_label = f" &nbsp;|&nbsp; window: last {window_hours // 24} day(s)"
        else:
            window_label = f" &nbsp;|&nbsp; window: last {window_hours}h"
    panel_html = _render_source_panel(source_stats or [])
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{safe_title}</title></head>
<body style='max-width:860px; margin:auto; padding:40px; background:#f8f9fa;'>
    <h2 style='font-family:sans-serif; color:#1a237e;
               border-bottom:2px solid #1a237e; padding-bottom:10px;'>
        {safe_title}
    </h2>
    <p style='font-family:sans-serif; color:#666; font-size:0.9em;'>
        Generated: {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}
        &nbsp;|&nbsp; {len(rows)} role(s) listed &nbsp;|&nbsp; {html.escape(summary_line)}{window_label}
    </p>
    {content}
    {panel_html}
</body></html>""")


# --- EXECUTION ---
# Maximum concurrent detail-page fetches per source. Keeps us polite to a
# single origin while still benefiting from async I/O instead of the previous
# strict 1.5s-per-fetch serial pacing.
DETAIL_FETCH_CONCURRENCY = 3
# Light jitter between detail fetches within a source so we don't burst even
# at the semaphore limit.
DETAIL_FETCH_JITTER = (0.3, 0.8)


async def _scrape_source(
    client: httpx.AsyncClient,
    src: Dict,
    cfg: Dict,
    db: Database,
) -> tuple:
    """Scrape one source: fetch listing, filter, fetch details concurrently, score.

    Returns ``(jobs, stats, seen_urls)`` where ``jobs`` is the list of *new*
    scored jobs ready for the caller to upsert, ``stats`` is a
    :class:`SourceStats` capturing the funnel counts for the email footer
    panel, and ``seen_urls`` is the already-known URLs the caller should
    refresh ``last_seen_at`` for. This coroutine only *reads* from the DB
    (``find_canonical``); every write — upserts and seen-refreshes — is
    returned to the caller so all writes run serially on the single SQLite
    connection. Coroutines do the I/O, the caller does the writes.
    """
    stats = SourceStats(name=src["name"])
    seen_urls: List[str] = []

    listing_resp = await fetch_with_retry(client, src["url"])
    if listing_resp is None:
        stats.listing_failed = True
        logger.info(f"{src['name']}: listing fetch failed — 0 link(s) processed")
        return [], stats, seen_urls

    soup = BeautifulSoup(listing_resp.text, "html.parser")
    links = list(soup.select(src["selector"]))
    stats.links_matched = len(links)
    # Per-source match count surfaces dead selectors passively in the log —
    # any source returning 0 matches across consecutive runs is a candidate
    # for removal from the YAML.
    logger.info(f"{src['name']}: {len(links)} link(s) matched selector")
    if not links:
        return [], stats, seen_urls

    candidates = []
    seen_candidate_urls = set()
    for a in links:
        raw_title = a.get_text(" ").strip()
        href = a.get("href", "")
        if not href or len(raw_title) < 10:
            continue
        url = urljoin(src["url"], href)

        if db.find_canonical(raw_title, url):
            # Known URL — record it so the caller can refresh last_seen_at,
            # which keeps stale-marking from firing while the listing is still
            # up. Collected here, written caller-side (see docstring).
            seen_urls.append(url)
            continue

        if not any(t in raw_title.lower() for t in cfg["title_gate"]):
            continue

        if url in seen_candidate_urls:
            continue
        seen_candidate_urls.add(url)
        candidates.append((raw_title, url))

    stats.candidates = len(candidates)
    if not candidates:
        return [], stats, seen_urls

    sem = asyncio.Semaphore(DETAIL_FETCH_CONCURRENCY)

    async def fetch_and_score(raw_title: str, url: str) -> Optional[Job]:
        async with sem:
            await asyncio.sleep(random.uniform(*DETAIL_FETCH_JITTER))
            det = await fetch_with_retry(client, url)
            if det is None:
                return None
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
            return score_job(job, cfg)

    results = await asyncio.gather(
        *[fetch_and_score(t, u) for t, u in candidates],
        return_exceptions=True,
    )

    scored: List[Job] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Error processing candidate in {src['name']}: {r}")
            continue
        if r is None:
            continue
        if r.score >= cfg["filters"]["minimum_score"]:
            scored.append(r)
    stats.new_scored = len(scored)
    return scored, stats, seen_urls


# --- SOURCE PAGINATION ---
# Some boards return one short page per request and paginate the rest. The
# source model is otherwise one-URL-one-fetch, so paginated sources are
# expanded into one concrete source per page before scraping. Defaults match
# jobs.ac.uk: page size 25, 1-based ``startIndex`` stepping 1, 26, 51, ….
DEFAULT_PAGE_PARAM = "startIndex"
DEFAULT_PAGE_SIZE = 25


def expand_sources(sources: List[Dict]) -> List[Dict]:
    """Flatten any source that requests pagination into one source per page.

    A source opts in with ``pages: N`` (N > 1). Pagination varies a single query
    parameter — ``page_param`` (default ``startIndex``) — starting from its value
    in ``url`` (default 1 if absent) and stepping by ``page_size`` (default 25).

    All pages keep the source's ``name`` so DB attribution and stale-marking stay
    coherent (the ``source`` column doubles as the user's pipeline); the per-source
    funnel panel aggregates the pages back into one row (see ``_aggregate_stats``).

    jobs.ac.uk caps ``pageSize`` at 25 and only paginates reliably when the search
    query (``keywords`` or ``academicDisciplineFacet[]``) is present in *every*
    request — otherwise pagination tracks a stateful per-client "current search"
    and concurrent fetches bleed together. So the query must live in the base
    ``url``; this expander preserves it on every page. Sources without ``pages``
    (or ``pages <= 1``) pass through unchanged.
    """
    expanded: List[Dict] = []
    for src in sources:
        pages = int(src.get("pages", 1) or 1)
        if pages <= 1:
            expanded.append(src)
            continue
        page_param = src.get("page_param", DEFAULT_PAGE_PARAM)
        page_size = int(src.get("page_size", DEFAULT_PAGE_SIZE))
        parsed = urlparse(src["url"])
        query = parse_qsl(parsed.query, keep_blank_values=True)
        start = 1
        for k, v in query:
            if k == page_param:
                try:
                    start = int(v)
                except ValueError:
                    start = 1
                break
        base_query = [(k, v) for k, v in query if k != page_param]
        for i in range(pages):
            page_query = base_query + [(page_param, str(start + i * page_size))]
            # safe="[]" keeps facet keys like academicDisciplineFacet[] literal —
            # quote_plus would percent-encode the brackets.
            page_url = urlunparse(
                parsed._replace(query=urlencode(page_query, safe="[]"))
            )
            page_src = {
                k: v for k, v in src.items()
                if k not in ("pages", "page_param", "page_size")
            }
            page_src["url"] = page_url
            expanded.append(page_src)
    return expanded


def _aggregate_stats(stats: List[SourceStats]) -> List[SourceStats]:
    """Merge per-page SourceStats sharing a name into one funnel-panel row.

    Pagination expands a configured source into one fetch per page, but the email
    should still show one row per source. Counts sum; a source is flagged
    ``listing_failed`` only if *every* page failed — a partial-page failure still
    yielded jobs, so the selector isn't actually dead.
    """
    merged: Dict[str, SourceStats] = {}
    order: List[str] = []
    for s in stats:
        if s.name not in merged:
            merged[s.name] = SourceStats(name=s.name, listing_failed=True)
            order.append(s.name)
        m = merged[s.name]
        m.links_matched += s.links_matched
        m.candidates += s.candidates
        m.new_scored += s.new_scored
        m.listing_failed = m.listing_failed and s.listing_failed
    return [merged[n] for n in order]


async def process_profile(profile_key: str, weekly: bool, dry_run: bool = False):
    cfg = PROFILES[profile_key]
    if dry_run:
        logger.info(f"[dry-run] {profile_key}: scoring only — no DB writes, no report")

    with Database(cfg["db_path"]) as db:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True
        ) as client:
            # Expand paginated sources (e.g. jobs.ac.uk keyword queries that span
            # several pages) into one concrete per-page source first.
            sources = expand_sources(cfg["sources"])
            # Sources hit different origins, so we run them concurrently;
            # per-source semaphore inside _scrape_source keeps detail-fetch
            # load polite per host. return_exceptions so one broken source
            # cannot bring down the whole run.
            source_results = await asyncio.gather(
                *[_scrape_source(client, src, cfg, db) for src in sources],
                return_exceptions=True,
            )

        source_stats: List[SourceStats] = []
        successful_sources: List[str] = []
        for src, result in zip(sources, source_results):
            if isinstance(result, Exception):
                logger.error(f"Error scraping {src['name']}: {result}")
                source_stats.append(SourceStats(name=src["name"], listing_failed=True))
                continue
            jobs, stats, seen_urls = result
            source_stats.append(stats)
            if not stats.listing_failed and stats.links_matched > 0:
                successful_sources.append(src["name"])
            # Refresh last_seen_at for already-known URLs (skipped on dry runs).
            if not dry_run:
                db.touch_seen_many(seen_urls, source=src["name"])
            for job in jobs:
                if dry_run:
                    logger.info(f"[dry-run] would save: [{job.score}] {job.title} @ {job.employer}")
                else:
                    db.upsert_job(job)
                    logger.info(f"Saved: [{job.score}] {job.title} @ {job.employer}")

        if dry_run:
            return

        stale_count = db.mark_stale(sources=successful_sources)
        if stale_count:
            logger.info(f"Marked {stale_count} job(s) as stale (not seen in {STALE_AFTER_HOURS}h)")

        # Daily window is profile-configurable so HE (a lower-cadence market
        # where roles are advertised once and recruited over 4-8 weeks) can use
        # a wider 72h window without flooding the higher-cadence charity feed.
        daily_window = cfg["filters"].get("daily_window_hours", 24)
        window_hours = 168 if weekly else daily_window
        recent = db.get_recent(window_hours, cfg["filters"]["minimum_score"])
        fname = f"{cfg['output_prefix']}{'weekly_digest' if weekly else 'new_jobs_report'}.html"
        generate_html_report(
            recent,
            f"{'Weekly' if weekly else 'Daily'} {cfg['label']} Report",
            fname,
            source_stats=_aggregate_stats(source_stats),
            window_hours=window_hours,
        )
        logger.info(f"Report written: {fname} ({len(recent)} roles)")


if __name__ == "__main__":
    import argparse
    setup_logging()
    p = argparse.ArgumentParser(description="Job search agent for HE leadership and charity CEO roles")
    p.add_argument("--profile", choices=["he", "charity", "sector", "all"], default="he",
                   help="Which search profile to run (all runs every profile)")
    p.add_argument("--weekly", action="store_true",
                   help="Generate a 7-day digest instead of 24-hour update")
    p.add_argument("--dry-run", action="store_true",
                   help="Scrape and score but do not write to the DB or generate a report — useful for iterating on selectors and scoring")
    args = p.parse_args()

    if args.profile == "all":
        for profile_key in ["he", "charity", "sector"]:
            asyncio.run(process_profile(profile_key, args.weekly, args.dry_run))
    else:
        asyncio.run(process_profile(args.profile, args.weekly, args.dry_run))
