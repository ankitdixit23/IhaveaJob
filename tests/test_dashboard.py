"""Web app (local mode) + config editing + GitHub client error handling."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ihaveajob.config import ConfigError, add_source_text, edit_sources_text, parse_sources
from ihaveajob.github_api import GitHubError, GitHubRepo
from ihaveajob.models import SourceConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "# header comment\nsources:\n  - url: https://www.wri.org/careers\n  - key: acme\n    company: Acme\n" \
         "    adapter: lever\n    url: https://jobs.lever.co/acme\n    options: {site: acme}\n"


def test_add_and_edit_keep_header_and_validate():
    text = add_source_text(CONFIG, SourceConfig(key="ms", company="Microsoft", adapter="auto",
                                                 url="https://apply.careers.microsoft.com/careers?query=x"))
    assert text.startswith("# header comment") and [s.key for s in parse_sources(text)] == ["wri", "acme", "ms"]
    with pytest.raises(ConfigError, match="already being monitored"):
        add_source_text(text, SourceConfig(key="other", company="X", adapter="auto", url="https://www.wri.org/careers/"))

    def pause_acme(entries):
        next(e for e in entries if e.get("key") == "acme")["enabled"] = False
    edited = edit_sources_text(text, pause_acme)
    assert edited.startswith("# header comment")
    assert {s.key: s.enabled for s in parse_sources(edited)} == {"wri": True, "acme": False, "ms": True}
    removed = edit_sources_text(edited, lambda entries: entries.clear())
    assert parse_sources(removed) == [] and removed.startswith("# header comment")


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.text, self.content = status, payload or {}, "x", b"db"

    def json(self):
        return self._payload


@pytest.mark.parametrize("status,needle", [(401, "rejected the token"), (403, "lacks permission"),
                                           (404, "Not found"), (409, "changed on GitHub")])
def test_github_errors_are_explained(monkeypatch, status, needle):
    monkeypatch.setattr("ihaveajob.github_api.requests.request", lambda *a, **k: _Resp(status))
    with pytest.raises(GitHubError, match=needle):
        GitHubRepo("t", "o/r").get_text("config/companies.yaml")


def test_github_put_sends_sha_and_branch(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        return _Resp(200, {"commit": {"html_url": "https://github.com/o/r/commit/1"}})
    monkeypatch.setattr("ihaveajob.github_api.requests.request", fake)
    GitHubRepo("t", "o/r").put_text("config/companies.yaml", "sources: []\n", "abc", "msg")
    method, url, kw = calls[0]
    assert method == "PUT" and url.endswith("/repos/o/r/contents/config/companies.yaml")
    assert kw["json"]["sha"] == "abc" and kw["json"]["branch"] == "main"
    assert kw["headers"]["Authorization"] == "Bearer t"


@pytest.fixture
def app_copy(tmp_path, monkeypatch):
    st = pytest.importorskip("streamlit")
    st.cache_resource.clear()  # Streamlit caches are process-wide; isolate each test
    st.cache_data.clear()
    for name in ("src", "dashboard"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "companies.yaml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    for var in ("GITHUB_TOKEN", "GITHUB_REPO", "APP_PASSWORD", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_app_lists_adds_pauses_and_removes_pages(app_copy):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(app_copy / "dashboard" / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Jobs (0)", "Add a page", "My pages (2)", "Changes"]
    at.text_input(key="add_url").input("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?q=climate")
    at.text_input(key="add_company").input("NVIDIA")
    next(b for b in at.button if b.label == "Add page").click().run()
    assert not at.exception and at.success
    cfg = app_copy / "config" / "companies.yaml"
    added = {s.key: s for s in parse_sources(cfg.read_text())}["nvidia-climate"]
    assert added.adapter == "workday" and added.options["search_text"] == "climate"
    next(b for b in at.button if b.key == "toggle-acme").click().run()
    assert {s.key: s.enabled for s in parse_sources(cfg.read_text())}["acme"] is False
    next(b for b in at.button if b.key == "rm-wri").click().run()
    assert "wri" not in {s.key for s in parse_sources(cfg.read_text())}
    assert cfg.read_text().startswith("# header comment")


def test_app_shows_jobs_with_search_and_escapes_page_text(app_copy, tmp_path):
    from datetime import datetime, timedelta, timezone

    from streamlit.testing.v1 import AppTest

    from ihaveajob.database import make_engine, session_factory, upgrade
    from ihaveajob.database.models import Company, Job, JobChange, Source

    engine = make_engine(f"sqlite:///{app_copy}/data/ihaveajob.db")
    upgrade(engine)
    now = datetime.now(timezone.utc)
    with session_factory(engine)() as s:
        co = Company(slug="acme", name="Acme")
        src = Source(key="acme", company=co, adapter="lever", url="https://jobs.lever.co/acme", filters={})
        s.add_all([co, src]); s.flush()
        for i, title in enumerate(["Climate Data Analyst", "<script>alert(1)</script> Policy Lead"]):
            job = Job(source_id=src.id, company_id=co.id, external_id=str(i), title=title, company_name="Acme",
                      locations=[], extra={}, url=f"https://jobs.lever.co/acme/{i}", first_detected_at=now,
                      last_seen_at=now, content_hash="x", is_baseline=i == 0)
            s.add(job); s.flush()
            s.add(JobChange(job_id=job.id, change_type="baseline" if i == 0 else "new", changed_fields={},
                            detected_at=now - timedelta(minutes=5)))
        s.commit()
    at = AppTest.from_file(str(app_copy / "dashboard" / "app.py"), default_timeout=60).run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "1 new job since yesterday at Acme." in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    at.text_input(key="job_search").input("climate").run()
    page = " ".join(m.value for m in at.tabs[0].markdown)
    assert "Climate Data Analyst" in page and "Policy Lead" not in page
    at.text_input(key="job_search").input("").run()
    at.toggle(key="job_only_new").set_value(True).run()
    page = " ".join(m.value for m in at.tabs[0].markdown)
    assert "Policy Lead" in page and "Climate Data Analyst" not in page


def test_app_password_gate(app_copy, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(str(app_copy / "dashboard" / "app.py"), default_timeout=60).run()
    assert not at.tabs  # nothing shown before login
    at.text_input[0].input("wrong").run()
    assert at.error
    at.text_input[0].input("s3cret").run()
    assert at.tabs
