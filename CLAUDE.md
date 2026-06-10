# Job Search Agent — Claude Project Instructions

## What this project is

An automated job monitoring agent that scrapes senior-level vacancies from multiple job boards daily, scores them for relevance, deduplicates against a SQLite database, and emails an HTML report. Runs on GitHub Actions on a daily/weekly schedule.

Owner: Charles Knight (charles.knight@gmail.com)

---

## Three search profiles

| Profile | Target roles | Profile file | Output |
|---|---|---|---|
| `he` | HE senior leadership (PVC, Dean, Registrar, Director) | `profiles/he.yaml` | `new_jobs_report.html` |
| `charity` | Charity CEO and Director roles | `profiles/charity.yaml` | `charity_new_jobs_report.html` |
| `sector` | Sector bodies and professional associations | `profiles/sector.yaml` | `sector_new_jobs_report.html` |

Profiles are loaded from YAML at startup by `load_profile()` in `job_search_agent.py`. Required keys: `label`, `db_path`, `output_prefix`, `filters`, `sources`, `weights`, `exec_titles`, `title_gate`, `exclusion_terms`. Missing keys fail fast at load time.

---

## How to run

```bash
# Daily report (single profile)
python job_search_agent.py --profile he
python job_search_agent.py --profile charity
python job_search_agent.py --profile sector

# All profiles in sequence
python job_search_agent.py --profile all

# Weekly digest (7-day window instead of 24h)
python job_search_agent.py --profile he --weekly

# Dry run — scrape and score, but no DB writes and no report
python job_search_agent.py --profile he --dry-run
```

---

## Architecture

1. **Load profile** — YAML config from `profiles/<name>.yaml` (validated against `REQUIRED_PROFILE_KEYS`)
2. **Scrape** — async HTTP requests (httpx) to job board listing pages with retry/backoff; CSS selectors extract job title links
3. **Fetch descriptions** — follow each job URL to get full description text (skipping URLs already in the DB)
4. **Score** — rule-based relevance scoring using title matching, keyword signals, and exclusion penalties
5. **Deduplicate** — SQLite database per profile stores job fingerprints; only new jobs appear in reports. URLs already known are touched (`last_seen_at` updated) so stale-marking can detect when a listing actually disappears.
6. **Stale-mark** — jobs not seen for `STALE_AFTER_HOURS` (default 168h) are marked `status = 'stale'`
7. **Report** — HTML email report generated and sent via Gmail SMTP

---

## Scoring logic

