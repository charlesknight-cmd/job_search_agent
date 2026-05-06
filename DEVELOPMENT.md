# Development Guide

## Project structure

This is a single-file Python application:

- `job_search_agent.py` — scraping, scoring, deduplication, and report generation
- `requirements.txt` — runtime dependencies
- `.github/workflows/job-search-agent.yml` — daily/weekly schedule and email delivery
- `CLAUDE.md` — project instructions and architecture overview

There are no `src/`, `tests/`, or `docs/` directories at the moment.

## Setup

```bash
git clone https://github.com/charlesknight-cmd/job_search_agent.git
cd job_search_agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11 is the supported version (matches the GitHub Actions workflow).

## Running locally

```bash
python job_search_agent.py --profile he         # daily HE leadership report
python job_search_agent.py --profile charity    # daily charity CEO/director report
python job_search_agent.py --profile sector     # daily sector bodies report
python job_search_agent.py --profile all        # all three profiles in sequence
python job_search_agent.py --profile he --weekly  # 7-day digest
```

Outputs land in the working directory:

- `new_jobs_report.html` (HE) / `charity_new_jobs_report.html` / `sector_new_jobs_report.html`
- `jobs_he.db` / `charity_jobs.db` / `sector_bodies_jobs.db`
- `job_search.log`

## Code quality

Optional pre-commit hooks (Black, Flake8, basic hygiene) are configured in
`.pre-commit-config.yaml`:

```bash
pip install pre-commit
pre-commit install        # run on every commit
pre-commit run --all-files
```

There is no test suite at present. If you add one, place tests under `tests/`
and wire `pytest` into the workflow.

## Adding a new source

Sources are defined inside the relevant `*_CONFIG` dict in `job_search_agent.py`:

```python
{"name": "Source Name", "url": "https://...", "selector": "css selector"}
```

When adding one, leave a comment explaining why the selector was chosen so
future maintainers can revalidate when the page structure shifts.

## Deployment

The agent is deployed via GitHub Actions on a cron schedule — there is no
container or external host. See `.github/workflows/job-search-agent.yml`.
Required repository secrets:

- `MAIL_USERNAME` — Gmail address used to send the report
- `MAIL_PASSWORD` — Gmail app password (not the account password)

## Troubleshooting

- Selector returned 0 rows: open the source URL in a browser and re-check the
  selector against the current HTML. Some boards (e.g. Odgers Berndtson) render
  vacancies via JavaScript and cannot be scraped this way.
- Empty report: jobs are being filtered. Lower `minimum_score` in the relevant
  `_CONFIG` to inspect what's being discarded.
- Database not persisting in CI: confirm the `actions/upload-artifact` step ran
  and the artefact name matches the `dawidd6/action-download-artifact` step in
  the next run.
