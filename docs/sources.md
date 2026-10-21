# Adding companies and sources

A **source** is one careers page you want watched, usually with your own search or filter applied. Sources live in [`config/companies.yaml`](../config/companies.yaml). Adding one never requires code changes, as long as the site type is supported.

## The easy way: paste a URL

1. Open the company's careers page in your browser.
2. Search or filter for your field: keyword, department, location, sort order.
3. Copy the URL from the address bar.
4. Preview it, then add it:

```bash
ihaveajob check-source "<url>"   # live preview: job count, matches, sample titles. Writes nothing.
ihaveajob add-source  "<url>"    # appends an entry to config/companies.yaml
```

5. Commit and push `config/companies.yaml`. The next scheduled run builds a **baseline** for the new source: existing jobs are recorded but not announced as new.

### Adding your own filters

Some sites filter only inside the browser, so the filter never appears in the URL. Watershed's location buttons are an example. In that case, add the filters yourself:

```bash
ihaveajob add-source "https://watershed.com/careers" \
  --keywords "data,climate,policy" \
  --exclude "intern,sales" \
  --departments "Engineering,Sustainability" \
  --locations "New York,Remote"
```

- `--keywords`: the job must mention at least one, in its title, department or description.
- `--exclude`: drop jobs whose title or department mention any of these.
- `--departments` and `--locations`: substring matches. "Remote" also matches the job's remote/workplace flag.

Filters are applied by IhaveaJob after fetching. Jobs that don't match are still tracked, so closures and updates stay accurate, but they're flagged `matches_filters = false` and hidden from `ihaveajob changes` and from future alerts.

Options: `--company "Display Name"` and `--key my-key` override the detected name and key.

## Supported sites

| Adapter | URLs recognised | What is fetched | Pagination / completeness |
|---|---|---|---|
| `watershed` | `watershed.com/careers` (any locale prefix) | Listing page, plus each job's page for its description and publish date (new jobs, then refreshed daily; at most 25 per run) | Full listing: **complete** |
| `google_careers` | `google.com/about/careers/applications/jobs/results?...` | The results page as you filtered it. Full descriptions are embedded. | **First page only** (20 jobs). robots.txt disallows `page=`, so closure detection is off when there are more matches |
| `ashby` | `jobs.ashbyhq.com/<board>` | `api.ashbyhq.com/posting-api/job-board/<board>` | Complete |
| `greenhouse` | `boards.greenhouse.io/<token>`, `job-boards.greenhouse.io/<token>` | `boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` | Complete if `meta.total` matches |
| `lever` | `jobs.lever.co/<site>` (and `jobs.eu.lever.co`) | `api.lever.co/v0/postings/<site>?mode=json`. URL `team`/`location` params become filters. | Complete |

**Many company sites embed one of the job boards.** If a company's own domain isn't supported, open one of its jobs and look at where "Apply" leads. If it goes to `jobs.ashbyhq.com/...`, `boards.greenhouse.io/...` or `jobs.lever.co/...`, add that URL instead.

**Check the company name.** Board names can collide. The Greenhouse board `watershed` belongs to *Watershed Informatics*, a bioinformatics company, not Watershed the climate company. `check-source` prints the company name each API reports.

## Verified response formats

These were captured from a GitHub Actions runner (the same environment the scheduler uses) on 2026-10-21. Test fixtures in `tests/fixtures/` are **synthetic**: they copy these structures but every job in them is invented.

### Watershed

- `https://watershed.com/careers` is server-rendered HTML with 39 jobs on the probe date. Each department is a `<h3>` followed by a `<ul>` of `<a class="job-link" href="/careers/<8-hex-id>-<slug>">` with `<span>` (title) and `.job-locations small` (one per location).
- Each job page has a schema.org `JobPosting` JSON-LD block. Fields used: `title`, `employmentUnit` (department), `employmentType`, `jobLocationType`, `description` (HTML), and `datePosted` (date only; stored with precision `date`).
- `validThrough` is regenerated as "now + about 31 days" on every request, so it is **ignored**.
- The "Apply" link points to Ashby (`jobs.ashbyhq.com/Watershed/<uuid>`). The 8-hex job id is the last 8 hex digits of that UUID.
- robots.txt allows `/careers` (it disallows `/api/`, `/preview/` and others, none of which we use).

### Google Careers

- The results page embeds `AF_initDataCallback({key: 'ds:1', ..., data: [jobs, null, total_matches, page_size]})`.
- Each job record is a list, read by position: `0` id, `1` title, `3`/`4`/`10` responsibilities, qualifications and about (HTML), `7` company (Google, DeepMind, ...), `9` locations, `12`/`13`/`14` `[epoch_seconds, nanos]`. Some records are 20 items long instead of 21.
- Across 20 records, `12` is always the earliest and `13`/`14` move together when a job is edited. So `12` is used as the publish time, labelled `google.created_ts.inferred` because Google doesn't document it. `14` is stored as `source_updated_at`.
- The record's own apply link is a sign-in redirect with an opaque token. The stable public job page `.../jobs/results/<id>-<slug>` is used as the link instead (checked to open the posting).
- robots.txt (`User-agent: *`) allows `/jobs/results` but disallows `page=`. On the probe date, `q=sustainability` matched 256 jobs, of which the first 20 are readable. **Narrow your search** (keyword plus location) so the jobs you care about fit on page 1.
- Field positions are undocumented and may change. If parsing fails, the run fails loudly and no jobs are closed.

## Adding support for a new site

1. Write `src/ihaveajob/collectors/<site>.py` with a `Collector` subclass:
   - `name`: the adapter id used in YAML.
   - `detect(url)`: return a `UrlDetection` for URLs you handle, otherwise `None`.
   - `fetch(source, client, ctx)`: return a `FetchResult(jobs, complete, total_reported)`.
   - Set `uses_html = True` and call `client.get_text(url, check_robots=True)` for HTML pages.
2. Set `complete=True` **only** if the result contains every open job. When in doubt, return `False`: it only disables closure detection.
3. Raise `FetchError("parse_error", ...)` when the page doesn't look as expected. Never return an empty list for a page you couldn't parse.
4. Register it in `COLLECTORS` in `collectors/__init__.py`.
5. Add a synthetic fixture and tests. Document the real response format here.
6. Prefer official APIs or feeds over HTML, and respect each site's terms and robots.txt.
