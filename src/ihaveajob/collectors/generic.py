"""Universal collector for ANY careers page (adapter ``auto``).

Resolution chain, best first (each step verified against real sites on
2026-10-21; see docs/sources.md):

1. **Embedded job board**: the page links to a known board (Workday,
   Ashby, Greenhouse, Lever). We switch to that board's official feed.
   Example: rmi.org/about/careers links to rockymountain.wd1.myworkdayjobs.com.
2. **JSON-LD JobPosting** data on the page.
3. **Job links**: repeated links that carry a job id
   (``/careers/job/1970…``, ``/details/200314010``, ``/profile/job_details/…``,
   ``…_JR2025837``). Title = link text; nearby text kept as a summary.
4. **Follow a "Job openings" link** once (e.g. wri.org/careers → /careers/jobs).
5. **Headless browser** for pages built with JavaScript (Microsoft, Meta,
   Amazon), then steps 1–3 again on the rendered page.
6. **Page-text watch**: if no individual jobs can be identified, the visible
   text is snapshotted and line-level changes are reported.

Failures are explicit: 404 → ``not_found``, 401/403/429 → ``blocked``,
robots.txt → ``robots_disallowed``. Nothing is silently skipped.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig
from ..normalization import clean_text
from ..rendering import render, rendering_available
from .base import CollectContext, Collector
from .jsonld import apply_posting, find_job_postings

# --- link heuristics ---------------------------------------------------------
_ID_SEGMENT = [
    re.compile(r"^\d{4,}$"),                                   # /jobs/10466326
    re.compile(r"^\d{4,}[-_].+"),                              # /12345-data-analyst
    re.compile(r".+[_-](?:[A-Za-z]{1,4}-?)?\d{4,}(?:-\d+)?$"),  # Title_JR2025837, title-R12345
    re.compile(r"^[A-Za-z]{1,4}-?\d{4,}$"),                    # R12345, REQ-1234
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),  # uuid
    re.compile(r"^(?=[0-9a-f]{0,7}\d)(?=[0-9a-f]{0,7}[a-f])[0-9a-f]{8}-[a-z0-9-]+$"),     # aaaa0001-slug
]
_ID_TOKEN = re.compile(r"([A-Za-z]{0,4}-?\d{4,}(?:-\d+)?|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
_ID_PARAMS = {"jobid", "job_id", "gh_jid", "pid", "jid", "id", "req", "reqid", "requisitionid", "postingid",
              "job", "jobreqid", "jobpostingid", "positionid"}
_JOB_WORDS = re.compile(r"job|position|opening|vacanc|requisition|posting|career|opportunit|role|details|req\b|stellen",
                        re.I)
_ASSET = re.compile(r"\.(css|js|ico|png|jpe?g|gif|svg|webp|pdf|woff2?|ttf|mp4|zip|xml|json)$", re.I)
_STOP_TITLES = {"apply", "apply now", "learn more", "view", "view job", "view details", "see details", "details",
                "more", "read more", "see locations", "share", "save", "save job", "job details", "see job"}
_LISTING_TEXT = re.compile(r"(open|current|all|view|see|explore|browse|search)\s+(job|position|role|opening|vacanc|career|opportunit)"
                           r"|job\s+openings|career\s+opportunities|join\s+our\s+team", re.I)
_LISTING_PATH = re.compile(r"/(jobs|openings|positions|vacancies|opportunities|job-openings|open-positions|search)/?$", re.I)
_PAGINATION_TEXT = re.compile(r"\b(load|show|see)\s+more\b|\bnext\s+page\b|\bpage\s+\d+\s+of\b|\b\d+\s*[-–]\s*\d+\s+of\s+\d", re.I)
_TOTAL_TEXT = re.compile(r"\b([\d,]{2,})\s+(?:jobs|results|positions|openings|roles|matches)\b", re.I)
_ATS_URL = re.compile(
    r"https?://(?:jobs\.ashbyhq\.com/[^\s\"'<>?#]+|(?:job-)?boards(?:-api)?\.greenhouse\.io/[^\s\"'<>#]+"
    r"|jobs\.(?:eu\.)?lever\.co/[^\s\"'<>?#]+|[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[^\s\"'<>#]*)", re.I)

MAX_LINK_JOBS = 300
MAX_SNAPSHOT_LINES = 1500


def _site(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-3:]) if len(parts) > 2 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else ".".join(parts[-2:])


def _job_token(url: str) -> tuple[str, int] | None:
    """Return (id token, index of the id segment) for a job-looking URL."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    for key, value in parse_qsl(parts.query):
        if key.lower() in _ID_PARAMS and _ID_TOKEN.fullmatch(value or ""):
            return value, len(segments)
    for idx, seg in enumerate(segments):
        if any(p.match(seg) for p in _ID_SEGMENT):
            tokens = _ID_TOKEN.findall(seg)
            if tokens:
                return tokens[-1] if not re.match(r"^\d{4,}", seg) else tokens[0], idx
    return None


