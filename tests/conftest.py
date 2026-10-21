"""Shared test helpers. No test touches the network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ihaveajob.config import Settings
from ihaveajob.database import make_engine, session_factory, upgrade
from ihaveajob.http import FetchError, HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeClient(HttpClient):
    """HttpClient whose responses come from a dict: url -> str | object | FetchError."""

    def __init__(self, routes: dict[str, Any]):
        super().__init__(sleep=lambda _s: None)
        self.routes = routes
        self.requested: list[str] = []

    def _lookup(self, url: str) -> Any:
        self.request_count += 1
        self.requested.append(url)
        if url not in self.routes:
            raise FetchError("http_status", f"HTTP 404 from {url}", url=url, status=404)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value

    def get_text(self, url: str, *, check_robots: bool = False, params=None) -> str:
        return self._lookup(url)

    def get_json(self, url: str, *, params=None) -> Any:
        return self._lookup(url)


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    upgrade(engine)  # exercises the real migrations
    return session_factory(engine)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path}/test.db", close_after_misses=3, close_after_hours=6,
                    max_detail_fetches=25, detail_refresh_hours=24)


@pytest.fixture
def clock() -> Clock:
    return Clock()
