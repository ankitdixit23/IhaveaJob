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
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Streamlit Cloud has no headless browser; previews read the page HTML. The
# hourly GitHub run uses a real browser for JavaScript-built pages.
os.environ.setdefault("IHAVEAJOB_DISABLE_RENDER", "1")

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from ihaveajob.collectors import CollectContext, UnsupportedUrl, get_collector  # noqa: E402
from ihaveajob.config import (  # noqa: E402
    ConfigError, add_source_text, build_source, edit_sources_text, parse_sources,
)
from ihaveajob.database import make_engine, session_factory  # noqa: E402
from ihaveajob.filters import matches  # noqa: E402
from ihaveajob.github_api import GitHubError, GitHubRepo  # noqa: E402
from ihaveajob.http import FetchError, HttpClient  # noqa: E402
from ihaveajob.models import SourceFilters  # noqa: E402
from ihaveajob.services.status import daily_activity, list_jobs, recent_changes, source_status  # noqa: E402

CONFIG_PATH = "config/companies.yaml"
PAGE_SIZE = 20
FETCH_HINTS = {
    "not_found": "This page doesn't exist (404). Open the link in your browser to check it.",
    "blocked": "This site refuses automated visitors (bot protection or a login), so it can't be monitored.",
    "robots_disallowed": "This site's robots.txt forbids automated reading of this page, so it can't be monitored.",
    "timeout": "The site didn't respond in time. Try again, or add it anyway: the hourly check retries.",
    "connection": "Couldn't reach the site from here. Check the link, or add it anyway: the hourly check retries.",
    "http_status": "The site returned an error. Try again later, or add it anyway: the hourly check retries.",
}

st.set_page_config(page_title="IhaveaJob", page_icon="🌿", layout="wide")

