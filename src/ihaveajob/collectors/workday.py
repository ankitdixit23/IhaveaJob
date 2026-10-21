"""Workday (``*.myworkdayjobs.com``) collector.

Verified 2026-10-21 from a GitHub Actions runner against NVIDIA and
Salesforce boards:

* The careers page is a JavaScript app; its static HTML has no jobs.
* The app calls a public JSON endpoint, which we call directly:
  ``POST https://{host}/wday/cxs/{tenant}/{site}/jobs`` with
  ``{"appliedFacets": {...}, "limit": 20, "offset": N, "searchText": "..."}``
  → ``{"total": int, "jobPostings": [{"title", "externalPath",
  "locationsText", "postedOn": "Posted 3 Days Ago", "bulletFields": [req id]}]}``.
* ``GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}`` →
  ``{"jobPostingInfo": {"id", "title", "jobDescription" (HTML), ...}}``.
  Other jobPostingInfo fields (``startDate``, ``location``, ``timeType``,
  ``externalUrl``) are read when present but have not been verified on
  every tenant.
* robots.txt on both boards allows the site path and does not disallow
  ``/wday/cxs/``.

Search text and facet filters in the pasted URL (``?q=...``, or facet ids
such as ``?locations=...&jobFamilyGroup=...``) are passed to the endpoint.
Pagination: 20 per page, up to ``max_pages`` (default 10). If ``total``
exceeds what we read, the result is incomplete (no closures).
"""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import SplitResult, parse_qsl, urlsplit

from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig, SourceFilters
from ..normalization import clean_text, html_to_text, parse_datetime
from .base import CollectContext, Collector, UrlDetection

_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\s+days?\s+ago)", re.I)
_NON_FACET_PARAMS = {"q", "redirect", "source", "utm_source", "utm_medium", "utm_campaign"}


def parse_board(url: SplitResult) -> tuple[str, str, str] | None:
    m = _HOST.match(url.netloc.lower())
    if not m:
        return None
    segments = [s for s in url.path.split("/") if s]
    while segments and _LOCALE.match(segments[0]):
        segments.pop(0)
    if not segments or segments[0] in ("wday", "job"):
        return None
    return url.netloc.lower(), m.group("tenant"), segments[0]


def _posted_on(text: str | None, now):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' → approx date.
    'Posted 30+ Days Ago' gives no usable date."""
    if not text:
        return None
    m = _POSTED.search(text)
    if not m:
        return None
    days = 0 if m.group(1).lower() == "today" else 1 if m.group(1).lower() == "yesterday" else int(m.group(2))
    if "+" in text:
        return None
    day = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return day


class WorkdayCollector(Collector):
    name = "workday"

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        board = parse_board(url)
        if not board:
            return None
        host, tenant, site = board
        params = parse_qsl(url.query)
        search = next((v for k, v in params if k == "q"), "")
        facets: dict[str, list[str]] = {}
        for k, v in params:
            if k not in _NON_FACET_PARAMS and v:
                facets.setdefault(k, []).append(v)
        options = {"host": host, "tenant": tenant, "site": site}
        if search:
            options["search_text"] = search
        if facets:
            options["facets"] = facets
        return UrlDetection(adapter=cls.name, url=f"https://{host}/{site}", company_hint=tenant, filters=SourceFilters(),
                            options=options)

    def _api(self, source: SourceConfig) -> str:
        o = source.options
        if not all(o.get(k) for k in ("host", "tenant", "site")):
            board = parse_board(urlsplit(source.url))
            if not board:
                raise FetchError("parse_error", f"Not a Workday board URL: {source.url}")
            o = {**o, "host": board[0], "tenant": board[1], "site": board[2]}
            source.options.update(o)
        return f"https://{o['host']}/wday/cxs/{o['tenant']}/{o['site']}"

    def parse_page(self, payload: object, source: SourceConfig, now) -> tuple[list[NormalizedJob], int | None]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list):
            raise FetchError("parse_error", "Workday: response missing 'jobPostings'")
        host, site = source.options["host"], source.options["site"]
        jobs = []
        for item in payload["jobPostings"]:
            path = item.get("externalPath") if isinstance(item, dict) else None
            if not path or not item.get("title"):
                raise FetchError("parse_error", "Workday: posting without title/externalPath")
            bullets = item.get("bulletFields") or []
            req_id = bullets[0] if bullets and isinstance(bullets[0], str) else None
            url = f"https://{host}/{site}{path}"
            posted = _posted_on(item.get("postedOn"), now)
            jobs.append(NormalizedJob(
                external_id=req_id or path,
                title=clean_text(item["title"]) or item["title"],
                company=source.company, url=url, application_url=url,
                locations=[clean_text(item.get("locationsText"))] if item.get("locationsText") else [],
                published_at=posted, published_at_precision="day-approx" if posted else None,
                published_at_source="workday.postedOn.relative" if posted else None,
                extra={"posted_on": item.get("postedOn"), "path": path},
            ))
        total = payload.get("total")
        return jobs, total if isinstance(total, int) else None

    def apply_detail(self, job: NormalizedJob, payload: object) -> None:
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            raise FetchError("parse_error", "Workday: detail missing jobPostingInfo")
        job.description_text = html_to_text(info.get("jobDescription")) or job.description_text
        job.employment_type = clean_text(info.get("timeType")) or job.employment_type
        locs = [info.get("location")] + list(info.get("additionalLocations") or [])
        locs = [clean_text(l) for l in locs if isinstance(l, str) and clean_text(l)]
        if locs:
            job.locations = locs
        if info.get("remoteType"):
            job.workplace_type = clean_text(info["remoteType"])
        start, precision = parse_datetime(info.get("startDate"))
        if start:  # posting start date as entered by the employer
            job.published_at, job.published_at_precision = start, precision
            job.published_at_source = "workday.startDate"
        if isinstance(info.get("externalUrl"), str) and info["externalUrl"].startswith("https://"):
            job.application_url = info["externalUrl"]
        job.extra["details_fetched"] = True

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        api = self._api(source)
        client.ensure_allowed(api + "/jobs")
        max_pages = int(source.options.get("max_pages", 10))
        jobs: list[NormalizedJob] = []
        total: int | None = None
        for page in range(max_pages):
            body = {"appliedFacets": source.options.get("facets") or {}, "limit": 20, "offset": page * 20,
                    "searchText": source.options.get("search_text") or ""}
            page_jobs, page_total = self.parse_page(client.post_json(api + "/jobs", body), source, now)
            # Verified live: Workday reports the real total on the first page and 0 on later pages.
            if page == 0 or (page_total and not total):
                total = page_total
            jobs.extend(page_jobs)
            if len(page_jobs) < 20 or (total and len(jobs) >= total):
                break
        notes = []
        for job in jobs:
            if not ctx.needs_details(job.external_id) or ctx.detail_fetches >= ctx.max_detail_fetches:
                continue
            ctx.detail_fetches += 1
            try:
                self.apply_detail(job, client.get_json(api + job.extra["path"]))
            except FetchError as exc:
                notes.append(f"detail failed for {job.external_id}: {exc.kind}")
        seen: set[str] = set()
        jobs = [j for j in jobs if not (j.external_id in seen or seen.add(j.external_id))]
        complete = total is not None and len(jobs) >= total
        if not complete:
            notes.append(f"Read {len(jobs)} of {total} jobs (page cap); closure detection disabled. "
                         "Add a search term or filters to the URL to narrow it.")
        return FetchResult(jobs=jobs, complete=complete, total_reported=total, notes=notes)
