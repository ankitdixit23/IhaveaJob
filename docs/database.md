# Database and migrations

SQLAlchemy 2 models with Alembic migrations in `src/ihaveajob/database/migrations/`. `ihaveajob run` (and every other command that opens the database) applies pending migrations automatically. You can also run `ihaveajob db-upgrade`.

SQLite is used for local development and tests. Postgres is recommended for the deployed monitor (see [scheduling.md](scheduling.md)). Timestamps are stored as UTC.

## Tables (Phase 1: public job data only)

| Table | Purpose | Key columns / indexes |
|---|---|---|
| `companies` | Employers | `slug` unique |
| `sources` | Monitored pages, mirrored from `companies.yaml` | `key` unique; `baseline_completed_at`, `last_complete_count`, `consecutive_failures` |
| `jobs` | One row per posting per source | unique `(source_id, external_id)`; indexes on `canonical_url`, `status`, `first_detected_at`, `published_at`, `company_id` |
| `job_changes` | Append-only history: `baseline`, `new`, `updated` (with field diff), `closed`, `reopened` | indexes on `job_id` and `(detected_at, change_type)` |
| `monitor_runs` | One per workflow run, with a JSON summary | |
| `source_runs` | One per source per run: status, `complete`, closure-check reason, counts, HTTP requests, error kind and message | index `(source_id, started_at)` |

Notable `jobs` columns:

- `published_at`, `published_at_source`, `published_at_precision`: the employer's date, where it came from, and whether it is a date or a timestamp.
- `first_detected_at` and `last_seen_at`: our observations.
- `status`: `open`, `missing` (absent from a complete fetch, not yet confirmed), or `closed`. Also `missing_count` and `closed_at`.
- `is_baseline`: true if the job was found while building the source's baseline.
- `matches_filters`: whether the job currently passes the source's filters.
- `content_hash`: SHA-256 of normalized tracked fields, used to detect updates.

Added in migration 0002: `sources.state` (how a page was last read, last error), `page_snapshots` (visible-text snapshots with added and removed lines for pages without identifiable jobs), `notifications` (each email attempt: status, subject, error, hashed recipient) and `notification_items` (unique per recipient and item, which prevents duplicate emails).

Planned tables: users, preferences, saved jobs and application tracking (Phases 2–5), scores and visa evidence (Phase 3). They will be added as new migrations.

## Creating a migration

```bash
# after editing database/models.py
python - <<'EOF'
from alembic import command
from ihaveajob.database import make_engine, alembic_config
cfg = alembic_config(make_engine("sqlite:///data/migration-scratch.db"))
command.upgrade(cfg, "head")
command.revision(cfg, message="describe change", autogenerate=True)
EOF
```

Review the generated file and replace custom column types (e.g. `UTCDateTime`) with `sa.DateTime()` so migrations stay frozen. Tests run every migration on a fresh database.
