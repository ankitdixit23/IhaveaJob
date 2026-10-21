"""Text, URL and date normalization helpers used by all collectors."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

# Query parameters that never identify a job (tracking, pagination, search echo).
_DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "source", "ref", "referrer", "q", "page", "lever-source", "lever-origin",
}
_KEEP_PARAMS = {"gh_jid"}  # Greenhouse job id on embedded boards


def html_to_text(value: str | None) -> str | None:
    """Convert untrusted HTML to plain text. Scripts/styles are dropped."""
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "div", "ul", "ol"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip() or None


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", html.unescape(str(value))).strip()
    return value or None


def canonical_url(url: str | None) -> str | None:
    """Canonical form for dedup: https, lowercase host, no fragment, no
    tracking/search params, no trailing slash, sorted remaining params."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = parts.netloc.lower()
    if host.startswith("www.") and host != "www.google.com":
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() in _KEEP_PARAMS or (k.lower() not in _DROP_PARAMS and not k.lower().startswith("utm_"))]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def parse_datetime(value: object) -> tuple[datetime | None, str | None]:
    """Parse ISO strings, dates, epoch seconds/millis. Returns (utc datetime, precision)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), "second"
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc), "date"
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value  # millis vs seconds
        return datetime.fromtimestamp(seconds, tz=timezone.utc), "second"
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        y, m, d = map(int, text.split("-"))
        return datetime(y, m, d, tzinfo=timezone.utc), "date"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), "second"


# Fields whose change counts as a meaningful job update.
TRACKED_FIELDS = (
    "title", "location", "department", "employment_type", "workplace_type",
    "description_text", "application_url",
)


def normalize_for_hash(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def content_hash(fields: dict[str, object]) -> str:
    payload = json.dumps({k: normalize_for_hash(fields.get(k)) for k in TRACKED_FIELDS}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
