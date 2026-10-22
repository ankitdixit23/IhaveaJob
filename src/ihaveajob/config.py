"""Load/save config/companies.yaml and environment settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import SourceConfig, SourceFilters

DEFAULT_CONFIG_PATH = Path(os.getenv("IHAVEAJOB_COMPANIES_FILE", "config/companies.yaml"))


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    database_url: str
    close_after_misses: int
    close_after_hours: float
    max_detail_fetches: int
    detail_refresh_hours: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL") or "sqlite:///data/ihaveajob.db",
            close_after_misses=int(os.getenv("IHAVEAJOB_CLOSE_AFTER_MISSES", "3")),
            close_after_hours=float(os.getenv("IHAVEAJOB_CLOSE_AFTER_HOURS", "6")),
            max_detail_fetches=int(os.getenv("IHAVEAJOB_MAX_DETAIL_FETCHES", "25")),
            detail_refresh_hours=float(os.getenv("IHAVEAJOB_DETAIL_REFRESH_HOURS", "24")),
        )


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "source"


def default_key(company: str, url: str) -> str:
    """Readable, stable key: company plus any search term in the URL."""
    from urllib.parse import parse_qsl, urlsplit

    params = dict(parse_qsl(urlsplit(url).query))
    term = next((params[k] for k in ("q", "query", "search", "keyword", "keywords", "base_query", "searchText")
                 if params.get(k)), None)
    return slugify(f"{company}-{term}" if term else company)


def load_sources(path: Path = DEFAULT_CONFIG_PATH) -> list[SourceConfig]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    return parse_sources(path.read_text(), str(path))


def parse_sources(text: str, path: str = "companies.yaml") -> list[SourceConfig]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    entries = data.get("sources")
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a top-level 'sources:' list")
    from .collectors import COLLECTORS  # local import to avoid a cycle

    seen: set[str] = set()
    sources = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: sources[{i}] must be a mapping")
        if not entry.get("url"):
            raise ConfigError(f"{path}: sources[{i}] missing url")
        if entry.get("name") and not entry.get("company"):
            entry = {**entry, "company": entry["name"]}
        # Only `url` is required: everything else can be worked out from it.
        if not entry.get("adapter") or not entry.get("company") or not entry.get("key"):
            from .collectors import UnsupportedUrl, detect_source

            try:
                det = detect_source(str(entry["url"]))
            except UnsupportedUrl as exc:
                raise ConfigError(f"{path}: sources[{i}]: {exc}") from exc
            entry = dict(entry)
            if not entry.get("adapter"):
                entry["adapter"], entry["url"] = det.adapter, det.url
                entry["options"] = {**det.options, **(entry.get("options") or {})}
                if det.filters.to_dict() and not entry.get("filters"):
                    entry["filters"] = det.filters.to_dict()
            if not entry.get("company"):
                entry["company"] = det.company_hint or "Company"
            if not entry.get("key"):
                entry["key"] = default_key(str(entry["company"]), str(entry["url"]))
        key = str(entry["key"])
        if not _KEY_RE.match(key):
            raise ConfigError(f"{path}: invalid key {key!r} (lowercase letters, digits, - and _)")
        if key in seen:
            raise ConfigError(f"{path}: duplicate key {key!r}: give one of them a different `key:`")
        seen.add(key)
        if entry["adapter"] not in COLLECTORS:
            raise ConfigError(f"{path}: {key}: unknown adapter {entry['adapter']!r}")
        sources.append(SourceConfig(
            key=key,
            company=str(entry["company"]),
            adapter=str(entry["adapter"]),
            url=str(entry["url"]),
            filters=SourceFilters.from_dict(entry.get("filters")),
            enabled=bool(entry.get("enabled", True)),
            options=dict(entry.get("options") or {}),
        ))
    return sources


def _entry(source: SourceConfig) -> dict:
    entry: dict = {"key": source.key, "company": source.company, "adapter": source.adapter, "url": source.url}
    if not source.filters.is_empty():
        entry["filters"] = source.filters.to_dict()
    if source.options:
        entry["options"] = source.options
    entry["enabled"] = source.enabled
    return entry


def add_source_text(text: str, source: SourceConfig) -> str:
    """Return the YAML text with ``source`` appended (comments and order kept)."""
    data = (yaml.safe_load(text) if text.strip() else None) or {"sources": []}
    existing = data.get("sources") or []
    if any(s.get("key") == source.key for s in existing):
        raise ConfigError(f"A page with key {source.key!r} already exists")
    if any(str(s.get("url", "")).rstrip("/") == source.url.rstrip("/") for s in existing):
        raise ConfigError("This page is already being monitored")
    text = text if text.strip() else "sources:\n"
    text = re.sub(r"^sources:\s*\[\]\s*$", "sources:", text, flags=re.M)
    block = yaml.safe_dump([_entry(source)], sort_keys=False, allow_unicode=True)
    block = "\n".join("  " + line if line else line for line in block.splitlines())
    if not text.endswith("\n"):
        text += "\n"
    result = text + block + "\n"
    parse_sources(result)  # validate
    return result


def edit_sources_text(text: str, edit) -> str:
    """Apply ``edit(list_of_entry_dicts)`` (which mutates in place) and re-serialise
    the sources list, keeping the comment header above ``sources:``."""
    data = yaml.safe_load(text) or {}
    entries = list(data.get("sources") or [])
    edit(entries)
    match = re.search(r"^sources:", text, flags=re.M)
    header = text[: match.start()] if match else ""
    if entries:
        dumped = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True)
        body = "sources:\n" + "\n".join("  " + line if line else line for line in dumped.splitlines()) + "\n"
    else:
        body = "sources: []\n"
    result = header + body
    parse_sources(result)
    return result


def append_source(source: SourceConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Add a source to the YAML file, keeping existing entries and comments' order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    path.write_text(add_source_text(text, source))


def build_source(url: str, *, company: str | None = None, key: str | None = None,
                 filters: SourceFilters | None = None) -> tuple[SourceConfig, list[str]]:
    """Turn a pasted careers URL (+ optional name/filters) into a SourceConfig."""
    from .collectors import detect_source

    det = detect_source(url)
    merged = SourceFilters.from_dict({**det.filters.to_dict()})
    if filters:
        for name in ("keywords_any", "keywords_exclude", "departments", "locations"):
            getattr(merged, name).extend(v for v in getattr(filters, name) if v not in getattr(merged, name))
    name = (company or "").strip() or det.company_hint or "Company"
    return SourceConfig(key=key or default_key(name, url), company=name, adapter=det.adapter, url=det.url,
                        filters=merged, options=dict(det.options)), list(det.notes)
