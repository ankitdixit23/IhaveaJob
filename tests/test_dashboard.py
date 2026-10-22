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
    pytest.importorskip("streamlit")
    for name in ("src", "dashboard"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "companies.yaml").write_text(CONFIG)
    for var in ("GITHUB_TOKEN", "GITHUB_REPO", "APP_PASSWORD", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_app_lists_adds_pauses_and_removes_pages(app_copy):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(app_copy / "dashboard" / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert [m.value for m in at.metric][:1] == ["2"]
    at.text_input[0].input("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?q=climate")
    at.text_input[1].input("NVIDIA")
    at.button[1].click().run()  # Add page
    assert not at.exception and at.success
    cfg = app_copy / "config" / "companies.yaml"
    added = {s.key: s for s in parse_sources(cfg.read_text())}["nvidia-climate"]
    assert added.adapter == "workday" and added.options["search_text"] == "climate"
    pause = next(b for b in at.button if b.key == "toggle-acme")
    pause.click().run()
    assert {s.key: s.enabled for s in parse_sources(cfg.read_text())}["acme"] is False
    next(b for b in at.button if b.key == "rm-wri").click().run()
    assert "wri" not in {s.key for s in parse_sources(cfg.read_text())}
    assert cfg.read_text().startswith("# header comment")


def test_app_password_gate(app_copy, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(str(app_copy / "dashboard" / "app.py"), default_timeout=60).run()
    assert not at.tabs  # nothing shown before login
    at.text_input[0].input("wrong").run()
    assert at.error
    at.text_input[0].input("s3cret").run()
    assert at.tabs
