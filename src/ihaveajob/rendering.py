"""Optional headless-browser rendering for careers pages built with JavaScript.

Uses Playwright/Chromium when installed (it is in the GitHub Actions
workflow; locally: ``pip install ".[render]" && playwright install chromium``).
The page URL is robots.txt-checked first. Images, fonts and media are blocked
to keep load on the site low. Without Playwright, ``render`` raises
``FetchError("render_unavailable")`` and callers fall back gracefully.
"""

from __future__ import annotations

import logging
import os

from .http import FetchError, HttpClient

log = logging.getLogger(__name__)

_BLOCKED_TYPES = {"image", "font", "media"}


def rendering_available() -> bool:
    if os.getenv("IHAVEAJOB_DISABLE_RENDER") == "1":
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def render(url: str, client: HttpClient, *, timeout_ms: int = 45000, settle_ms: int = 2500) -> tuple[str, str, str]:
    """Return (final_url, html, visible_text) after JavaScript has run."""
    if not rendering_available():
        raise FetchError("render_unavailable", "Headless browser (Playwright) is not installed", url=url)
    client.ensure_allowed(url)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    client.request_count += 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context(user_agent=client.user_agent)
                page = context.new_page()
                page.route("**/*", lambda route: route.abort()
                           if route.request.resource_type in _BLOCKED_TYPES else route.continue_())
                # Many careers sites never go fully network-idle (analytics beacons),
                # so load the DOM, then wait for idle only up to a short limit.
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = resp.status if resp else None
                if status is not None and status >= 400:
                    raise FetchError("blocked" if status in (401, 403, 429) else "http_status",
                                     f"HTTP {status} from {url} (headless browser)", url=url, status=status)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightError:
                    pass  # still busy: use what has rendered so far
                page.wait_for_timeout(settle_ms)
                return page.url, page.content(), page.inner_text("body")
            finally:
                browser.close()
    except PlaywrightError as exc:
        kind = "timeout" if "Timeout" in str(exc) else "render_error"
        raise FetchError(kind, f"Headless browser failed on {url}: {str(exc)[:300]}", url=url) from exc
