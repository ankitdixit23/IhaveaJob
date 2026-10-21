"""Collectors for public job-board APIs: Ashby, Greenhouse, Lever.

These follow each provider's public, unauthenticated job-board API
documentation. They are covered by unit tests with synthetic payloads but
have NOT yet been verified against a live board in this project; run
``ihaveajob check-source <url>`` before relying on one.

* Ashby:      GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
* Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
* Lever:      GET https://api.lever.co/v0/postings/{site}?mode=json

API responses list every open posting, so results are ``complete`` when the
response parses with the expected top-level shape.
"""

from __future__ import annotations

import html as htmllib
import re
from urllib.parse import SplitResult, parse_qsl, urlsplit

from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig, SourceFilters
from ..normalization import clean_text, html_to_text, parse_datetime
from .base import CollectContext, Collector, UrlDetection


def _first_path_segment(url: SplitResult) -> str | None:
    seg = [s for s in url.path.split("/") if s]
    return seg[0] if seg else None


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise FetchError("parse_error", message)


class AshbyCollector(Collector):
    name = "ashby"

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        if url.netloc.lower() != "jobs.ashbyhq.com":
            return None
        board = _first_path_segment(url)
        if not board:
            return None
        return UrlDetection(adapter=cls.name, url=f"https://jobs.ashbyhq.com/{board}", company_hint=board,
                            filters=SourceFilters(), options={"board": board})

    def parse(self, payload: object, company: str) -> FetchResult:
        _require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
                 "Ashby: response missing 'jobs' list")
        jobs = []
        for item in payload["jobs"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
                raise FetchError("parse_error", "Ashby: job without id/title")
            if item.get("isListed") is False:
                continue
            locations = [item.get("location")] + [
                (s or {}).get("location") for s in item.get("secondaryLocations") or [] if isinstance(s, dict)]
            published, precision = parse_datetime(item.get("publishedAt"))
            url = item.get("jobUrl") or f"https://jobs.ashbyhq.com/{company}/{item['id']}"
            jobs.append(NormalizedJob(
                external_id=str(item["id"]), title=clean_text(item["title"]) or item["title"], company=company,
                url=url, application_url=item.get("applyUrl") or url,
                locations=[clean_text(l) for l in locations if clean_text(l)],
                department=clean_text(item.get("department") or item.get("team")),
                employment_type=clean_text(item.get("employmentType")),
                workplace_type=clean_text(item.get("workplaceType")) or ("Remote" if item.get("isRemote") else None),
                description_text=(item.get("descriptionPlain") or "").strip()
                or html_to_text(item.get("descriptionHtml")),
                published_at=published, published_at_precision=precision,
                published_at_source="ashby.publishedAt" if published else None,
            ))
        return FetchResult(jobs=jobs, complete=True, total_reported=len(jobs))

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        board = source.options.get("board") or _first_path_segment(urlsplit(source.url))
        payload = client.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                                  params={"includeCompensation": "true"})
        return self.parse(payload, source.company)


