"""Apply a source's SourceFilters (the user's search/sort choices) to jobs."""

from __future__ import annotations

from .models import NormalizedJob, SourceFilters


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(n.lower() in haystack for n in needles)


def passes_structural(job: NormalizedJob, filters: SourceFilters) -> bool:
    """Department/location checks: usable on listing data alone."""
    if filters.departments and not _contains_any((job.department or "").lower(), filters.departments):
        return False
    if filters.locations:
        loc_text = " | ".join(job.locations or [job.extra.get("card_text") or ""]).lower()
        if job.workplace_type:
            loc_text += " | " + job.workplace_type.lower()
        if not _contains_any(loc_text, filters.locations):
            return False
    return True


def passes_keywords(job: NormalizedJob, filters: SourceFilters) -> bool:
    text = " ".join(filter(None, [job.title, job.department, job.description_text,
                                  job.extra.get("card_text")])).lower()
    if filters.keywords_exclude and _contains_any(" ".join(filter(None, [job.title, job.department])).lower(),
                                                   filters.keywords_exclude):
        return False
    if filters.keywords_any and not _contains_any(text, filters.keywords_any):
        return False
    return True


def matches(job: NormalizedJob, filters: SourceFilters) -> bool:
    return passes_structural(job, filters) and passes_keywords(job, filters)
