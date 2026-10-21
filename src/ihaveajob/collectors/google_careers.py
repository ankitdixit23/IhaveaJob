"""Google Careers search-results collector.

Verified response format (fetched from a GitHub Actions runner, 2026-10-21):

* ``/about/careers/applications/jobs/results?q=...`` is server-rendered and
  embeds an ``AF_initDataCallback({key: 'ds:1', ..., data: [...]})`` block.
  ``data = [jobs, null, total_matches, page_size]``; each job is a list:
  0 id, 1 title, 2 apply/sign-in URL, 3 [_, responsibilities HTML],
  4 [_, qualifications HTML], 7 company name, 9 locations
  ([display, [address], city, postcode, region, country], ...),
  10 [_, about-the-job HTML], 12/13/14 [epoch_seconds, nanos].
  Records may be shorter (20 vs 21 items), so every index is read defensively.
* Comparing records shows 12 <= 13 == 14 with 13/14 moving on edits, so 12 is
  treated as the creation/publication time. Google does not document this,
  hence ``published_at_source = "google.created_ts.inferred"``.
* Field positions are undocumented and may change; a parse failure fails the
  run (no closures) instead of guessing.
* robots.txt (User-agent: *) allows ``.../jobs/results`` but DISALLOWS any
  ``page=`` parameter, so only the first page (20 results) is fetched. When
  ``total_matches`` exceeds the jobs returned, the result is marked
  incomplete and closure detection is disabled for the run.
* Per-job URLs: ``.../jobs/results/<id>-<slug>``. Search filters chosen on
  the site (q, location, sort, ...) live in the URL and are passed through.
"""

from __future__ import annotations

import json
import re
from urllib.parse import SplitResult, parse_qsl, urlencode, urlunsplit

from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig, SourceFilters
from ..normalization import clean_text, html_to_text, parse_datetime
from .base import CollectContext, Collector, UrlDetection

_RESULTS_PATH = "/about/careers/applications/jobs/results"
_DS1 = re.compile(r"AF_initDataCallback\(\{key: 'ds:1',.*?data:(.*?), sideChannel: \{\}\}\);\s*</script>", re.S)


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _at(seq: object, *path: int) -> object:
    cur = seq
    for idx in path:
        if not isinstance(cur, list) or len(cur) <= idx:
            return None
        cur = cur[idx]
    return cur


def _ts(value: object):
    seconds = _at(value, 0)
    if isinstance(seconds, (int, float)) and seconds > 0:
        return parse_datetime(int(seconds))[0]
    return None


class GoogleCareersCollector(Collector):
    name = "google_careers"
    uses_html = True

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        if url.netloc.lower() not in ("www.google.com", "google.com", "careers.google.com"):
            return None
        if not url.path.rstrip("/").endswith("/jobs/results"):
            return None
        params = [(k, v) for k, v in parse_qsl(url.query) if k != "page"]
        clean = urlunsplit(("https", "www.google.com", _RESULTS_PATH, urlencode(params), ""))
        notes = ["Only the first page (20 jobs) is monitored: Google's robots.txt disallows paginated results. "
                 "Narrow your search (keyword + location) so relevant jobs fit on page 1."]
        if any(k == "page" for k, _ in parse_qsl(url.query)):
            notes.append("Removed the page= parameter from the URL.")
        return UrlDetection(adapter=cls.name, url=clean, company_hint="Google", filters=SourceFilters(), notes=notes)

    def parse(self, html: str, company_default: str) -> FetchResult:
        match = _DS1.search(html)
        if not match:
            raise FetchError("parse_error", "Google results page: embedded ds:1 data block not found (format change?)")
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise FetchError("parse_error", f"Google results page: ds:1 data is not valid JSON: {exc}") from exc
        records = _at(data, 0)
        if records is None:
            records = []
        if not isinstance(records, list):
            raise FetchError("parse_error", "Google results page: unexpected ds:1 structure")
        total = _at(data, 2)
        jobs: list[NormalizedJob] = []
        for rec in records:
            ext_id, title = _at(rec, 0), _at(rec, 1)
            if not isinstance(ext_id, str) or not ext_id.isdigit() or not isinstance(title, str):
                raise FetchError("parse_error", "Google results page: job record missing id/title (format change?)")
            parts = []
            for idx, heading in ((10, "About the job"), (3, "Responsibilities"), (4, "Qualifications")):
                section = _at(rec, idx, 1)
                if isinstance(section, str) and section.strip():
                    parts.append(f"{heading}\n{html_to_text(section)}")
            locations = []
            for loc in _at(rec, 9) or []:
                name = _at(loc, 0)
                if isinstance(name, str):
                    locations.append(clean_text(name))
            created, updated = _ts(_at(rec, 12)), _ts(_at(rec, 14)) or _ts(_at(rec, 13))
            company = _at(rec, 7)
            url = f"https://www.google.com{_RESULTS_PATH}/{ext_id}-{_slugify(title)}"
            apply_url = _at(rec, 2)
            jobs.append(NormalizedJob(
                external_id=ext_id,
                title=clean_text(title) or title,
                company=company if isinstance(company, str) and company else company_default,
                url=url,
                # The record's own apply link is a sign-in redirect with an opaque
                # token; the public job page is the stable link to share.
                application_url=url,
                extra={"signin_apply_url": apply_url} if isinstance(apply_url, str) else {},
                locations=[loc for loc in locations if loc],
                description_text="\n\n".join(parts) or None,
                published_at=created,
                published_at_source="google.created_ts.inferred" if created else None,
                published_at_precision="second" if created else None,
                source_updated_at=updated,
            ))
        complete = isinstance(total, int) and total <= len(jobs)
        notes = []
        if not complete:
            notes.append(f"Showing {len(jobs)} of {total} matches (first page only); closure detection disabled.")
        return FetchResult(jobs=jobs, complete=complete, total_reported=total if isinstance(total, int) else None,
                           notes=notes)

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        html = client.get_text(source.url, check_robots=True)
        return self.parse(html, source.company)
