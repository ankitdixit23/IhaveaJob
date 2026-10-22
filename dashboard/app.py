"""IhaveaJob web app: add careers pages, see what's monitored, and recent changes.

Run locally:   streamlit run dashboard/app.py
Deploy:        Streamlit Community Cloud (see docs/dashboard.md)

With GITHUB_TOKEN + GITHUB_REPO secrets the app edits config/companies.yaml in
the GitHub repository and reads the job database from the `data` branch.
Without them it works on the local files (handy for development).
"""

from __future__ import annotations

import hmac
import os
import sys
import tempfile
from datetime import datetime, timezone
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
from ihaveajob.services.status import recent_changes, source_status  # noqa: E402

CONFIG_PATH = "config/companies.yaml"
FETCH_HINTS = {
    "not_found": "This page doesn't exist (404). Open the link in your browser to check it.",
    "blocked": "This site refuses automated visitors (bot protection or login), so it can't be monitored.",
    "robots_disallowed": "This site's robots.txt forbids automated reading of this page, so it can't be monitored.",
    "timeout": "The site didn't respond in time. Try again, or add it anyway: the hourly check will retry.",
    "connection": "Couldn't reach the site from here. Check the link, or add it anyway: the hourly check will retry.",
    "http_status": "The site returned an error. Try again later, or add it anyway: the hourly check will retry.",
}

st.set_page_config(page_title="IhaveaJob", page_icon="💼", layout="centered")


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
    st.title("IhaveaJob")
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw, expected):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
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
            raise GitHubError("Running checks from the app needs GITHUB_TOKEN and GITHUB_REPO. Locally, run: ihaveajob run")
        self.github.run_workflow("monitor.yml")


@st.cache_resource(ttl=300, show_spinner=False)
def load_database(remote: bool):
    """Session factory for the job database (data branch copy when deployed)."""
    db_url = setting("DATABASE_URL")
    if db_url:
        return session_factory(make_engine(db_url))
    if remote:
        store = Store()
        blob = store.github.get_bytes("ihaveajob.db", ref="data")
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
        "jobs": [{"Title": j.title, "Location": j.location or j.extra.get("card_text", ""), "Link": j.url}
                 for j in hits[:25]],
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


# --- UI ------------------------------------------------------------------------------
def header(store: Store, sources, status) -> None:
    st.title("💼 IhaveaJob")
    st.caption("Track any company's careers page. Get an email when jobs are added, changed or removed.")
    last = max((s["last_checked"] for s in status.values() if s["last_checked"]), default=None)
    matching = sum(status.get(s.key, {}).get("matching", 0) for s in sources if s.enabled)
    c1, c2, c3 = st.columns(3)
    c1.metric("Pages monitored", sum(1 for s in sources if s.enabled))
    c2.metric("Jobs tracked", matching)
    c3.metric("Last check", ago(last))
    if not store.remote:
        st.info("Local mode: changes are saved to config/companies.yaml on this computer. "
                "Set GITHUB_TOKEN and GITHUB_REPO to manage the live monitor.", icon="💻")


def add_tab(store: Store) -> None:
    st.subheader("Add a careers page")
    st.write("Open the company's careers page, search or filter it for your field, then copy the link from the "
             "address bar. **Any company works.**")
    with st.form("add"):
        url = st.text_input("Careers page link", placeholder="https://company.com/careers?q=climate")
        company = st.text_input("Company name (optional)", placeholder="Worked out from the link if left blank")
        with st.expander("Only email me about some jobs (optional)"):
            keywords = st.text_input("Keywords", placeholder="climate, sustainability, data",
                                     help="Only jobs mentioning at least one of these.")
            locations = st.text_input("Locations", placeholder="New York, Remote")
            exclude = st.text_input("Exclude", placeholder="intern, sales",
                                    help="Skip jobs whose title mentions any of these.")
        c1, c2 = st.columns(2)
        do_preview = c1.form_submit_button("🔍 Preview", width="stretch")
        do_add = c2.form_submit_button("➕ Add page", type="primary", width="stretch")

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
        st.success(f"**{source.company}** added. The next check records its current jobs (no email for those); "
                   "after that you'll be emailed about every change.", icon="✅")
        st.session_state["just_added"] = True
    except (UnsupportedUrl, ConfigError, GitHubError) as exc:
        st.error(str(exc))


