# Architecture

```
config/companies.yaml ──► config.load_sources ──► SourceConfig[]
                                                   │
GitHub Actions (cron / manual) ──► cli `run` ──► services.monitor.run_monitor
                                                   │ for each enabled source (own transaction)
                                                   ▼
                       collectors.<adapter>.fetch(source, HttpClient, CollectContext)
                         │  HttpClient: robots.txt, timeouts, retries, Retry-After,
                         │  per-host throttle, size cap, request counting
                         ▼
                       FetchResult(jobs: NormalizedJob[], complete, total_reported)
                         ▼
                       monitor.process_result
                         ├─ dedup: (source, external_id), fallback canonical URL
                         ├─ merge: None never erases stored fields
                         ├─ hash tracked fields → "updated" + diff
                         ├─ filters.matches → jobs.matches_filters
                         ├─ baseline on first success (no "new")
                         └─ closure only if complete + sanity check + N misses + age
                         ▼
                       database (SQLAlchemy; SQLite or Postgres; Alembic migrations)
```

## Package layout

| Path | Responsibility |
|---|---|
| `src/ihaveajob/models.py` | Plain dataclasses shared across layers (`SourceConfig`, `SourceFilters`, `NormalizedJob`, `FetchResult`) |
| `src/ihaveajob/collectors/` | One module per site or API, plus the registry and URL detection. Collectors never touch the database. |
| `src/ihaveajob/http.py`, `robots.py` | Polite HTTP and an RFC 9309 robots.txt matcher (wildcards supported) |
| `src/ihaveajob/normalization/` | HTML to text (scripts stripped), canonical URLs, date parsing, content hashing |
| `src/ihaveajob/filters.py` | Applies your keyword/department/location filters |
| `src/ihaveajob/database/` | ORM models, engine/session helpers, Alembic migrations |
| `src/ihaveajob/services/monitor.py` | The monitoring run and all change-detection rules |
| `src/ihaveajob/cli.py` | `ihaveajob` command |
| `tests/` | Unit tests with synthetic fixtures. No network. |

Planned packages: `scoring/` and `visa/` (Phase 3), `notifications/` (Phase 2), `auth/` (Phase 5), and `dashboard/` (Phase 4).

## Key decisions

- **Presence is tracked for every fetched job, not just matching ones.** Otherwise a job whose description loads one run later would look "new", and a job that stops matching your filter would look "closed".
- **`complete` is the collector's promise.** Closure detection trusts only complete fetches. Capped sources (Google page 1) report `complete=False` when more matches exist.
- **Publish time vs detection time.** `published_at` (plus `published_at_source` and `published_at_precision`) is what the employer says. `first_detected_at` is when we first saw the job. They are never mixed.
- **Untrusted content.** Job descriptions are external input. They are converted to text with scripts removed, stored as data, and never executed or interpreted as instructions. Future AI features must keep them in the data channel of any prompt.
- **Public vs private data.** Phase 1 stores only public job data. User preferences, notification history and application tracking (later phases) will live in separate tables and require a private database (see `docs/scheduling.md`).
