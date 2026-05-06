# Job Search Agent — Claude Project Instructions

## What this project is

An automated job monitoring agent that scrapes senior-level vacancies from multiple job boards daily, scores them for relevance, deduplicates against a SQLite database, and emails an HTML report. Runs on GitHub Actions on a daily/weekly schedule.

Owner: Charles Knight (charles.knight@gmail.com)

---

## Three search profiles

| Profile | Target roles | Config | Output |
|---|---|---|---|
| `he` | HE senior leadership (PVC, Dean, Registrar, Director) | `HE_CONFIG` | `new_jobs_report.html` |
| `charity` | Charity CEO and Director roles | `CHARITY_CONFIG` | `charity_new_jobs_report.html` |
| `sector` | Sector bodies and professional associations | `SECTOR_BODIES_CONFIG` | `sector_new_jobs_report.html` |

---

## How to run

```bash
# Daily report (single profile)
python job_search_agent.py --profile he
python job_search_agent.py --profile charity
python job_search_agent.py --profile sector

# Weekly digest
python job_search_agent.py --profile he --weekly
```

---

## Architecture

1. **Scrape** — async HTTP requests (httpx) to job board listing pages; CSS selectors extract job title links
2. **Fetch descriptions** — follow each job URL to get full description text
3. **Score** — rule-based relevance scoring using title matching, keyword signals, and exclusion penalties
4. **Deduplicate** — SQLite database per profile stores job fingerprints; only new jobs appear in reports
5. **Report** — HTML email report generated and sent via Gmail SMTP

---

## Scoring logic

Each job gets a float score. Jobs below `minimum_score` are filtered out.

Signals (weights vary by profile — see config):
- `executive_bonus` — title matches exec_titles list
- `director_bonus` (charity/sector only) — title matches director_titles list
- `permanent_signal` — description suggests permanent role
- `expertise_signal` — description matches domain expertise keywords
- `sector_fit_bonus` (charity/sector) — content matches sector context
- `geography_bonus` (HE only) — UK geography signals
- `exclusion_penalty` — title/description matches exclusion_terms (hard negative)

Title gate: jobs whose title doesn't contain any `title_gate` term are dropped before scoring.

---

## GitHub Actions schedule

- Daily at 9 AM UTC — standard report
- Monday at 8 AM UTC — weekly digest (includes older jobs)
- Manual trigger available via `workflow_dispatch`

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

```
httpx
beautifulsoup4
lxml
```

Install: `pip install -r requirements.txt`

---

## Known limitations

- Odgers Berndtson excluded — vacancies are JavaScript-rendered, not in HTML source
- Perrett Laver selector targets vacancy links by href pattern — verify on first run
- Some job boards may change their HTML structure without notice, breaking selectors
- No retry on selector failure — if a source returns 0 results it fails silently

---

## Writing/coding conventions

- Python 3.11
- Async scraping via `httpx` and `asyncio`
- SQLite for deduplication state (one `.db` file per profile)
- No external APIs — all scraping is HTML parsing via BeautifulSoup
- Keep scoring logic transparent and rule-based — no ML
- When adding new sources, add a comment explaining why the selector was chosen