def show_preview(p: dict) -> None:
    src = p["source"]
    if p.get("error"):
        st.error(FETCH_HINTS.get(p["error"], p["message"]))
        if p["error"] in ("timeout",):
            st.caption(p["message"])
        return
    st.markdown(f"**{src.company}**: read via `{p['mode']}`")
    c1, c2 = st.columns(2)
    c1.metric("Jobs found", p["found"])
    c2.metric("Matching your filters", p["matching"])
    if p["jobs"]:
        st.dataframe(pd.DataFrame(p["jobs"]), hide_index=True, width="stretch",
                     column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open")})
    elif p["page_text"] is not None:
        st.info("No individual jobs found in the page's HTML. Many sites build their job list with JavaScript: the "
                "hourly check opens those pages in a real browser, so it usually finds the jobs there. Either way, "
                "you'll be emailed when the page changes.", icon="ℹ️")
    for note in p["notes"]:
        st.caption(f"• {note}")


def pages_tab(store: Store, text: str, sha: str | None, sources, status) -> None:
    st.subheader("My pages")
    if not sources:
        st.info("No pages yet. Add one in the first tab.")
        return
    for src in sorted(sources, key=lambda s: s.company.lower()):
        info = status.get(src.key)
        with st.container(border=True):
            left, right = st.columns([4, 1.4])
            with left:
                badge = "🟢" if src.enabled and not (info and info["failures_in_a_row"] >= 3) else (
                    "⏸️" if not src.enabled else "🔴")
                st.markdown(f"{badge} **{src.company}**  \n[{src.url}]({src.url})")
                f = src.filters.to_dict()
                if f:
                    st.caption(" · ".join(f"{k.replace('_any', '').replace('keywords_exclude', 'exclude')}: "
                                          f"{', '.join(v)}" for k, v in f.items()))
                if not src.enabled:
                    st.caption("Paused")
                elif info is None:
                    st.caption("Waiting for its first check (runs hourly, or use **Check all pages now**).")
                else:
                    st.caption(f"{info['matching']} jobs tracked · checked {ago(info['last_checked'])} · "
                               f"read via {info['read_via']}")
                    if info["error"]:
                        st.caption(f"⚠️ Last check failed ({info['failures_in_a_row']}× in a row): {info['error'][:160]}")
            with right:
                if st.button("Resume" if not src.enabled else "Pause", key=f"toggle-{src.key}",
                             width="stretch"):
                    _edit(store, text, sha, src.key, "toggle", src.company)
                with st.popover("Remove", width="stretch"):
                    st.write(f"Stop monitoring **{src.company}**? Its history is kept.")
                    if st.button("Yes, remove", key=f"rm-{src.key}", type="primary"):
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
        new_text = edit_sources_text(text, apply)
        verb = "Remove" if action == "remove" else "Pause/resume"
        store.save_config(new_text, sha, f"{verb} careers page: {company}")
        st.toast(f"{company} updated", icon="✅")
        st.rerun()
    except (ConfigError, GitHubError) as exc:
        st.error(str(exc))


def changes_tab(sessions) -> None:
    st.subheader("Recent changes")
    if sessions is None:
        st.info("No checks have run yet.")
        return
    hours = st.segmented_control("Show", options=[24, 48, 168], default=48,
                                 format_func=lambda h: "1 week" if h == 168 else f"{h} hours") or 48
    with sessions() as session:
        rows = recent_changes(session, hours=hours)
    if not rows:
        st.info("No changes in this period. You'll get an email as soon as something changes.")
        return
    df = pd.DataFrame(rows)
    df["when"] = df["when"].map(ago)
    st.dataframe(df[["when", "change", "company", "title", "location", "what_changed", "link"]], hide_index=True,
                 width="stretch",
                 column_config={"link": st.column_config.LinkColumn("Link", display_text="Open"),
                                "what_changed": "What changed", "when": "When", "change": "Change",
                                "company": "Company", "title": "Job", "location": "Location"})


def sidebar(store: Store) -> None:
    with st.sidebar:
        st.markdown("### Checks")
        st.caption("Pages are checked automatically every hour on GitHub's servers.")
        if st.button("🔄 Check all pages now", width="stretch", disabled=not store.remote,
                     type="primary" if st.session_state.get("just_added") else "secondary"):
            try:
                store.run_now()
                st.success("Started. Results in a few minutes (and an email if anything changed).")
                st.session_state["just_added"] = False
            except GitHubError as exc:
                st.error(str(exc))
        if store.remote:
            try:
                runs = store.github.recent_runs("monitor.yml", 3)
            except GitHubError:
                runs = []
            for r in runs:
                icon = {"success": "✅", "failure": "❌", None: "⏳"}.get(r["conclusion"], "•")
                when = ago(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")))
                st.caption(f"{icon} [{when}]({r['url']})")
        if st.button("Refresh data", width="stretch"):
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
        st.title("💼 IhaveaJob")
        st.error(f"Couldn't load your pages: {exc}")
        return
    try:
        sessions = load_database(store.remote)
    except Exception as exc:  # noqa: BLE001 - show status even if the DB can't be read
        st.warning(f"Job history unavailable right now: {exc}")
        sessions = None
    status = {}
    if sessions is not None:
        with sessions() as session:
            status = source_status(session)
    sidebar(store)
    header(store, sources, status)
    tab_add, tab_pages, tab_changes = st.tabs(["➕ Add a page", f"📋 My pages ({len(sources)})", "🔔 Recent changes"])
    with tab_add:
        add_tab(store)
    with tab_pages:
        pages_tab(store, text, sha, sources, status)
    with tab_changes:
        changes_tab(sessions)


main()
