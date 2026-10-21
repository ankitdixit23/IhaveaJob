"""Collector registry and careers-URL detection.

To support a new site, write a Collector subclass with ``name``, ``fetch``
and ``detect`` and add it to ``COLLECTORS``. No monitor changes are needed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .ats import AshbyCollector, GreenhouseCollector, LeverCollector
from .base import CollectContext, Collector, UrlDetection
from .google_careers import GoogleCareersCollector
from .watershed import WatershedCollector

COLLECTORS: dict[str, type[Collector]] = {
    c.name: c
    for c in (WatershedCollector, GoogleCareersCollector, AshbyCollector, GreenhouseCollector, LeverCollector)
}


class UnsupportedUrl(ValueError):
    pass


def get_collector(adapter: str) -> Collector:
    try:
        return COLLECTORS[adapter]()
    except KeyError:
        raise ValueError(f"Unknown adapter '{adapter}'. Known: {', '.join(sorted(COLLECTORS))}") from None


def detect_source(url: str) -> UrlDetection:
    """Work out which collector handles a careers URL the user pasted."""
    raw = url.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UnsupportedUrl(f"Not a web URL: {url!r}")
    for collector in COLLECTORS.values():
        detection = collector.detect(parts)
        if detection:
            return detection
    raise UnsupportedUrl(
        f"No collector supports {parts.netloc} yet. Supported: watershed.com/careers, Google Careers results, "
        "jobs.ashbyhq.com, boards.greenhouse.io / job-boards.greenhouse.io, jobs.lever.co. "
        "Many company sites embed one of these boards: open a job and check where 'Apply' leads."
    )


__all__ = ["COLLECTORS", "CollectContext", "Collector", "UnsupportedUrl", "UrlDetection", "detect_source", "get_collector"]
