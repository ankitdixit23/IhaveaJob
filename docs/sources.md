# Adding careers pages

**Any public careers page link works.** Go to a company's careers page, search or filter it for what you want if it has a search box, copy the address bar, and add it. IhaveaJob works out the best way to read it.

## Three ways to add a page

**1. From GitHub (no terminal; works on your phone).** Go to **Actions → "add careers page" → Run workflow**, paste the URL, and optionally add a company name, keywords and locations. The workflow previews what it can read, adds the page to `config/companies.yaml`, and commits it. The preview appears on the run's Summary page.

**2. Edit `config/companies.yaml`.** Only `url` is required:

```yaml
sources:
  - url: https://www.wri.org/careers
  - url: https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?q=climate
    name: NVIDIA
    filters:
      keywords_any: [climate, sustainability, energy]
      locations: [Remote, California]
```

**3. Command line:**
```bash
ihaveajob check-source "<url>"      # preview: how it's read, what's found. Writes nothing.
ihaveajob add-source  "<url>" --keywords "climate,data" --locations "New York,Remote"
```

The first run after adding a page records a **baseline**: existing jobs are remembered, not emailed. After that you get emailed about changes.

## How a link is read

Specialised readers handle sites with known structure:

| Site | Method |
|---|---|
| Workday (`*.myworkdayjobs.com`) | The board's public JSON feed. Search text and filters in the URL are applied. |
| Ashby, Greenhouse, Lever boards | Their official public job-board APIs |
| Google Careers search | The data embedded in the results page (first page only, per robots.txt) |
| Watershed | The listing page plus each job's structured data |

**Every other link uses the universal reader**, which tries these in order and uses the first that works:

1. **An embedded job board.** Many company pages link to Workday, Ashby, Greenhouse or Lever behind the scenes. The reader switches to that board's feed. *Example: rmi.org/about/careers → RMI's Workday board.*
2. **Structured job data** (schema.org `JobPosting`) on the page.
3. **Job links.** Repeated links carrying a job ID, such as `/careers/job/1970…`, `/details/200314010`, `/profile/job_details/…` or `…_JR2025837`. The link text becomes the title, and nearby text (location, team) is kept as a summary.
4. **A "Job openings" link** on a landing page, followed once. *Example: wri.org/careers → /careers/jobs.*
5. **A headless browser** (real Chromium, on GitHub's servers) for pages built with JavaScript, then steps 1–3 again. *Examples: Microsoft, Meta.*
6. **Page-text watch.** If no individual jobs can be identified, the page's visible text is saved each run and you're emailed the lines that changed.

For new jobs, the reader also opens each job's page, up to 25 per run, to pick up the description and posting date when the site provides structured data.

### What can't be monitored

The reader never works around a site's protections, and it reports the reason instead of failing silently:

| Result | Meaning |
|---|---|
| `blocked` | The site refuses automated visitors (HTTP 401/403, bot protection) or requires login. *Example: tesla.com/careers.* |
| `not_found` | The link is wrong or the page was removed (HTTP 404). |
| `robots_disallowed` | The site's robots.txt forbids automated reading of that page. |

### Verified on 2026-10-21 (from GitHub's servers)

| Link | Read via | Result |
|---|---|---|
| watershed.com/careers | Watershed reader | 39 jobs |
| google.com/about/careers/…?q=sustainability | Google reader | 20 of 256 (first page) |
| jobs.careers.microsoft.com/…?q=sustainability | headless browser → job links | 20 jobs* |
| metacareers.com/jobs?q=sustainability | headless browser → job links | 10 jobs |
| jobs.apple.com/en-us/search?search=sustainability | job links | 20 jobs |
| nvidia.wd5.myworkdayjobs.com/…?q=climate | Workday feed | 34 jobs with details |
| rmi.org/about/careers | discovered Workday board | 2 jobs with details |
| wri.org/careers | followed "Job Openings" → job links | 18 jobs |
| amazon.jobs/en/search?base_query=sustainability | headless browser → job links | 10 jobs (after fixing the browser wait; the first attempt timed out) |
| apply.careers.microsoft.com/careers?query=sustainability | headless browser → job links | 19 jobs; search kept, but results are broad, so add keyword filters |
| tesla.com/careers/search | — | `blocked` (403) |
| edf.org/jobs | — | `not_found` (404) |

\* Microsoft's old search URL redirects to its new careers site and **drops the search term**. Use a search URL from the new site (`apply.careers.microsoft.com/careers?query=…`) and add `keywords` filters.

## Tips

- **Check that the search term survives.** Some sites ignore or drop URL search terms. `check-source` (or the preview in the "add careers page" workflow) shows what was actually found. If the results don't match your search, add `--keywords` filters, which IhaveaJob applies itself.
- **Big companies show only the first page of results.** Make the search narrow (keyword plus location) so the jobs you care about are on it. On these pages, jobs that drop off are not reported as removed, because they may have just moved to page 2.
- **Board names can collide.** The Greenhouse board `watershed` belongs to Watershed Informatics, not Watershed the climate company. Prefer the link from the company's own site.
- **Respect site terms.** Only add pages you're allowed to read automatically.

## Filters

| Filter | Effect |
|---|---|
| `keywords_any` | Job must mention at least one, in its title, department, description or card summary |
| `keywords_exclude` | Drop jobs whose title or department mention any |
| `departments` | Department must contain one |
| `locations` | A location (or "Remote") must contain one. For job-link pages, the card summary is checked. |

Non-matching jobs are still tracked, so updates and removals stay correct. They're just never emailed.

## Response formats (for developers)

- **Watershed:** server-rendered listing (`a.job-link` under department `<h3>`s), plus per-job JSON-LD `JobPosting` with `datePosted`. `validThrough` is regenerated on every request and ignored.
- **Google Careers:** `AF_initDataCallback` `ds:1` = `[jobs, null, total, page_size]`. Each job is a positional array: 0 id, 1 title, 3/4/10 HTML sections, 7 company, 9 locations, 12/13/14 `[epoch, nanos]`. Field 12 is treated as the posting time (inferred). robots.txt disallows `page=`.
- **Workday:** `POST /wday/cxs/{tenant}/{site}/jobs` with `{appliedFacets, limit: 20, offset, searchText}` returns `{total, jobPostings[{title, externalPath, locationsText, postedOn, bulletFields}]}`. `total` is reported only on the first page. Details come from `GET /wday/cxs/{tenant}/{site}{externalPath}`, giving `jobPostingInfo.jobDescription`. `postedOn` is relative ("Posted 3 Days Ago").

## Adding a specialised reader

Most sites don't need one. Write one only when the universal reader can't do well.

1. Add `src/ihaveajob/collectors/<site>.py` with a `Collector` subclass: `name`, `detect(url)`, and `fetch(source, client, ctx)` returning a `FetchResult`.
2. Set `complete=True` only when every open job was returned. On unexpected structure, raise `FetchError("parse_error", …)`. Never return an empty list for a page you couldn't parse.
3. Register it in `COLLECTORS` before `GenericPageCollector`, add a synthetic fixture and tests, and document the real format here.
