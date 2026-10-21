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


def load_sources(path: Path = DEFAULT_CONFIG_PATH) -> list[SourceConfig]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("sources")
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a top-level 'sources:' list")
    from .collectors import COLLECTORS  # local import to avoid a cycle

    seen: set[str] = set()
    sources = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: sources[{i}] must be a mapping")
        missing = [k for k in ("key", "company", "adapter", "url") if not entry.get(k)]
        if missing:
            raise ConfigError(f"{path}: sources[{i}] missing {', '.join(missing)}")
        key = str(entry["key"])
        if not _KEY_RE.match(key):
            raise ConfigError(f"{path}: invalid key {key!r} (lowercase letters, digits, - and _)")
        if key in seen:
            raise ConfigError(f"{path}: duplicate key {key!r}")
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


def append_source(source: SourceConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Add a source to the YAML file, keeping existing entries and comments' order."""
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    data = data or {"sources": []}
    if any(s.get("key") == source.key for s in data.get("sources", [])):
        raise ConfigError(f"A source with key {source.key!r} already exists in {path}")
    entry: dict = {"key": source.key, "company": source.company, "adapter": source.adapter, "url": source.url}
    if not source.filters.is_empty():
        entry["filters"] = source.filters.to_dict()
    if source.options:
        entry["options"] = source.options
    entry["enabled"] = source.enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else "sources:\n"
    text = re.sub(r"^sources:\s*\[\]\s*$", "sources:", text, flags=re.M)
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True)
    block = "\n".join("  " + line if line else line for line in block.splitlines())
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + block + "\n")
    load_sources(path)  # validate the result
