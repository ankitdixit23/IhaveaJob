# IhaveaJob

An open-source, AI-powered job intelligence platform that monitors company career pages, discovers opportunities, evaluates career relevance and visa sponsorship, and delivers personalized email alerts through an interactive dashboard.

> **Status:** monitoring any careers page, change detection, history, hourly scheduling, **email alerts** and a **web app for managing pages** work today. Relevance scoring, visa analysis, saved jobs, exports and multi-user accounts are planned (see [Roadmap](#roadmap)). Nothing below describes a feature that isn't implemented.

## What it does

1. **You give it careers-page links, from any company.** Search or filter the page for your field if it has a search box, then paste the URL into the **web app** ([docs/dashboard.md](docs/dashboard.md)). You can also use a form in GitHub (works from your phone), add one line to `config/companies.yaml`, or run `ihaveajob add-source "<url>"`.
2. **Every 2 hours, it checks every page** on GitHub's servers, so your computer can be off. Each link is read the best way available:
   - the company's job-board feed (Workday, Ashby, Greenhouse, Lever), found automatically even when it's hidden behind the company's own site;
   - structured job data or job links on the page;
   - a real headless browser for pages built with JavaScript;
   - and, as a last resort, watching the page text.
3. **It emails you what changed:** new jobs, edited jobs (old → new, with added and removed description lines), jobs no longer listed, and page-text changes. You get one digest per check, only when something changed, and never the same item twice.

Pages that block automated access, need a login, or whose robots.txt forbids reading are reported as such. They're never silently skipped, and the protections are never worked around.

**Verified live (2026-10-21):** Watershed, Google, Microsoft, Meta, Apple, Amazon, NVIDIA (Workday), RMI (Workday found on its site), WRI (followed "Job Openings"). Tesla is correctly reported as blocked. See [docs/sources.md](docs/sources.md#verified-on-2026-10-21-from-githubs-servers).

## Get it running (about 10 minutes, $0)

1. **Email:** add the Gmail App Password and recipient as repository secrets ([docs/email.md](docs/email.md)).
2. **Pages:** go to Actions → **add careers page** → Run workflow, and paste a link. Repeat for each company.
3. **Start:** go to Actions → **monitor** → Run workflow. You'll get a "monitoring started" email, then change digests every 2 hours.
4. Optional: add a private Postgres database as the `DATABASE_URL` secret ([docs/scheduling.md](docs/scheduling.md)).

## Local development

```bash
git clone https://github.com/ankitdixit23/IhaveaJob.git && cd IhaveaJob
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,render]" && playwright install chromium   # browser is optional locally

ihaveajob check-source "https://www.wri.org/careers"   # preview any link (no database writes)
ihaveajob add-source "https://www.wri.org/careers" --keywords "data,climate"
ihaveajob run                     # one pass (first run per page = silent baseline)
ihaveajob notify --dry-run        # print the email that would be sent
ihaveajob status                  # per-page health
pytest                            # 117 tests, no network needed
streamlit run dashboard/app.py    # the web app (local mode)
```

## Documentation

- [Adding careers pages and how links are read](docs/sources.md)
- [Email alerts](docs/email.md)
- [Web app for managing pages](docs/dashboard.md)
- [Scheduling, storage and limits](docs/scheduling.md)
- [Architecture](docs/architecture.md) · [Configuration](docs/configuration.md) · [Database](docs/database.md)
- [Security and privacy](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Sources, collectors, normalization, dedup, history, change detection, scheduled workflow | **Done** |
| 2 | Universal reader for any careers link; Workday; email digests (hourly, deduplicated) | **Done** (single recipient list via secrets; per-user preferences come with accounts in Phase 5) |
| 3 | Transparent 0–100 relevance scoring; visa/work-authorization evidence extraction | Planned |
| 4 | Streamlit dashboard, saved jobs and application pipeline, CSV/Excel export, optional Google Sheets | **Started:** web app for adding and managing pages and viewing recent changes |
| 5 | Accounts, per-user preferences and data isolation, security review, public-release checklist | Planned |

## License

[MIT](LICENSE)
