"""IhaveaJob web app: browse tracked jobs, add careers pages, manage them, see changes.

Run locally:   streamlit run dashboard/app.py
Deploy:        Streamlit Community Cloud (see docs/dashboard.md)

With GITHUB_TOKEN + GITHUB_REPO secrets the app edits config/companies.yaml in
the GitHub repository and reads the job database from the `data` branch.
Without them it works on the local files (handy for development).

All text that comes from careers pages is HTML-escaped before display.
"""

from __future__ import annotations

import hmac
import html
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Streamlit Cloud has no headless browser; previews read the page HTML. The
# hourly GitHub run uses a real browser for JavaScript-built pages.
os.environ.setdefault("IHAVEAJOB_DISABLE_RENDER", "1")

import streamlit as st  # noqa: E402

from ihaveajob.collectors import CollectContext, UnsupportedUrl, get_collector  # noqa: E402
from ihaveajob.config import (  # noqa: E402
    ConfigError, add_source_text, build_source, edit_sources_text, parse_sources, source_entry,
)
from ihaveajob.database import make_engine, session_factory  # noqa: E402
from ihaveajob.filters import matches  # noqa: E402
from ihaveajob.github_api import GitHubError, GitHubRepo  # noqa: E402
from ihaveajob.http import FetchError, HttpClient  # noqa: E402
from ihaveajob.models import SourceFilters  # noqa: E402
from ihaveajob.services.status import daily_activity, list_jobs, source_status  # noqa: E402

CONFIG_PATH = "config/companies.yaml"
MAX_PER_COMPANY = 100
FETCH_HINTS = {
    "not_found": "This page doesn't exist (404). Open the link in your browser to check it.",
    "blocked": "This site refuses automated visitors (bot protection or a login), so it can't be monitored.",
    "robots_disallowed": "This site's robots.txt forbids automated reading of this page, so it can't be monitored.",
    "timeout": "The site didn't respond in time. Try again, or add it anyway: the hourly check retries.",
    "connection": "Couldn't reach the site from here. Check the link, or add it anyway: the hourly check retries.",
    "http_status": "The site returned an error. Try again later, or add it anyway: the hourly check retries.",
}

st.set_page_config(page_title="IhaveaJob", layout="wide")

