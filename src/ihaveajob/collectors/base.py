"""Collector interface and run context."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import SplitResult

from ..http import HttpClient
from ..models import FetchResult, SourceConfig, SourceFilters


@dataclass
class CollectContext:
    """Per-run hints the monitor gives a collector.

    ``needs_details(external_id)`` tells collectors that load descriptions from
    per-job pages whether a detail fetch is due (new job, or stale details).
    ``max_detail_fetches`` caps per-run requests to a site.
    """

    needs_details: Callable[[str], bool] = lambda _id: True
    max_detail_fetches: int = 25
    detail_fetches: int = field(default=0)


@dataclass
class UrlDetection:
    adapter: str
    url: str  # URL to monitor (normalized; pagination removed)
    company_hint: str | None
    filters: SourceFilters
    options: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Collector(ABC):
    name: str = ""
    #: True when this collector parses HTML pages (robots.txt is enforced).
    uses_html: bool = False

    @abstractmethod
    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult: ...

    @classmethod
    def detect(cls, url: SplitResult) -> UrlDetection | None:  # pragma: no cover - default
        return None