def _humanize(segment: str, token: str) -> str:
    text = segment.replace(token, "")
    return clean_text(re.sub(r"[-_]+", " ", text)) or token


def _card(anchor: Tag, token_of: dict[int, str], token: str) -> Tag:
    """Largest ancestor that still contains only this job's links (the job 'card')."""
    best = anchor
    node = anchor.parent
    for _ in range(5):
        if node is None or node.name in ("body", "html", "ul", "ol", "table", "tbody", "main", "section"):
            break
        tokens = {token_of.get(id(a)) for a in node.find_all("a", href=True)} - {None}
        if tokens != {token} or len(node.get_text(" ", strip=True)) > 600:
            break
        best = node
        node = node.parent
    return best


def extract_job_links(html: str, page_url: str, company: str) -> list[NormalizedJob]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    page = urlsplit(page_url)
    anchors: list[tuple[Tag, str, str, int]] = []
    token_of: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or _ASSET.search(parts.path):
            continue
        if _site(parts.netloc) != _site(page.netloc) and not _ATS_URL.match(url):
            continue
        found = _job_token(url)
        if not found:
            continue
        token, seg_idx = found
        segments = [s for s in parts.path.split("/") if s]
        before = "/".join(segments[:seg_idx])
        if not _JOB_WORDS.search(before + " " + parts.query):
            continue  # an id in a non-job path (news article, blog post ...)
        anchors.append((a, url, token, seg_idx))
        token_of[id(a)] = token

    by_token: dict[str, list[tuple[Tag, str, int]]] = defaultdict(list)
    for a, url, token, seg_idx in anchors:
        by_token[token].append((a, url, seg_idx))

    jobs: list[NormalizedJob] = []
    for token, links in list(by_token.items())[:MAX_LINK_JOBS]:
        # Prefer the link that isn't an "apply"/"locationPicker" variant.
        links.sort(key=lambda x: ("apply" in x[1].lower(), len(urlsplit(x[1]).path.split("/")) - x[2], len(x[1])))
        anchor, url, seg_idx = links[0]
        title = None
        for a, _, _ in links:
            for line in a.get_text("\n").splitlines():
                line = clean_text(line)
                if line and line.lower() not in _STOP_TITLES and 2 < len(line) <= 160:
                    title = line
                    break
            title = title or clean_text(a.get("aria-label") or a.get("title"))
            if title:
                break
        if not title:
            segments = [s for s in urlsplit(url).path.split("/") if s]
            seg = segments[seg_idx] if seg_idx < len(segments) else token
            nxt = segments[seg_idx + 1] if seg_idx + 1 < len(segments) else ""
            title = _humanize(seg if seg != token else nxt, token)
        card = _card(anchor, token_of, token)
        card_lines = [clean_text(l) for l in card.get_text("\n").splitlines()]
        summary = " · ".join(l for l in card_lines if l and l != title and l.lower() not in _STOP_TITLES)[:300]
        parts = urlsplit(url)
        canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        jobs.append(NormalizedJob(external_id=token, title=title, company=company, url=canonical,
                                  application_url=canonical, extra={"card_text": summary} if summary else {}))
    return jobs


def looks_paginated(html: str, found: int) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(attrs={"rel": "next"}) or soup.find(attrs={"aria-label": re.compile(r"next", re.I)}):
        return True
    text = soup.get_text(" ")
    if _PAGINATION_TEXT.search(text):
        return True
    for m in _TOTAL_TEXT.finditer(text):
        try:
            if int(m.group(1).replace(",", "")) > found:
                return True
        except ValueError:
            pass
    return False


def discover_board(html: str) -> str | None:
    """Return the most-referenced known job-board URL on the page, if any."""
    from . import detect_source  # local import: avoid a cycle

    counts: Counter[tuple[str, str]] = Counter()
    for raw in _ATS_URL.findall(html):
        try:
            det = detect_source(raw.replace("&amp;", "&"))
        except ValueError:
            continue
        if det.adapter != "auto":
            counts[(det.adapter, det.url)] += 1
    if not counts:
        return None
    (adapter, url), _ = counts.most_common(1)[0]
    return url


