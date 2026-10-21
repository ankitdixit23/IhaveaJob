# Security and privacy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a public issue.

## Current state (Phase 1)

- **No user accounts or personal data are stored yet.** The database holds public job postings, change history and run logs only.
- **Secrets** (database URL; SMTP credentials later) come from environment variables or GitHub Actions secrets. Nothing secret belongs in the repository. `.gitignore` excludes `.env`, databases, exports and credential files. `.env.example` contains placeholders only.
- **The `data` branch is public** when the repository is public. It is only used when no `DATABASE_URL` secret is set, and it must only ever contain public job data. Before Phase 2 stores any user data, deployments must use a private database. The code will enforce this.
- **Workflow hardening:** manual-run inputs reach shell scripts through environment variables, never interpolated into the script. Workflow permissions are limited to `contents: write` (monitor, for the data branch) and `contents: read` (tests).

## Handling external content

Job pages and API responses are **untrusted input**:

- HTML is converted to plain text with `<script>`, `<style>` and `<noscript>` removed. It is stored as data and never executed or rendered as raw HTML.
- Malformed or unexpected responses raise a `parse_error`. They are never guessed at, and never cause jobs to be closed.
- Response size is capped (8 MB by default), and requests have timeouts.
- Planned AI features (Phase 3) must pass job text only as data inside a prompt. They must never let it select tools, run commands, or override instructions.

## Respecting websites

- robots.txt is checked before every HTML page fetch, with wildcard rules supported. Disallowed pages are not fetched. Example: Google's paginated results are never requested.
- Official public APIs are preferred over HTML. Requests identify themselves with an honest User-Agent, are rate-limited per host (at least 1 second apart), and honour `Retry-After`.
- Check each employer's terms of use before adding a source.

## Planned (Phases 2–5)

Authentication via an established provider or library, per-user authorization on every data path, separation of public job data from private user data, CSRF protection and rate limiting, account deletion, personal-data export, and a documented retention policy. A secrets and personal-data scan is required before any public release checklist is signed off.
