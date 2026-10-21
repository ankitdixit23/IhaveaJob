# IhaveaJob

An open-source, AI-powered job intelligence platform that monitors company career pages, discovers opportunities, evaluates career relevance and visa sponsorship, and delivers personalized email alerts through an interactive dashboard.

> **Status: Phase 1 of 5 (monitoring foundation).** Job collection, change detection, history and scheduled runs work today. Email alerts, relevance scoring, visa analysis, the dashboard and multi-user accounts are planned. See [Roadmap](#roadmap). Nothing below describes a feature that isn't implemented.

## What works now

- **Paste a careers URL to track it.** Search or filter a company's careers page for your field, copy the URL, and run `ihaveajob add-source "<url>"`. The site type is detected automatically, and search filters in the URL are kept.
- **Supported sources:**
  | Source | How it's read | Verified live |
  |---|---|---|
  | Watershed (`watershed.com/careers`) | Listing HTML + JSON-LD on each job page | Yes (2026-10-21) |
  | Google Careers search results | Data embedded in the results page, first page only | Yes (2026-10-21) |
  | Ashby, Greenhouse, Lever job boards | Official public job-board APIs | Unit-tested only |
- **Tracked per job:** title, company, location(s), department, employment type, description, application link, the employer's publish date (kept separate from when IhaveaJob first saw the job), last seen time, and full change history.
- **Change detection:** new, updated (with a field-by-field diff), reopened, and closed jobs.
- **No false alerts:**
  - The first run for a company builds a silent *baseline*, so adding a company never produces a flood of "new" jobs.
  - Closures need several consecutive complete fetches *and* a minimum time gap.
  - Failed, partial, capped, empty or suspiciously shrunken responses never close jobs.
- **Polite and compliant:** robots.txt is checked before reading any HTML page, with timeouts, retries with backoff, `Retry-After` support, per-host rate limiting, response size caps and per-run request caps.
- **Runs while your Mac is off:** a GitHub Actions workflow runs every 30 minutes and stores history in a database that survives between runs.

## Quick start (local)

```bash
git clone https://github.com/ankitdixit23/IhaveaJob.git && cd IhaveaJob
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Preview what a careers URL returns (no database writes):
ihaveajob check-source "https://www.google.com/about/careers/applications/jobs/results?q=sustainability"

# Track a page, optionally narrowing it further:
ihaveajob add-source "https://watershed.com/careers" --keywords "data,climate" --locations "New York,Remote"

ihaveajob run        # one monitoring pass (first run per source = baseline)
ihaveajob status     # per-source health and counts
ihaveajob changes --hours 24   # recent new/updated/closed jobs that match your filters
pytest               # 75 tests, no network needed
```

By default the database is a local SQLite file at `data/ihaveajob.db`. Set `DATABASE_URL` to use Postgres.

## Documentation

- [Adding companies and sources](docs/sources.md): including the verified response format of each source
- [Architecture](docs/architecture.md)
- [Configuration and environment variables](docs/configuration.md)
- [Database and migrations](docs/database.md)
- [Scheduling with GitHub Actions](docs/scheduling.md): setup, limits, recovery
- [Security and privacy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Source config, collectors, normalization, dedup, history, change detection, scheduled workflow | **Done** |
| 2 | SMTP email alerts (instant and daily digest), per-user notification preferences and dedup | Planned |
| 3 | Transparent 0–100 relevance scoring; visa/work-authorization evidence extraction | Planned |
| 4 | Streamlit dashboard, saved jobs and application pipeline, CSV/Excel export, optional Google Sheets | Planned |
| 5 | Authentication, per-user data isolation, security review, public-release checklist | Planned |

## License

[MIT](LICENSE)