def find_listing_link(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    page = urlsplit(page_url)
    candidates: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or _site(parts.netloc) != _site(page.netloc):
            continue
        if parts.path.rstrip("/") == page.path.rstrip("/"):
            continue
        text = clean_text(a.get_text(" ")) or ""
        score = (2 if _LISTING_TEXT.search(text) else 0) + (1 if _LISTING_PATH.search(parts.path) else 0)
        if score:
            candidates.append((score, urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


def visible_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "head"]):
        tag.decompose()
    seen: set[str] = set()
    lines: list[str] = []
    for raw in soup.get_text("\n").splitlines():
        line = clean_text(raw)
        if line and len(line) > 2 and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines[:MAX_SNAPSHOT_LINES]


class GenericPageCollector(Collector):
    name = "auto"
    uses_html = True

    def _from_html(self, html: str, url: str, source: SourceConfig, client: HttpClient, ctx: CollectContext,
                   how: str) -> FetchResult | None:
        board = discover_board(html)
        if board:
            from . import detect_source, get_collector

            det = detect_source(board)
            delegate = replace(source, adapter=det.adapter, url=det.url, options={**det.options})
            result = get_collector(det.adapter).fetch(delegate, client, ctx)
            result.notes.insert(0, f"Found a {det.adapter} job board on the page ({det.url}); reading its feed.")
            result.resolved = {"mode": f"board:{det.adapter}", "url": det.url}
            return result
        postings = find_job_postings(html)
        if postings:
            jobs = []
            for i, posting in enumerate(postings):
                job_url = posting.get("url") or url
                ident = posting.get("identifier")
                ident = ident.get("value") if isinstance(ident, dict) else ident
                ext = str(ident or job_url if job_url != url else f"{url}#{i}")
                job = NormalizedJob(external_id=ext, title=clean_text(posting.get("title")) or "Untitled",
                                    company=source.company, url=job_url, application_url=job_url)
                apply_posting(job, posting)
                jobs.append(job)
            return FetchResult(jobs=jobs, complete=not looks_paginated(html, len(jobs)), total_reported=len(jobs),
                               notes=[f"Read {len(jobs)} jobs from structured JobPosting data ({how})."],
                               resolved={"mode": f"jsonld:{how}", "url": url})
        jobs = extract_job_links(html, url, source.company)
        if jobs:
            complete = bool(source.options.get("complete")) or not looks_paginated(html, len(jobs))
            notes = [f"Found {len(jobs)} job links on the page ({how})."]
            if not complete:
                notes.append("The page appears to show only part of the results (pagination/'load more'), "
                             "so removed jobs are not reported. Narrow the search in the URL if needed.")
            self._enrich(jobs, client, ctx, notes)
            return FetchResult(jobs=jobs, complete=complete, total_reported=None, notes=notes,
                               resolved={"mode": f"links:{how}", "url": url})
        return None

    def _enrich(self, jobs: list[NormalizedJob], client: HttpClient, ctx: CollectContext, notes: list[str]) -> None:
        """For new jobs, read the job page's JSON-LD (if any) for description/date."""
        enriched = 0
        for job in jobs:
            if not ctx.needs_details(job.external_id) or ctx.detail_fetches >= ctx.max_detail_fetches:
                continue
            ctx.detail_fetches += 1
            try:
                _, html = client.get_page(job.url)
            except FetchError:
                continue
            postings = find_job_postings(html)
            if postings:
                apply_posting(job, postings[0])
                enriched += 1
            else:
                job.extra["details_fetched"] = True  # tried; nothing structured on the page
        if enriched:
            notes.append(f"Read details for {enriched} job pages.")

    def fetch(self, source: SourceConfig, client: HttpClient, ctx: CollectContext) -> FetchResult:
        notes: list[str] = []
        final_url, html = client.get_page(source.url)
        result = self._from_html(html, final_url, source, client, ctx, "page")
        if result:
            return result
        listing = find_listing_link(html, final_url)
        if listing:
            try:
                listing_url, listing_html = client.get_page(listing)
            except FetchError as exc:
                notes.append(f"Tried the job listings link {listing} but it failed ({exc.kind}).")
            else:
                result = self._from_html(listing_html, listing_url, source, client, ctx, "linked listings page")
                if result:
                    result.notes.insert(0, f"Followed the job listings link: {listing_url}")
                    return result
        text_lines = visible_text_lines(html)
        mode = "text:page"
        if rendering_available():
            try:
                r_url, r_html, r_text = render(final_url, client)
            except FetchError as exc:
                if exc.kind in ("blocked", "robots_disallowed", "not_found"):
                    raise
                notes.append(f"Headless browser failed ({exc.kind}); watching the static page text instead.")
            else:
                result = self._from_html(r_html, r_url, source, client, ctx, "headless browser")
                if result:
                    result.notes = notes + result.notes
                    return result
                lines = (clean_text(line) for line in r_text.splitlines())
                text_lines = list(dict.fromkeys(l for l in lines if l and len(l) > 2))[:MAX_SNAPSHOT_LINES]
                mode = "text:headless"
        else:
            notes.append("No jobs in the page HTML and no headless browser installed here (the GitHub workflow has one).")
        notes.append("No individual jobs could be identified; watching the page text for changes.")
        return FetchResult(jobs=[], complete=False, page_text=text_lines, resolved={"mode": mode}, notes=notes)