# --- look and feel ----------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap');
:root { --accent:#1E3A5F; --ink:#111827; --muted:#6B7280; --line:#E5E7EB; --soft:#F9FAFB; --new:#1D4ED8; }
html, body, .stApp, .stMarkdown p, .stMarkdown li, label, input, textarea, button p, h1, h2, h3,
[data-testid="stCaptionContainer"] { font-family:'Public Sans', system-ui, -apple-system, sans-serif; }
.block-container { max-width: 960px; padding-top: 3.5rem; }
h3 { font-weight:600 !important; font-size:1.15rem !important; }
.hj-brand { color:var(--accent); font-weight:700; font-size:1.05rem; letter-spacing:-0.01em; }
.hj-headline { font-size:1.6rem; line-height:1.25; color:var(--ink); font-weight:600; margin:.6rem 0 .25rem; }
.hj-sub { color:var(--muted); font-size:.95rem; margin-bottom:1.2rem; }
.hj-list { border:1px solid var(--line); border-radius:8px; background:#fff; }
.hj-job { display:grid; grid-template-columns: 1fr auto; gap:2px 16px; padding:12px 16px; }
.hj-job + .hj-job { border-top:1px solid var(--line); }
.hj-job a.hj-title, .hj-job .hj-title { color:var(--ink); font-weight:600; font-size:.98rem; text-decoration:none; }
.hj-job a.hj-title:hover, .hj-job a.hj-title:focus { color:var(--accent); text-decoration:underline; }
.hj-meta { color:var(--muted); font-size:.87rem; grid-column:1; }
.hj-meta .co { color:var(--ink); font-weight:500; margin-right:.5rem; }
.hj-meta .team { margin-left:.5rem; padding-left:.5rem; border-left:1px solid var(--line); }
.hj-when { color:var(--muted); font-size:.82rem; text-align:right; white-space:nowrap; grid-row:1 / span 2; grid-column:2; }
.hj-tag { color:var(--new); font-size:.78rem; font-weight:600; margin-left:.5rem; }
.hj-change { font-size:.86rem; color:var(--muted); margin-top:2px; grid-column:1 / span 2; }
.hj-empty { color:var(--muted); padding:1rem 0; }
@media (max-width: 640px) {
  .hj-headline { font-size:1.3rem; }
  .hj-job { grid-template-columns: 1fr; } .hj-when { grid-column:1; grid-row:auto; text-align:left; }
}
</style>
"""


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


# --- settings ------------------------------------------------------------------
def setting(name: str, default: str | None = None) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets file locally
        value = None
    return value or os.getenv(name) or default


def check_password() -> bool:
    expected = setting("APP_PASSWORD")
    if not expected or st.session_state.get("authed"):
        return True
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="hj-brand">IhaveaJob</div>', unsafe_allow_html=True)
    pw = st.text_input("Password", type="password", key="password")
    if pw:
        if hmac.compare_digest(pw, expected):
            st.session_state["authed"] = True
            st.rerun()
        st.error("That password isn't right.")
    return False


# --- storage: GitHub (deployed) or local files (development) -----------------------
class Store:
    def __init__(self) -> None:
        token, repo = setting("GITHUB_TOKEN"), setting("GITHUB_REPO")
        self.github = GitHubRepo(token, repo, setting("GITHUB_BRANCH", "main")) if token and repo else None

    @property
    def remote(self) -> bool:
        return self.github is not None

    def read_config(self) -> tuple[str, str | None]:
        if self.github:
            return self.github.get_text(CONFIG_PATH)
        path = ROOT / CONFIG_PATH
        return (path.read_text() if path.exists() else "sources: []\n"), None

    def save_config(self, text: str, sha: str | None, message: str) -> None:
        if self.github:
            self.github.put_text(CONFIG_PATH, text, sha or "", message + " (via web app)")
        else:
            (ROOT / CONFIG_PATH).write_text(text)
        load_database.clear()

    def run_now(self) -> None:
        if not self.github:
            raise GitHubError("Checking from the app needs GITHUB_TOKEN and GITHUB_REPO. Locally, run: ihaveajob run")
        self.github.run_workflow("monitor.yml")


@st.cache_resource(ttl=300, show_spinner=False)
def load_database(remote: bool):
    """Session factory for the job database (data branch copy when deployed)."""
    db_url = setting("DATABASE_URL")
    if db_url:
        return session_factory(make_engine(db_url))
    if remote:
        blob = Store().github.get_bytes("ihaveajob.db", ref="data")
        if blob is None:
            return None
        path = Path(tempfile.gettempdir()) / "ihaveajob-dashboard.db"
        path.write_bytes(blob)
        return session_factory(make_engine(f"sqlite:///{path}"))
    local = ROOT / "data" / "ihaveajob.db"
    return session_factory(make_engine(f"sqlite:///{local}")) if local.exists() else None


@st.cache_data(ttl=600, show_spinner=False)
def preview(url: str, company: str, keywords: str, locations: str, exclude: str) -> dict:
    source, notes = build_source(url, company=company or None, filters=_filters(keywords, locations, exclude))
    client = HttpClient()
    try:
        result = get_collector(source.adapter).fetch(source, client, CollectContext(max_detail_fetches=3))
    except FetchError as exc:
        return {"error": exc.kind, "message": str(exc), "source": source}
    hits = [j for j in result.jobs if matches(j, source.filters)]
    return {
        "source": source, "notes": notes + result.notes, "found": len(result.jobs), "matching": len(hits),
        "mode": (result.resolved or {}).get("mode", source.adapter), "page_text": result.page_text,
        "jobs": [{"company": source.company, "title": j.title, "location": j.location or j.extra.get("card_text", ""),
                  "department": j.department or "", "url": j.url, "posted": j.published_at, "first_seen": None,
                  "is_new": False} for j in hits[:12]],
    }


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _filters(keywords: str, locations: str, exclude: str) -> SourceFilters:
    return SourceFilters(keywords_any=_csv(keywords), locations=_csv(locations), keywords_exclude=_csv(exclude))


def ago(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    minutes = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def nice_date(ts: datetime | None) -> str:
    return ts.strftime("%b %-d") if ts else ""


def join_names(names: list[str]) -> str:
    names = sorted(set(names))
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:2]) + f" and {len(names) - 2} more"


# --- pieces of UI ------------------------------------------------------------------
def job_card(job: dict, change: dict | None = None) -> str:
    url = safe_url(job.get("url"))
    title = (f'<a class="hj-title" href="{e(url)}" target="_blank" rel="noopener">{e(job["title"])}</a>'
             if url else f'<span class="hj-title">{e(job["title"])}</span>')
    tag = '<span class="hj-tag">New</span>' if job.get("is_new") else ""  # plain text label
    where = e(job.get("location"))
    if job.get("department"):
        where += f'<span class="team">{e(job["department"])}</span>'

    when = (f"Posted {nice_date(job['posted'])}" if job.get("posted")
            else f"Seen {nice_date(job['first_seen'])}" if job.get("first_seen") else "")
    extra = f'<div class="hj-change">{change}</div>' if change else ""
    return (f'<div class="hj-job"><div>{title}{tag}</div>'
            f'<div class="hj-when">{e(when)}</div>'
            f'<div class="hj-meta"><span class="co">{e(job["company"])}</span>{where}</div>{extra}</div>')


def hero(sources, jobs: list[dict], activity: list[dict]) -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="hj-brand">IhaveaJob</div>', unsafe_allow_html=True)
    since = date.today() - timedelta(days=1)
    fresh = [a for a in activity if a["change"] == "New" and a["day"] >= since]
    watching = [s for s in sources if s.enabled]
    if fresh:
        n = len(fresh)
        headline = f"{n} new job{'s' if n != 1 else ''} since yesterday at {join_names([a['company'] for a in fresh])}."
    elif watching:
        headline = ""
    else:
        headline = "Add a careers page to get started."
    if watching:
        sub = (f"Watching {len(jobs)} jobs on {len(watching)} careers page{'s' if len(watching) != 1 else ''}. "
               "Pages are checked every hour, and you're emailed when something changes.")
    else:
        sub = "Paste any company's careers link. It's checked every hour, and you're emailed when something changes."
    head = f'<div class="hj-headline">{e(headline)}</div>' if headline else ""
    st.markdown(f'{head}<div class="hj-sub">{e(sub)}</div>', unsafe_allow_html=True)


def jobs_tab(jobs: list[dict]) -> None:
    if not jobs:
        st.markdown('<div class="hj-empty">No jobs yet. Add a careers page, then press <b>Check now</b> in the '
                    'sidebar, or wait for the next hourly check.</div>', unsafe_allow_html=True)
        return
    c1, c2, c3 = st.columns([2.2, 1.6, 1])
    query = c1.text_input("Search jobs", placeholder="Title, location or team", key="job_search",
                          label_visibility="collapsed")
    companies = sorted({j["company"] for j in jobs})
    picked = c2.multiselect("Companies", companies, placeholder="All companies", key="job_companies",
                            label_visibility="collapsed")
    only_new = c3.toggle("Only new", key="job_only_new")
    q = query.strip().lower()
    shown = [j for j in jobs
             if (not picked or j["company"] in picked) and (not only_new or j["is_new"])
             and (not q or q in f'{j["title"]} {j["location"]} {j["department"]} {j["company"]}'.lower())]
    if not shown:
        st.caption(f"0 of {len(jobs)} jobs")
        st.markdown('<div class="hj-empty">No jobs match. Try a shorter search or clear the filters.</div>',
                    unsafe_allow_html=True)
        return
    groups: dict[str, list[dict]] = {}
    for j in shown:
        groups.setdefault(j["company"], []).append(j)
    st.caption(f"{len(shown)} of {len(jobs)} jobs at {len(groups)} compan{'y' if len(groups) == 1 else 'ies'}")
    filtering = bool(q or picked or only_new)
    ordered = sorted(groups.items(), key=lambda kv: (-sum(j["is_new"] for j in kv[1]), kv[0].lower()))
    for company, items in ordered:
        new = sum(j["is_new"] for j in items)
        label = f"{company}  ({len(items)} job{'s' if len(items) != 1 else ''}{f', {new} new' if new else ''})"
        with st.expander(label, expanded=filtering or len(groups) <= 3 or new > 0):
            st.markdown('<div class="hj-list">' + "".join(job_card(j) for j in items[:MAX_PER_COMPANY]) + "</div>",
                        unsafe_allow_html=True)
            if len(items) > MAX_PER_COMPANY:
                st.caption(f"Showing the newest {MAX_PER_COMPANY}. Search to narrow the list.")


def add_tab(store: Store) -> None:
    st.markdown("### Add a careers page")
    st.caption("Open the company's careers page, search or filter it for your field, and copy the link from the "
               "address bar. Any company works.")
    with st.form("add"):
        url = st.text_input("Careers page link", placeholder="https://company.com/careers?q=climate", key="add_url")
        company = st.text_input("Company name", placeholder="Optional: worked out from the link", key="add_company")
        with st.expander("Only tell me about some jobs"):
            keywords = st.text_input("Keywords", placeholder="climate, sustainability, data", key="add_keywords",
                                     help="Only jobs mentioning at least one of these.")
            locations = st.text_input("Locations", placeholder="New York, Remote", key="add_locations")
            exclude = st.text_input("Skip jobs mentioning", placeholder="intern, sales", key="add_exclude")
        b1, b2, _ = st.columns([1, 1, 3])
        do_preview = b1.form_submit_button("Preview", width="stretch")
        do_add = b2.form_submit_button("Add page", type="primary", width="stretch")
    if not (do_preview or do_add):
        return
    if not url.strip():
        st.warning("Paste a careers page link first.")
        return
    try:
        if do_preview:
            with st.spinner("Reading the page…"):
                show_preview(preview(url.strip(), company, keywords, locations, exclude))
            return
        source, _ = build_source(url.strip(), company=company or None, filters=_filters(keywords, locations, exclude))
        text, sha = store.read_config()
        store.save_config(add_source_text(text, source), sha, f"Add careers page: {source.company}")
        st.success(f"Now watching **{source.company}**. The next check records its current jobs; from then on "
                   "you'll be emailed about every change.")
    except (UnsupportedUrl, ConfigError, GitHubError) as exc:
        st.error(str(exc))


def show_preview(p: dict) -> None:
    src = p["source"]
    if p.get("error"):
        st.error(FETCH_HINTS.get(p["error"], p["message"]))
        return
    st.markdown(f"### {e(src.company)}")
    st.caption(f"Read via {p['mode']}. Found {p['found']} jobs, {p['matching']} match your filters.")
    if p["jobs"]:
        st.markdown('<div class="hj-list">' + "".join(job_card(j) for j in p["jobs"]) + "</div>",
                    unsafe_allow_html=True)
    elif p["page_text"] is not None:
        st.info("No individual jobs in this page's HTML. Many sites build their job list with JavaScript; the hourly "
                "check opens pages in a real browser and usually finds the jobs there. Either way you'll be told "
                "when the page changes.")
    for note in p["notes"]:
        st.caption(note)


def pages_tab(store: Store, text: str, sha: str | None, sources, status) -> None:
    if not sources:
        st.markdown('<div class="hj-empty">No pages yet. Add one in the <b>Add a page</b> tab.</div>',
                    unsafe_allow_html=True)
        return
    for src in sorted(sources, key=lambda s: s.company.lower()):
        info = status.get(src.key)
        failing = bool(info and info["failures_in_a_row"] >= 3)
        with st.container(border=True):
            left, right = st.columns([4, 1.3])
            with left:
                state = "Paused" if not src.enabled else ("Needs attention" if failing else "Active")
                color = {"Paused": "#6B7280", "Needs attention": "#B91C1C", "Active": "#047857"}[state]
                st.markdown(f'**{e(src.company)}** &nbsp; <small style="color:{color};font-weight:600">{state}</small>',
                            unsafe_allow_html=True)
                st.markdown(f"[{e(src.url)}]({src.url})")
                details = []
                if src.enabled and info:
                    details.append(f"{info['matching']} jobs on watch, checked {ago(info['last_checked'])}, "
                                   f"read via {info['read_via']}.")
                elif src.enabled:
                    details.append("Waiting for its first check (hourly, or press Check now).")
                f = src.filters.to_dict()
                if f.get("keywords_any"):
                    details.append("Keywords: " + ", ".join(f["keywords_any"]) + ".")
                if f.get("locations"):
                    details.append("Locations: " + ", ".join(f["locations"]) + ".")
                if f.get("keywords_exclude"):
                    details.append("Skipping: " + ", ".join(f["keywords_exclude"]) + ".")
                st.caption(" ".join(details))
                if info and info["error"]:
                    st.caption(f"Last check failed ({info['failures_in_a_row']} in a row): {info['error'][:160]}")
            with right:
                with st.popover("Edit", width="stretch"):
                    edit_form(store, text, sha, src)
                if st.button("Resume" if not src.enabled else "Pause", key=f"toggle-{src.key}", width="stretch"):
                    _edit(store, text, sha, src.key, "toggle", src.company)
                with st.popover("Remove", width="stretch"):
                    st.write(f"Stop watching **{src.company}**? Its history is kept.")
                    if st.button("Remove page", key=f"rm-{src.key}", type="primary"):
                        _edit(store, text, sha, src.key, "remove", src.company)


def edit_form(store: Store, text: str, sha: str | None, src) -> None:
    f = src.filters
    with st.form(f"edit-{src.key}"):
        company = st.text_input("Company name", value=src.company, key=f"edit-company-{src.key}")
        url = st.text_input("Careers page link", value=src.url, key=f"edit-url-{src.key}")
        keywords = st.text_input("Keywords", value=", ".join(f.keywords_any), key=f"edit-kw-{src.key}",
                                 help="Only jobs mentioning at least one of these. Leave empty for all jobs.")
        locations = st.text_input("Locations", value=", ".join(f.locations), key=f"edit-loc-{src.key}")
        exclude = st.text_input("Skip jobs mentioning", value=", ".join(f.keywords_exclude), key=f"edit-ex-{src.key}")
        saved = st.form_submit_button("Save changes", type="primary")
    if not saved:
        return
    try:
        new, _ = build_source(url.strip(), company=company.strip() or src.company, key=src.key,
                              filters=_filters(keywords, locations, exclude))
        new.enabled = src.enabled
        if new.url == src.url and new.adapter == src.adapter:
            new.options = {**src.options, **new.options}
    except (UnsupportedUrl, ConfigError) as exc:
        st.error(str(exc))
        return

    def apply(entries: list[dict]) -> None:
        for i, entry in enumerate(entries):
            entry_key = entry.get("key") or parse_sources(f"sources:\n  - url: {entry['url']}\n")[0].key
            if entry_key == src.key:
                entries[i] = source_entry(new)
                return

    try:
        store.save_config(edit_sources_text(text, apply), sha, f"Edit careers page: {new.company}")
        st.toast(f"{new.company} saved")
        st.rerun()
    except (ConfigError, GitHubError) as exc:
        st.error(str(exc))


def _edit(store: Store, text: str, sha: str | None, key: str, action: str, company: str) -> None:
    def apply(entries: list[dict]) -> None:
        for i, entry in enumerate(entries):
            entry_key = entry.get("key") or parse_sources(f"sources:\n  - url: {entry['url']}\n")[0].key
            if entry_key == key:
                if action == "remove":
                    entries.pop(i)
                else:
                    entry["enabled"] = not entry.get("enabled", True)
                return

    try:
        store.save_config(edit_sources_text(text, apply), sha,
                          f"{'Remove' if action == 'remove' else 'Pause/resume'} careers page: {company}")
        st.toast(f"{company} {'removed' if action == 'remove' else 'updated'}")
        st.rerun()
    except (ConfigError, GitHubError) as exc:
        st.error(str(exc))


def sidebar(store: Store, status: dict) -> None:
    last = max((v["last_checked"] for v in status.values() if v["last_checked"]), default=None)
    with st.sidebar:
        st.markdown("### Checks")
        if st.button("Check now", type="primary", width="stretch", disabled=not store.remote):
            try:
                store.run_now()
                st.success("Check started. Results appear in a few minutes.")
            except GitHubError as exc:
                st.error(str(exc))
        st.caption(f"Last checked: {ago(last)}")


def main() -> None:
    if not check_password():
        return
    store = Store()
    try:
        text, sha = store.read_config()
        sources = parse_sources(text)
    except (GitHubError, ConfigError) as exc:
        st.error(f"Couldn't load your pages: {exc}")
        return
    try:
        sessions = load_database(store.remote)
    except Exception as exc:  # noqa: BLE001 - keep the app usable if history can't be read
        st.warning(f"Job history is unavailable right now: {exc}")
        sessions = None
    status, jobs, activity = {}, [], []
    if sessions is not None:
        with sessions() as session:
            status, jobs, activity = source_status(session), list_jobs(session), daily_activity(session, 31)
    sidebar(store, status)
    hero(sources, jobs, activity)
    t_add, t_jobs, t_pages = st.tabs(["Add a page", f"Jobs ({len(jobs)})", f"My pages ({len(sources)})"])
    with t_add:
        add_tab(store)
    with t_jobs:
        jobs_tab(jobs)
    with t_pages:
        pages_tab(store, text, sha, sources, status)


main()
