# Contributing

Thanks for helping. The basics:

1. Fork, then create a branch: `git checkout -b feature/short-name`.
2. `pip install -e ".[dev]"` and `pytest`. Tests must pass without network access.
3. Keep changes focused, with type hints and tests. For a new site, follow [docs/sources.md](docs/sources.md#adding-support-for-a-new-site).
4. Never commit secrets, personal data, real résumés, databases, or full copies of third-party web pages. Test fixtures must be **synthetic** and say so in a comment.
5. Open a pull request describing what changed and how you tested it.

## Ground rules for collectors

- Prefer official job-board APIs and feeds. Use HTML only where the site's robots.txt and terms permit it.
- Mark results `complete=True` only when you are sure every open job was returned.
- Raise `FetchError("parse_error", ...)` on unexpected structure. Never return an empty list for a page you couldn't parse.
- Keep request volume low: no pagination workarounds, no robots.txt workarounds.

## Database changes

Edit `database/models.py`, then add an Alembic migration (see [docs/database.md](docs/database.md)). Never edit a migration that has already been released.
