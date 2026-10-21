"""Domain objects shared by collectors, filters and the monitor service.

These are plain dataclasses (not ORM rows) so collectors stay independent of
the database layer and are easy to test with synthetic data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SourceFilters:
    """What the user searched/filtered for on the careers page.

    Empty lists mean "no restriction". Matching is case-insensitive substring
    matching; keywords match title, department and (when available) description.
    """

    keywords_any: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SourceFilters":
        data = data or {}

        def _list(key: str) -> list[str]:
            value = data.get(key) or []
            if isinstance(value, str):
                value = [value]
            return [str(v).strip() for v in value if str(v).strip()]

        return cls(
            keywords_any=_list("keywords_any"),
            keywords_exclude=_list("keywords_exclude"),
            departments=_list("departments"),
            locations=_list("locations"),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            k: v
            for k, v in {
                "keywords_any": self.keywords_any,
                "keywords_exclude": self.keywords_exclude,
                "departments": self.departments,
                "locations": self.locations,
            }.items()
            if v
        }

    def is_empty(self) -> bool:
        return not (self.keywords_any or self.keywords_exclude or self.departments or self.locations)


@dataclass
class SourceConfig:
    """One monitored careers page, as configured in config/companies.yaml."""

    key: str
    company: str
    adapter: str  # "auto" = universal reader (works with any careers page)
    url: str
    filters: SourceFilters = field(default_factory=SourceFilters)
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedJob:
    """A job in the common shape every collector must produce."""

    external_id: str
    title: str
    company: str
    url: str
    application_url: str | None = None
    locations: list[str] = field(default_factory=list)
    department: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    description_text: str | None = None
    published_at: datetime | None = None
    # Where published_at came from and how precise it is, e.g.
    # ("jsonld.datePosted", "date") or ("google.created_ts.inferred", "second").
    published_at_source: str | None = None
    published_at_precision: str | None = None
    source_updated_at: datetime | None = None
    # Free-form source-specific extras (kept small; stored as JSON).
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str | None:
        return "; ".join(self.locations) if self.locations else None


@dataclass
class FetchResult:
    """What a collector returns for one source.

    ``complete`` must only be True when the collector is confident it saw every
    currently open job for this source (after the source-side filter, but
    before our own SourceFilters). Closure detection is skipped otherwise, so
    a partial/capped/failed response can never produce false "closed" alerts.
    """

    jobs: list[NormalizedJob]
    complete: bool
    total_reported: int | None = None
    notes: list[str] = field(default_factory=list)
    # When no individual jobs can be identified: the page's visible text lines,
    # snapshotted so line-level changes can be reported.
    page_text: list[str] | None = None
    # How a generic ("auto") source was read this time, e.g. {"mode": "links:page"}.
    resolved: dict[str, Any] | None = None
