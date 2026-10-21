# Configuration

## Environment variables

All are optional in Phase 1. Copy `.env.example` to `.env` for local use. Your shell or a tool like `direnv` must export the values. The app doesn't auto-load `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/ihaveajob.db` | SQLAlchemy URL. `postgresql://…` and `postgres://…` are accepted (the `psycopg` driver is installed with the `postgres` extra). |
| `IHAVEAJOB_COMPANIES_FILE` | `config/companies.yaml` | Path to the sources file. |
| `IHAVEAJOB_CLOSE_AFTER_MISSES` | `3` | Consecutive complete fetches a job must be missing from before it's closed. |
| `IHAVEAJOB_CLOSE_AFTER_HOURS` | `6` | And at least this many hours since it was last seen. |
| `IHAVEAJOB_MAX_DETAIL_FETCHES` | `25` | Per-source, per-run cap on job-page requests (Watershed). |
| `IHAVEAJOB_DETAIL_REFRESH_HOURS` | `24` | How often job pages are re-read to detect description changes. |
| `IHAVEAJOB_USER_AGENT` | `IhaveaJobBot/0.1 (+repo URL)` | Sent with every request. Keep it identifying and honest. |
| `IHAVEAJOB_LOG_FORMAT` | `json` | `json` (structured) or `text`. |
| `IHAVEAJOB_LOG_LEVEL` | `INFO` | Standard logging level. |

| `SMTP_HOST`, `SMTP_PORT` | — / `587` | Email server (`smtp.gmail.com`, 587 = STARTTLS, 465 = SSL). See [email.md](email.md). |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | — | Login. For Gmail use an **App Password**. |
| `SMTP_FROM` | `IhaveaJob <SMTP_USERNAME>` | Sender shown in emails. |
| `ALERT_EMAIL_TO` | — | Comma-separated recipients. Email is off when unset. |
| `IHAVEAJOB_TIMEZONE` | `UTC` | Time zone for times in emails, e.g. `America/Los_Angeles`. In GitHub, set it as a repository *variable*. |
| `IHAVEAJOB_DISABLE_RENDER` | unset | `1` turns off the headless browser. |

Authentication and scoring settings arrive with Phases 3–5.

## `config/companies.yaml`

See the comments at the top of the file and [sources.md](sources.md). The file is committed to the repository, so it is public. Don't put personal data or credentials in it.

## CLI

```
ihaveajob check-source <url> [filters]     live preview, no database writes
ihaveajob add-source <url> [filters] [--company X] [--key k] [--dry-run]
ihaveajob sources                           list configured sources
ihaveajob run [--source KEY ...]            one monitoring pass
ihaveajob status                            per-source health
ihaveajob changes [--hours 24] [--types new,updated,closed,reopened] [--all]
ihaveajob notify [--dry-run]               email (or print) what changed since the last email
ihaveajob test-email                        send a test message
ihaveajob db-upgrade                        apply migrations (run also does this)
```

Filters: `--keywords`, `--exclude`, `--departments`, `--locations` (comma-separated).
