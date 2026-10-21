"""schema.org JobPosting (JSON-LD) helpers shared by several collectors.

Many careers sites embed JobPosting data for search engines. It is the most
reliable structured source on an arbitrary page.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from bs4 import BeautifulSoup

from ..models import NormalizedJob
from ..normalization import clean_text, html_to_text, parse_datetime

EMPLOYMENT_TYPES = {
    "FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary", "INTERN": "Internship", "PER_DIEM": "Per diem", "VOLUNTEER": "Volunteer",
}


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        types = node.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key])


def find_job_postings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    postings: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        postings.extend(_walk(data))
    return postings


def posting_locations(posting: dict) -> list[str]:
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    out: list[str] = []
    for loc in locs if isinstance(locs, list) else []:
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, str):
            out.append(clean_text(addr) or "")
        elif isinstance(addr, dict):
            parts = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            text = ", ".join(clean_text(p) for p in parts if clean_text(p))
            if text:
                out.append(text)
    if posting.get("jobLocationType") == "TELECOMMUTE":
        out.append("Remote")
    return [o for o in out if o]


def apply_posting(job: NormalizedJob, posting: dict, *, use_locations: bool = True) -> None:
    """Enrich a job with JobPosting data. Never erases existing values."""
    job.title = clean_text(posting.get("title")) or job.title
    job.description_text = html_to_text(posting.get("description")) or job.description_text
    dept = posting.get("employmentUnit") or (posting.get("occupationalCategory") if isinstance(
        posting.get("occupationalCategory"), str) else None)
    job.department = clean_text(dept) or job.department
    emp = posting.get("employmentType")
    if isinstance(emp, list):
        emp = emp[0] if emp else None
    if emp:
        job.employment_type = EMPLOYMENT_TYPES.get(str(emp).upper(), clean_text(emp))
    if posting.get("jobLocationType") == "TELECOMMUTE":
        job.workplace_type = "Remote"
    if use_locations:
        locs = posting_locations(posting)
        if locs:
            job.locations = locs
    published, precision = parse_datetime(posting.get("datePosted"))
    if published:
        job.published_at = published
        job.published_at_precision = precision
        job.published_at_source = "jsonld.datePosted"
    job.extra["details_fetched"] = True
