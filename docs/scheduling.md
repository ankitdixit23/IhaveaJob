# Scheduling with GitHub Actions

`.github/workflows/monitor.yml` runs every 2 hours at minute 7, and on demand via **Actions → monitor → Run workflow**. Each run:

1. restores the database;
2. checks every page (`ihaveajob run`), using a headless browser where needed;
3. emails what changed (`ihaveajob notify`), if email secrets are set;
4. saves the database.

It runs on GitHub's servers, so your computer can be off.

## Where history is stored

Runners are temporary, so the database must live somewhere else. Two options:

| Mode | When | Notes |
|---|---|---|
| **Postgres via `DATABASE_URL` secret** (recommended) | The `DATABASE_URL` repository secret is set | Private. Required before any per-user data (Phase 2+) is stored. Free tiers exist (e.g. Neon, Supabase), but check their current limits; free tiers can pause idle databases or change terms. |
| **SQLite on the `data` branch** (default) | No `DATABASE_URL` secret | Zero setup. The workflow restores `ihaveajob.db` from the `data` branch, runs, and force-pushes it back as a single commit, so repository history doesn't grow. **The branch is as public as the repository**, so it must only ever hold public job data. |

To switch to Postgres: create a database, then add the secret under **Settings → Secrets and variables → Actions → New repository secret**, named `DATABASE_URL` with value `postgresql://user:pass@host/db?sslmode=require`. The next run creates the tables. History in the `data` branch is not migrated automatically. The first Postgres run builds fresh baselines.

## Limits to know

- **Cost.** GitHub-hosted standard runners are free for public repositories. A *private* repository on the Free plan gets 2,000 minutes per month. A run with the headless browser takes roughly 2–4 minutes; every 2 hours that's about 1,000–1,500 minutes a month, close to the limit in a private repo. Keep the repo public, or reduce the schedule (for example `7 */4 * * *`).
- **Delays.** GitHub runs schedules on a best-effort basis. Runs can start late or be dropped when GitHub is busy, especially at the top of the hour, which is why the cron uses minute 7. "Every 2 hours" means roughly every 2 hours.
- **Auto-disable.** In public repositories, scheduled workflows are disabled after **60 days with no repository activity**. GitHub emails you, and you can re-enable it from the Actions tab with one click. Pushing any commit also counts as activity.
- **Overlap.** `concurrency: monitor` makes runs queue instead of overlapping, which would corrupt the SQLite file.
- **Request budget per run.** For each page: robots.txt, the page itself, sometimes one followed "openings" link or one headless-browser load, plus at most 25 job pages (only for new jobs, or for details older than a day). Each host gets at least 1 second between requests. Images, fonts and media are not loaded by the headless browser.

## Health and failures

- A source that fails (timeout, HTTP error, layout change) is logged as structured JSON. Its jobs are left untouched and its failure counter increases.
- After **3 consecutive failures** of the same source, `ihaveajob run` exits with code 2. The workflow is then marked failed, and GitHub emails the repository owner by default.
- Each run's summary, plus the last hour's changes, appears on the run page under **Summary**.
- `ihaveajob status` shows per-source health: last run, error, failure streak, baseline state.

## Recovery

| Problem | What to do |
|---|---|
| Workflow disabled after 60 days | Actions tab → *monitor* → **Enable workflow**. Missed jobs are picked up on the next run. Their `published_at` still reflects the employer's date. |
| A source keeps failing with `parse_error` | The site changed its layout. Run `ihaveajob check-source <url>` locally, then update the collector and its fixture. Jobs are not closed in the meantime. |
| `robots_disallowed` | The site changed its robots.txt to forbid that page. Stop monitoring it (`enabled: false`). Don't work around it. |
| Corrupted or lost `data` branch | Delete the `data` branch. The next run starts a new database and rebuilds baselines without alerting. Change history before that point is lost, which is why Postgres is recommended. |
| Want to re-run now | Actions → monitor → Run workflow. |
