"""Polite HTTP client: timeouts, retries with backoff, Retry-After, per-host
rate limiting, response-size caps and robots.txt checks for HTML sources."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .robots import RobotsPolicy

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "IhaveaJobBot/0.1 (+https://github.com/ankitdixit23/IhaveaJob)"


class FetchError(Exception):
    """A request failed. ``kind`` is one of: timeout, connection, http_status,
    not_found (404/410), blocked (401/403, bot protection), rate_limited,
    robots_disallowed, too_large, parse_error, render_unavailable, render_error."""

    def __init__(self, kind: str, message: str, url: str | None = None, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status = status


class HttpClient:
    def __init__(
        self,
        user_agent: str | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        max_attempts: int = 3,
        min_interval_per_host: float = 1.0,
        max_bytes: int = 8 * 1024 * 1024,
        max_retry_after: float = 60.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.user_agent = user_agent or os.getenv("IHAVEAJOB_USER_AGENT") or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.min_interval = min_interval_per_host
        self.max_bytes = max_bytes
        self.max_retry_after = max_retry_after
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotsPolicy | None] = {}
        self.request_count = 0

    # -- public API -------------------------------------------------------
    def get_text(self, url: str, *, check_robots: bool = False, params: dict[str, Any] | None = None) -> str:
        if check_robots:
            self.ensure_allowed(url)
        return self._get(url, params=params).text

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        resp = self._get(url, params=params, accept="application/json")
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError("parse_error", f"Malformed JSON from {url}: {exc}", url=url) from exc

    def post_json(self, url: str, body: Any) -> Any:
        """POST a JSON body to a public read-only search endpoint (e.g. Workday)."""
        resp = self._get(url, accept="application/json", method="POST", json_body=body)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError("parse_error", f"Malformed JSON from {url}: {exc}", url=url) from exc

    def get_page(self, url: str) -> tuple[str, str]:
        """Fetch an HTML page (robots-checked). Returns (final_url, text)."""
        self.ensure_allowed(url)
        resp = self._get(url)
        final = getattr(resp, "url", None) or url
        if final != url:
            self.ensure_allowed(final)
        return final, resp.text

    def ensure_allowed(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                text = self._get(origin + "/robots.txt").text
                self._robots[origin] = RobotsPolicy(text)
            except FetchError as exc:
                # 4xx on robots.txt => no restrictions (RFC 9309). Anything else
                # (5xx/timeout) => be conservative and treat as disallowed.
                if exc.status and 400 <= exc.status < 500:
                    self._robots[origin] = None
                else:
                    raise FetchError("robots_disallowed", f"robots.txt unavailable for {origin}: {exc}", url=url) from exc
        policy = self._robots[origin]
        if policy is not None and not policy.is_allowed(url, self.user_agent):
            raise FetchError("robots_disallowed", f"robots.txt disallows {url}", url=url)

    # -- internals --------------------------------------------------------
    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = time.monotonic()

    def _get(self, url: str, params: dict[str, Any] | None = None, accept: str | None = None,
             method: str = "GET", json_body: Any = None) -> requests.Response:
        host = urlsplit(url).netloc
        headers = {"Accept": accept} if accept else None
        last_error: FetchError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(host)
            self.request_count += 1
            try:
                if method == "POST":
                    resp = self.session.post(url, json=json_body, headers=headers, timeout=self.timeout, stream=True)
                else:
                    resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=True)
                body = resp.raw.read(self.max_bytes + 1, decode_content=True) if resp.raw is not None else resp.content
                if len(body) > self.max_bytes:
                    resp.close()
                    raise FetchError("too_large", f"Response larger than {self.max_bytes} bytes", url=url)
                resp._content = body  # make .text/.json() use the capped body
            except requests.Timeout as exc:
                last_error = FetchError("timeout", f"Timeout fetching {url}: {exc}", url=url)
            except requests.ConnectionError as exc:
                last_error = FetchError("connection", f"Connection error fetching {url}: {exc}", url=url)
            else:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    kind = "rate_limited" if resp.status_code == 429 else "http_status"
                    last_error = FetchError(kind, f"HTTP {resp.status_code} from {url}", url=url, status=resp.status_code)
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    if retry_after is not None and attempt < self.max_attempts:
                        if retry_after > self.max_retry_after:
                            raise last_error  # don't block a scheduled run for minutes
                        log.warning("retry_after", extra={"url": url, "seconds": retry_after})
                        self._sleep(retry_after)
                        continue
                elif resp.status_code >= 400:
                    code = resp.status_code
                    kind = "not_found" if code in (404, 410) else "blocked" if code in (401, 403) else "http_status"
                    raise FetchError(kind, f"HTTP {code} from {url}", url=url, status=code)
                else:
                    return resp
            if attempt < self.max_attempts:
                backoff = min(2 ** (attempt - 1) * 2.0, 30.0)
                log.warning(
                    "fetch_retry",
                    extra={"url": url, "attempt": attempt, "error": str(last_error), "backoff_s": backoff},
                )
                self._sleep(backoff)
        assert last_error is not None
        raise last_error


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