# --- look and feel ----------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Public+Sans:wght@400;500;600&display=swap');
:root { --forest:#12372A; --moss:#4F7C5A; --mist:#F4F7F5; --ink:#1B2420; --slate:#66756F; --amber:#E3A03A;
        --line:#DCE5E0; --paper:#FFFFFF; }
html, body, .stApp, .stMarkdown p, .stMarkdown li, label, input, textarea, button p, [data-testid="stCaptionContainer"] {
  font-family:'Public Sans', system-ui, sans-serif; }
h1, h2, h3, .hj-headline { font-family:'Bricolage Grotesque', 'Public Sans', sans-serif !important; letter-spacing:-0.01em; }
.block-container { max-width: 1080px; padding-top: 2.2rem; }
.hj-brand { display:flex; align-items:center; gap:.5rem; color:var(--moss); font-weight:600; font-size:.95rem; }
.hj-brand b { color:var(--forest); font-family:'Bricolage Grotesque', sans-serif; font-size:1.15rem; }
.hj-headline { font-size:2.35rem; line-height:1.15; color:var(--ink); font-weight:700; margin:.35rem 0 .4rem; max-width: 30ch; }
.hj-sub { color:var(--slate); font-size:1rem; max-width: 62ch; margin-bottom: 1.4rem; }
.hj-pulse { background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:14px 18px; margin-bottom:1.6rem; }
.hj-pulse-row { display:grid; grid-template-columns: minmax(110px, 180px) 1fr auto; align-items:center; gap:14px; padding:6px 0; }
.hj-pulse-row + .hj-pulse-row { border-top:1px solid #EEF2F0; }
.hj-pulse-name { font-weight:600; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hj-days { display:flex; gap:4px; }
.hj-day { width:16px; height:16px; border-radius:4px; background:#E9EFEC; }
.hj-day.l1 { background:#F6DDB0; } .hj-day.l2 { background:#EDBE6B; } .hj-day.l3 { background:var(--amber); }
.hj-pulse-count { color:var(--slate); font-size:.85rem; white-space:nowrap; }
.hj-pulse-legend { color:var(--slate); font-size:.8rem; margin-top:6px; }
.hj-job { background:var(--paper); border:1px solid var(--line); border-left:4px solid transparent; border-radius:10px;
          padding:12px 16px; margin-bottom:10px; display:grid; grid-template-columns: 1fr auto; gap:4px 16px; }
.hj-job.new { border-left-color: var(--amber); }
.hj-job a.hj-title { color:var(--ink); font-weight:600; font-size:1.02rem; text-decoration:none; }
.hj-job a.hj-title:hover, .hj-job a.hj-title:focus { color:var(--forest); text-decoration:underline; }
.hj-meta { color:var(--slate); font-size:.88rem; grid-column:1; }
.hj-meta .co { color:var(--moss); font-weight:600; margin-right:.6rem; }
.hj-meta .team { margin-left:.6rem; padding-left:.6rem; border-left:1px solid var(--line); }
.hj-when { color:var(--slate); font-size:.82rem; text-align:right; white-space:nowrap; grid-row:1 / span 2; grid-column:2; }
.hj-tag { display:inline-block; background:#FBEBCD; color:#7A4E07; border-radius:999px; padding:1px 9px; font-size:.75rem;
          font-weight:600; margin-left:.5rem; vertical-align:2px; }
.hj-change { font-size:.88rem; color:var(--ink); margin-top:4px; grid-column:1 / span 2; }
.hj-change del { color:#A23B2A; } .hj-change ins { color:#1F6B3A; text-decoration:none; font-weight:600; }
.hj-empty { color:var(--slate); padding: 1.2rem 0; }
div[data-testid="stTabs"] button p { font-size:1rem; font-weight:600; }
@media (max-width: 640px) {
  .hj-headline { font-size:1.7rem; }
  .hj-pulse-row { grid-template-columns: 1fr; gap:6px; }
  .hj-day { width:13px; height:13px; }
  .hj-job { grid-template-columns: 1fr; } .hj-when { grid-column:1; grid-row:auto; text-align:left; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
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
    st.markdown('<div class="hj-brand">🌿 <b>IhaveaJob</b></div>', unsafe_allow_html=True)
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
    tag = '<span class="hj-tag">New</span>' if job.get("is_new") else ""
    where = e(job.get("location"))
    if job.get("department"):
        where += f'<span class="team">{e(job["department"])}</span>'

    when = (f"Posted {nice_date(job['posted'])}" if job.get("posted")
            else f"Seen {nice_date(job['first_seen'])}" if job.get("first_seen") else "")
    extra = f'<div class="hj-change">{change}</div>' if change else ""
    return (f'<div class="hj-job{" new" if job.get("is_new") else ""}"><div>{title}{tag}</div>'
            f'<div class="hj-when">{e(when)}</div>'
            f'<div class="hj-meta"><span class="co">{e(job["company"])}</span>{where}</div>{extra}</div>')


def hero(sources, jobs: list[dict], activity: list[dict]) -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="hj-brand">🌿 <b>IhaveaJob</b></div>', unsafe_allow_html=True)
    since = date.today() - timedelta(days=1)
    fresh = [a for a in activity if a["change"] == "New" and a["day"] >= since]
    watching = [s for s in sources if s.enabled]
    if fresh:
        n = len(fresh)
        headline = f"{n} new job{'s' if n != 1 else ''} since yesterday at {join_names([a['company'] for a in fresh])}."
    elif watching:
        headline = "Nothing new since yesterday. Still watching."
    else:
        headline = "Add a careers page to start watching."
    sub = (f"{len(jobs)} jobs on watch across {len(watching)} careers page{'s' if len(watching) != 1 else ''}. "
           "Checked every hour; you get an email whenever something changes.") if watching else \
        "Paste any company's careers link. IhaveaJob checks it every hour and emails you what changed."
    st.markdown(f'<div class="hj-headline">{e(headline)}</div><div class="hj-sub">{e(sub)}</div>',
                unsafe_allow_html=True)
    if watching:
        pulse(watching, activity)


def pulse(sources, activity: list[dict], days: int = 14) -> None:
    today = date.today()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    per = defaultdict(Counter)
    for a in activity:
        if a["change"] == "New":
            per[a["company"]][a["day"]] += 1
    rows = []
    for name in sorted({s.company for s in sources}):
        counts = per.get(name, Counter())
        cells = "".join(
            f'<span class="hj-day {"l3" if counts[d] >= 3 else "l2" if counts[d] == 2 else "l1" if counts[d] else ""}" '
            f'title="{e(d.strftime("%b %-d"))}: {counts[d]} new"></span>' for d in window)
        total = sum(counts[d] for d in window)
        rows.append(f'<div class="hj-pulse-row"><div class="hj-pulse-name">{e(name)}</div>'
                    f'<div class="hj-days" aria-label="{e(name)}: {total} new jobs in {days} days">{cells}</div>'
                    f'<div class="hj-pulse-count">{total} new in {days} days</div></div>')
    st.markdown(f'<div class="hj-pulse">{"".join(rows)}<div class="hj-pulse-legend">Each square is a day; '
                f'amber means new jobs appeared. Hover a square for the count.</div></div>', unsafe_allow_html=True)


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
    st.caption(f"{len(shown)} of {len(jobs)} jobs" + (" (newest first)" if shown else ""))
    if not shown:
        st.markdown('<div class="hj-empty">No jobs match. Try a shorter search or clear the filters.</div>',
                    unsafe_allow_html=True)
        return
    limit = st.session_state.get("job_limit", PAGE_SIZE)
    st.markdown("".join(job_card(j) for j in shown[:limit]), unsafe_allow_html=True)
    if len(shown) > limit and st.button(f"Show {min(PAGE_SIZE, len(shown) - limit)} more", key="more_jobs"):
        st.session_state["job_limit"] = limit + PAGE_SIZE
        st.rerun()


def add_tab(store: Store) -> None:
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.markdown("### Add a careers page")
        st.write("Open the company's careers page, search or filter it for your field, and copy the link from the "
                 "address bar. Any company works.")
        with st.form("add"):
            url = st.text_input("Careers page link", placeholder="https://company.com/careers?q=climate",
                                key="add_url")
            company = st.text_input("Company name", placeholder="Optional: worked out from the link",
                                    key="add_company")
            with st.expander("Only tell me about some jobs"):
                keywords = st.text_input("Keywords", placeholder="climate, sustainability, data", key="add_keywords",
                                         help="Only jobs mentioning at least one of these.")
                locations = st.text_input("Locations", placeholder="New York, Remote", key="add_locations")
                exclude = st.text_input("Skip jobs mentioning", placeholder="intern, sales", key="add_exclude")
            b1, b2 = st.columns(2)
            do_preview = b1.form_submit_button("Preview", width="stretch")
            do_add = b2.form_submit_button("Add page", type="primary", width="stretch")
    with right:
        if not (do_preview or do_add):
            st.markdown("### What happens next")
            st.markdown("- **Preview** shows the jobs IhaveaJob can read on that page.\n"
                        "- **Add page** starts watching it. The first check quietly records today's jobs.\n"
                        "- After that, every new, changed or removed job is emailed to you within the hour.")
            return
        if not url.strip():
            st.warning("Paste a careers page link first.")
            return
        try:
            if do_preview:
                with st.spinner("Reading the page…"):
                    show_preview(preview(url.strip(), company, keywords, locations, exclude))
                return
            source, _ = build_source(url.strip(), company=company or None,
                                     filters=_filters(keywords, locations, exclude))
            text, sha = store.read_config()
            store.save_config(add_source_text(text, source), sha, f"Add careers page: {source.company}")
            st.success(f"Now watching **{source.company}**. The next check records its current jobs; from then on "
                       "you'll be emailed about every change.", icon="✅")
            st.session_state["just_added"] = True
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
        st.markdown("".join(job_card(j) for j in p["jobs"]), unsafe_allow_html=True)
    elif p["page_text"] is not None:
        st.info("No individual jobs in this page's HTML. Many sites build their job list with JavaScript; the hourly "
                "check opens pages in a real browser and usually finds the jobs there. Either way you'll be told "
                "when the page changes.", icon="ℹ️")
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
                state = "Paused" if not src.enabled else ("Needs attention" if failing else "Watching")
                icon = {"Paused": "⏸", "Needs attention": "⚠️", "Watching": "🟢"}[state]
                st.markdown(f"**{e(src.company)}** &nbsp; <small>{icon} {state}</small>", unsafe_allow_html=True)
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
                if st.button("Resume" if not src.enabled else "Pause", key=f"toggle-{src.key}", width="stretch"):
                    _edit(store, text, sha, src.key, "toggle", src.company)
                with st.popover("Remove", width="stretch"):
                    st.write(f"Stop watching **{src.company}**? Its history is kept.")
                    if st.button("Remove page", key=f"rm-{src.key}", type="primary"):
                        _edit(store, text, sha, src.key, "remove", src.company)


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
        st.toast(f"{company} {'removed' if action == 'remove' else 'updated'}", icon="✅")
        st.rerun()
    except (ConfigError, GitHubError) as exc:
        st.error(str(exc))


def changes_tab(sessions, activity: list[dict]) -> None:
    if sessions is None:
        st.markdown('<div class="hj-empty">No checks have run yet.</div>', unsafe_allow_html=True)
        return
    c1, c2 = st.columns([1, 2])
    hours = c1.segmented_control("Period", options=[24, 168, 720], default=168, key="chg_period",
                                 format_func=lambda h: {24: "Day", 168: "Week", 720: "Month"}[h]) or 168
    kinds = c2.pills("Show", ["New", "Updated", "Removed"], selection_mode="multi",
                     default=["New", "Updated", "Removed"], key="chg_kinds") or ["New", "Updated", "Removed"]
    with sessions() as session:
        rows = [r for r in recent_changes(session, hours=hours, limit=400) if r["change"] in kinds]
    days = max(1, hours // 24)
    since = date.today() - timedelta(days=days - 1)
    chart = pd.DataFrame([a for a in activity if a["day"] >= since and a["change"] in kinds])
    if not chart.empty and days > 1:
        import altair as alt

        kinds_order = [k for k in ("New", "Updated", "Removed") if k in kinds]
        all_days = [since + timedelta(days=i) for i in range(days)]
        counted = chart.groupby(["day", "change"]).size()
        grid = pd.DataFrame([{"day": d.strftime("%b %-d"), "change": k, "count": int(counted.get((d, k), 0))}
                             for d in all_days for k in kinds_order])
        palette = {"New": "#E3A03A", "Updated": "#4F7C5A", "Removed": "#9AA7A1"}
        bars = alt.Chart(grid).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("day:O", title=None, sort=[d.strftime("%b %-d") for d in all_days],
                    axis=alt.Axis(labelAngle=0, labelOverlap=True)),
            y=alt.Y("count:Q", title=None, stack=True, axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("change:N", title=None, sort=kinds_order,
                            scale=alt.Scale(domain=kinds_order, range=[palette[k] for k in kinds_order]),
                            legend=alt.Legend(orient="top")),
            tooltip=[alt.Tooltip("day:O", title="Day"), alt.Tooltip("change:N", title="Change"),
                     alt.Tooltip("count:Q", title="Jobs")],
        ).properties(height=170)
        st.altair_chart(bars, width="stretch")
    if not rows:
        st.markdown('<div class="hj-empty">No changes in this period. You\'ll get an email as soon as something '
                    'changes.</div>', unsafe_allow_html=True)
        return
    cards = []
    for r in rows:
        note = None
        if r["change"] == "Updated" and r["what_changed"]:
            note = "Changed: " + e(r["what_changed"].replace("_text", "").replace("_", " "))
        elif r["change"] == "Removed":
            note = "No longer listed on the page."
        job = {"company": r["company"], "title": r["title"], "location": r["location"], "department": "",
               "url": r["link"], "posted": None, "first_seen": r["when"], "is_new": r["change"] == "New"}
        label = {"New": "", "Updated": "Updated ", "Removed": "Removed "}[r["change"]]
        cards.append(job_card(job, note).replace(f"Seen {nice_date(r['when'])}", f"{label}{ago(r['when'])}"))
    st.markdown("".join(cards), unsafe_allow_html=True)


def sidebar(store: Store) -> None:
    with st.sidebar:
        st.markdown("### Checks")
        st.caption("Every page is checked automatically once an hour on GitHub's servers.")
        if st.button("Check now", width="stretch", disabled=not store.remote,
                     type="primary" if st.session_state.get("just_added") else "secondary"):
            try:
                store.run_now()
                st.success("Check started. Results appear in a few minutes, and you'll get an email if anything "
                           "changed.")
                st.session_state["just_added"] = False
            except GitHubError as exc:
                st.error(str(exc))
        if store.remote:
            try:
                runs = store.github.recent_runs("monitor.yml", 4)
            except GitHubError:
                runs = []
            if runs:
                st.caption("Recent checks")
            for r in runs:
                icon = {"success": "✅", "failure": "❌", None: "⏳"}.get(r["conclusion"], "•")
                when = ago(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")))
                st.caption(f"{icon} [{when}]({r['url']})")
        else:
            st.caption("Local mode: changes save to config/companies.yaml on this computer.")
        if st.button("Refresh", width="stretch"):
            load_database.clear()
            preview.clear()
            st.rerun()


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
    sidebar(store)
    hero(sources, jobs, activity)
    t_jobs, t_add, t_pages, t_changes = st.tabs(
        [f"Jobs ({len(jobs)})", "Add a page", f"My pages ({len(sources)})", "Changes"])
    with t_jobs:
        jobs_tab(jobs)
    with t_add:
        add_tab(store)
    with t_pages:
        pages_tab(store, text, sha, sources, status)
    with t_changes:
        changes_tab(sessions, activity)


main()