class GreenhouseCollector(Collector):
    name = "greenhouse"

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        host = url.netloc.lower()
        if host not in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"):
            return None
        segs = [s for s in url.path.split("/") if s]
        if host == "boards-api.greenhouse.io":
            token = segs[2] if len(segs) >= 3 and segs[:2] == ["v1", "boards"] else None
        else:
            token = segs[0] if segs and segs[0] != "embed" else dict(parse_qsl(url.query)).get("for")
        if not token:
            return None
        return UrlDetection(adapter=cls.name, url=f"https://boards.greenhouse.io/{token}", company_hint=token,
                            filters=SourceFilters(), options={"board": token},
                            notes=["Greenhouse board tokens can be shared by unrelated companies with similar "
                                   "names (e.g. 'watershed' is Watershed Informatics). Check the company name."])

    def parse(self, payload: object, company: str) -> FetchResult:
        _require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
                 "Greenhouse: response missing 'jobs' list")
        jobs = []
        for item in payload["jobs"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
                raise FetchError("parse_error", "Greenhouse: job without id/title")
            content = item.get("content")
            published, precision = parse_datetime(item.get("first_published"))
            updated, _ = parse_datetime(item.get("updated_at"))
            depts = [d.get("name") for d in item.get("departments") or [] if isinstance(d, dict)]
            jobs.append(NormalizedJob(
                external_id=str(item["id"]), title=clean_text(item["title"]) or item["title"],
                company=clean_text(item.get("company_name")) or company,
                url=item.get("absolute_url") or "", application_url=item.get("absolute_url"),
                locations=[clean_text((item.get("location") or {}).get("name"))] if (item.get("location") or {}).get("name") else [],
                department=clean_text(", ".join(filter(None, depts))),
                description_text=html_to_text(htmllib.unescape(content)) if isinstance(content, str) else None,
                published_at=published, published_at_precision=precision,
                published_at_source="greenhouse.first_published" if published else None,
                source_updated_at=updated,
            ))
        meta_total = (payload.get("meta") or {}).get("total") if isinstance(payload.get("meta"), dict) else None
        complete = meta_total is None or meta_total <= len(jobs)
        return FetchResult(jobs=jobs, complete=complete, total_reported=meta_total)

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        board = source.options.get("board") or _first_path_segment(urlsplit(source.url))
        payload = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
        return self.parse(payload, source.company)


class LeverCollector(Collector):
    name = "lever"
    _URL_FILTERS = {"team": "departments", "department": "departments", "location": "locations"}

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:
        host = url.netloc.lower()
        if host not in ("jobs.lever.co", "jobs.eu.lever.co"):
            return None
        site = _first_path_segment(url)
        if not site:
            return None
        filters = SourceFilters()
        for key, value in parse_qsl(url.query):
            target = cls._URL_FILTERS.get(key.lower())
            if target and value:
                getattr(filters, target).append(value)
        region = "eu" if host == "jobs.eu.lever.co" else "global"
        return UrlDetection(adapter=cls.name, url=f"https://{host}/{site}", company_hint=site, filters=filters,
                            options={"site": site, "region": region})

    def parse(self, payload: object, company: str) -> FetchResult:
        _require(isinstance(payload, list), "Lever: expected a JSON list of postings")
        jobs = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("id") or not item.get("text"):
                raise FetchError("parse_error", "Lever: posting without id/text")
            cats = item.get("categories") or {}
            locations = cats.get("allLocations") or ([cats.get("location")] if cats.get("location") else [])
            published, precision = parse_datetime(item.get("createdAt"))
            desc = item.get("descriptionPlain") or html_to_text(item.get("description")) or ""
            for block in item.get("lists") or []:
                if isinstance(block, dict):
                    desc += f"\n\n{block.get('text', '')}\n{html_to_text(block.get('content')) or ''}"
            if item.get("additionalPlain"):
                desc += "\n\n" + item["additionalPlain"]
            jobs.append(NormalizedJob(
                external_id=str(item["id"]), title=clean_text(item["text"]) or item["text"], company=company,
                url=item.get("hostedUrl") or "", application_url=item.get("applyUrl") or item.get("hostedUrl"),
                locations=[clean_text(l) for l in locations if clean_text(l)],
                department=clean_text(cats.get("team") or cats.get("department")),
                employment_type=clean_text(cats.get("commitment")),
                workplace_type=clean_text(item.get("workplaceType")),
                description_text=desc.strip() or None,
                published_at=published, published_at_precision=precision,
                published_at_source="lever.createdAt" if published else None,
            ))
        return FetchResult(jobs=jobs, complete=True, total_reported=len(jobs))

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        site = source.options.get("site") or _first_path_segment(urlsplit(source.url))
        host = "api.eu.lever.co" if source.options.get("region") == "eu" else "api.lever.co"
        payload = client.get_json(f"https://{host}/v0/postings/{site}", params={"mode": "json"})
        return self.parse(payload, source.company)