Each job gets a float score. Jobs below `minimum_score` (in the profile's `filters` block) are filtered out.

Signals (weights vary by profile — see each profile's `weights` block):
- `executive_bonus` — title matches `exec_titles` list
- `director_bonus` (charity/sector only) — title matches `director_titles` list
- `permanent_signal` — description suggests permanent role (word-boundary match on `permanent`/`substantive`, so `non-permanent` does **not** trip it)
- `expertise_signal` — description matches domain expertise keywords (PSF, NTFS, TEF, REF, accreditation, governance, AI in education, etc.)
- `sector_fit_bonus` (charity/sector) — content matches sector context (education charity, social mobility, widening participation, etc.)
- `geography_bonus` (HE only) — UK geography signals (Scotland, remote, hybrid, etc.)
- `exclusion_penalty` — title/description matches `exclusion_terms` (hard negative; short-circuits scoring)

Title gate: jobs whose title doesn't contain any `title_gate` term are dropped before description fetch.

Senior+interim handling: if a senior title is matched and the description suggests interim/fixed-term, the job is tagged "Strategic Interim" rather than penalised.

---

## HTTP retry behaviour

`fetch_with_retry` retries on `429`, `503`, and network errors. It honours the `Retry-After` header (delta-seconds form only) capped at `RETRY_AFTER_CAP` (60s). Other 4xx responses are treated as permanent failures and return `None` — so the caller never parses a 404 page as content.

---

## GitHub Actions schedule

- Daily at 09:15 UTC — standard report (off the contended top-of-hour slot)
- Monday at 08:00 UTC — weekly digest (7-day window)
- Manual trigger available via `workflow_dispatch` (with optional `weekly` input)

Three parallel jobs run independently: `he-agent`, `charity-agent`, `sector-agent`.

Databases persist between runs via GitHub Actions artifacts (`job-databases-he`, `job-databases-charity`, `job-databases-sector`).

---

## Required secrets

| Secret | Purpose |
|---|---|
| `MAIL_USERNAME` | Gmail address for sending reports |
| `MAIL_PASSWORD` | Gmail app password (not account password) |

---

## Dependencies

Runtime (`requirements.txt`):

```
httpx>=0.27,<0.29
beautifulsoup4>=4.12,<5.0
PyYAML>=6.0,<7.0
```

Dev/test (`requirements-dev.txt`): adds `pytest` and `pre-commit`.

Install: `pip install -r requirements.txt` (or `-r requirements-dev.txt` for the test suite).

---

## Tests

Tests live under `tests/` and use stdlib `unittest` (so they also run under `pytest`):

```bash
python -m unittest discover -s tests -v
# or
pytest
```

Coverage focus: `score_job` (including the regression test for the `non-permanent` false-positive), `extract_employer`, and `_parse_retry_after`. When changing scoring logic, add a test case here first.

---

## Known limitations

- Odgers Berndtson excluded — vacancies are JavaScript-rendered, not in HTML source
- Source audit (June 2026): many boards had silently gone dry (selector rot or a move to client-side rendering). Repaired by selector fix (Peridot, CharityJob, NFP Consulting), card-heading title fallback (Dixon Walter), or by switching to a server-rendered data path that bypasses the JS app: **THE UniJobs** via its Madgex RSS feed (`format: rss`), **Veredus** via its WP Job Manager jobs feed (`format: rss`), **Minerva Search** via the role links still present in its HTML. Working sources after the audit: jobs.ac.uk, THE UniJobs, Peridot, Dixon Walter, Minerva, Veredus (HE); CharityJob, NFP People, NFP Consulting (charity/sector).
- Still excluded (genuinely JS-rendered with no readable feed/endpoint found): Odgers Berndtson, Perrett Laver, Saxton Bampfylde, Anderson Quigley (HE); Harris Hill, Third Sector (charity). Prospectus and Guardian Jobs return HTTP 403 (anti-bot). Per-firm leads (Saxton exposes only a `people` REST type and loads roles via admin-ajax; Harris Hill uses a shazamme.io POST API; Third Sector renders a Madgex app with no feed at the obvious paths) are noted in the commented YAML blocks for a future pass.
- Re-enabling a JS-rendered source means finding a server-rendered path: an RSS/Atom feed (`format: rss`), the site's JSON/AJAX endpoint, or role links that are actually in the HTML behind an unexpected path. A headless browser is the last resort — the current scraper is httpx + BeautifulSoup + stdlib feed parsing only, by design
- Some job boards may change their HTML structure without notice, breaking selectors — the per-source funnel panel in each email flags any source showing 0 links
- `Retry-After` HTTP-date form is intentionally not supported (clock-skew not worth the complexity)
- jobs.ac.uk (June 2026): RSS feeds retired and the old job-type taxonomy removed. `/search/<slug>` now returns the full unfiltered set (page 1 only) and the old `.j-search-result__title a` selector matches nothing. The agent now uses the server-rendered **keyword search** (`/search/?keywords=…`) with the title selector `.j-search-result__text > a`. The search query (keyword or `academicDisciplineFacet[]`) must be in every request — jobs.ac.uk otherwise tracks a stateful per-client "current search" and concurrent fetches bleed together. Results are newest-first; `pageSize` is capped at 25 and pages step `startIndex` by 25.

---

## Writing/coding conventions

- Python 3.11
- Async scraping via `httpx` and `asyncio`
- SQLite for deduplication state (one `.db` file per profile)
- No external APIs — all scraping is HTML parsing via BeautifulSoup
- Search parameters live in YAML under `profiles/`; non-Python users can edit them without touching code
- Keep scoring logic transparent and rule-based — no ML
- When adding a new source, leave a comment in the YAML explaining why the selector was chosen so future maintainers can revalidate when the page structure shifts
- Multi-page sources: a source may set `pages: N` (optional `page_param`, default `startIndex`; `page_size`, default 25) to fetch N pages. `expand_sources()` flattens it into one fetch per page before scraping; all pages keep the source `name` (so DB attribution/stale-marking stay coherent) and the funnel panel re-aggregates them into one row. The query that makes pagination work must live in the base `url`.
- Feed sources: a source may set `format: rss` (and omit `selector`) to be parsed as an RSS/Atom feed via `parse_feed_entries()` (stdlib `xml.etree`, no lxml) instead of a CSS selector. Use this when a board dropped its HTML listing for a JS app but still publishes a feed (e.g. THE UniJobs' Madgex `jobsrss` endpoint, which honours a `Keywords` query — so feed sources can be keyword-narrowed like the jobs.ac.uk ones).
- Pre-commit hooks (Black, Flake8, basic hygiene) configured in `.pre-commit-config.yaml`
