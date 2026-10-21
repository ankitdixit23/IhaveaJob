"""Watershed (watershed.com/careers) collector.

Verified response format (fetched from a GitHub Actions runner, 2026-10-21):

* ``/careers`` is server-rendered HTML. Jobs are grouped by department:
  ``<div id=...><h3>Department</h3><ul><li><a class="job-link"
  href="/careers/<8-hex-id>-<slug>"><span>Title</span>
  <div class="job-locations"><small>Location</small>...</div></a></li></ul></div>``.
  The location radio buttons filter in the browser only (no URL parameter).
* ``/careers/<id>-<slug>`` contains a schema.org ``JobPosting`` JSON-LD block
  with title, employmentUnit (department), employmentType, jobLocation,
  description (HTML), and ``datePosted`` (date only). ``validThrough`` is
  regenerated as now+~31 days on every request, so it is NOT used.
* The page links to Ashby (``jobs.ashbyhq.com/Watershed/<uuid>``); the 8-hex
  id is the last 8 hex digits of that Ashby UUID.
* robots.txt allows /careers.

The listing page is treated as complete when it parses into at least one
job and the department sections are present.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import SplitResult, urljoin

from bs4 import BeautifulSoup

from ..filters import passes_structural
from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig, SourceFilters
from ..normalization import clean_text, html_to_text, parse_datetime
from .base import CollectContext, Collector, UrlDetection

log = logging.getLogger(__name__)

_JOB_HREF = re.compile(r"^/careers/([0-9a-f]{8})-[^/?#]+$")
_EMPLOYMENT_TYPES = {
    "FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary", "INTERN": "Internship",
}


class WatershedCollector(Collector):
    name = "watershed"
    uses_html = True

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        host = url.netloc.lower().removeprefix("www.")
        if host != "watershed.com" or not re.match(r"^(/[a-z]{2}(-[A-Z]{2})?)?/careers/?$", url.path):
            return None
        return UrlDetection(
            adapter=cls.name,
            url="https://watershed.com/careers",
            company_hint="Watershed",
            filters=SourceFilters(),
            notes=["Watershed's location/department filters run in the browser and are not in the URL; "
                   "add them with --locations/--departments/--keywords."],
        )

    def parse_listing(self, html: str, base_url: str, company: str) -> list[NormalizedJob]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: dict[str, NormalizedJob] = {}
        for link in soup.select("a.job-link[href]"):
            href = link["href"]
            match = _JOB_HREF.match(href)
            if not match:
                continue
            ext_id = match.group(1)
            title_el = link.find("span")
            title = clean_text(title_el.get_text() if title_el else link.get_text())
            if not title:
                continue
            locations = [clean_text(s.get_text()) for s in link.select(".job-locations small")]
            section = link.find_parent("ul")
            heading = section.find_previous_sibling(["h2", "h3"]) if section else None
            jobs[ext_id] = NormalizedJob(
                external_id=ext_id,
                title=title,
                company=company,
                url=urljoin(base_url, href),
                application_url=urljoin(base_url, href),
                locations=[loc for loc in locations if loc],
                department=clean_text(heading.get_text()) if heading else None,
            )
        return list(jobs.values())

    @staticmethod
    def parse_detail(html: str) -> dict | None:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        return None

    def apply_detail(self, job: NormalizedJob, posting: dict) -> None:
        job.description_text = html_to_text(posting.get("description")) or job.description_text
        job.department = clean_text(posting.get("employmentUnit")) or job.department
        emp = posting.get("employmentType")
        if isinstance(emp, list):
            emp = emp[0] if emp else None
        if emp:
            job.employment_type = _EMPLOYMENT_TYPES.get(str(emp).upper(), clean_text(emp))
        if posting.get("jobLocationType") == "TELECOMMUTE":
            job.workplace_type = "Remote"
        published, precision = parse_datetime(posting.get("datePosted"))
        if published:
            job.published_at = published
            job.published_at_precision = precision
            job.published_at_source = "jsonld.datePosted"
        job.extra["details_fetched"] = True

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        html = client.get_text(source.url, check_robots=True)
        jobs = self.parse_listing(html, source.url, source.company)
        if not jobs:
            raise FetchError("parse_error", "No job links found on Watershed careers page (layout change?)", url=source.url)
        notes: list[str] = []
        skipped = 0
        for job in jobs:
            if not passes_structural(job, source.filters) or not ctx.needs_details(job.external_id):
                continue
            if ctx.detail_fetches >= ctx.max_detail_fetches:
                skipped += 1
                continue
            ctx.detail_fetches += 1
            try:
                detail_html = client.get_text(job.url, check_robots=True)
            except FetchError as exc:
                notes.append(f"detail fetch failed for {job.external_id}: {exc.kind}")
                continue
            posting = self.parse_detail(detail_html)
            if posting is None:
                notes.append(f"no JobPosting JSON-LD on {job.url}")
                continue
            self.apply_detail(job, posting)
        if skipped:
            notes.append(f"{skipped} detail fetches deferred to later runs (per-run cap)")
        return FetchResult(jobs=jobs, complete=True, total_reported=len(jobs), notes=notes)
