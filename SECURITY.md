# Security and privacy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a public issue.

## Current state (Phases 1–2)

- **No user accounts or personal data are stored.** The database holds public job postings, change history, run logs, and a record of which changes were emailed. Recipients are identified only by a one-way hash of their address. Email addresses and SMTP credentials live only in GitHub secrets or environment variables.
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

- robots.txt is checked before every page fetch, headless-browser load and job-board API call, with wildcard rules supported. Disallowed pages are not fetched. Example: Google's paginated results are never requested.
- Sites that refuse automated visitors (401/403, bot protection, CAPTCHA, login walls) are reported as `blocked`. IhaveaJob never tries to evade them: no fake browser identities, no CAPTCHA solving, no proxy rotation.
- The headless browser identifies itself with the same honest User-Agent and does not load images, fonts or media.
- Email content built from job pages is HTML-escaped, and only `http(s)` links are rendered.
- Official public APIs are preferred over HTML. Requests identify themselves with an honest User-Agent, are rate-limited per host (at least 1 second apart), and honour `Retry-After`.
- Check each employer's terms of use before adding a source.

## Planned (Phases 2–5)

Authentication via an established provider or library, per-user authorization on every data path, separation of public job data from private user data, CSRF protection and rate limiting, account deletion, personal-data export, and a documented retention policy. A secrets and personal-data scan is required before any public release checklist is signed off.
