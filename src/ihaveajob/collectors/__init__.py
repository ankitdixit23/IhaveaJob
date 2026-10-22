"""Collector registry and careers-URL detection.

To support a new site, write a Collector subclass with ``name``, ``fetch``
and ``detect`` and add it to ``COLLECTORS``. No monitor changes are needed.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from .ats import AshbyCollector, GreenhouseCollector, LeverCollector
from .base import CollectContext, Collector, UrlDetection
from .generic import GenericPageCollector
from .google_careers import GoogleCareersCollector
from .watershed import WatershedCollector
from .workday import WorkdayCollector
from ..models import SourceFilters

# Specialised collectors first; GenericPageCollector ("auto") handles everything else.
COLLECTORS: dict[str, type[Collector]] = {
    c.name: c
    for c in (WatershedCollector, GoogleCareersCollector, WorkdayCollector, AshbyCollector, GreenhouseCollector,
              LeverCollector, GenericPageCollector)
}


class UnsupportedUrl(ValueError):
    pass


def get_collector(adapter: str) -> Collector:
    try:
        return COLLECTORS[adapter]()
    except KeyError:
        raise ValueError(f"Unknown adapter '{adapter}'. Known: {', '.join(sorted(COLLECTORS))}") from None


def detect_source(url: str) -> UrlDetection:
    """Work out how to read a careers URL. Any public web page is accepted:
    known job systems get a specialised reader, everything else the universal
    ``auto`` reader."""
    raw = url.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc or "." not in parts.netloc:
        raise UnsupportedUrl(f"Not a web URL: {url!r}")
    for collector in COLLECTORS.values():
        detection = collector.detect(parts)
        if detection:
            return detection
    from .generic import _site

    label = _site(parts.netloc).split(".")[0].replace("-", " ")
    company = label.upper() if len(label) <= 3 else label.title()  # wri -> WRI, microsoft -> Microsoft
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return UrlDetection(adapter="auto", url=clean, company_hint=company, filters=SourceFilters(),
                        notes=["Using the universal reader: it finds job links, structured job data or an embedded "
                               "job board on the page, and falls back to watching the page text."])


__all__ = ["COLLECTORS", "CollectContext", "Collector", "UnsupportedUrl", "UrlDetection", "detect_source", "get_collector"]
