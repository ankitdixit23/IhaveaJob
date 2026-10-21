"""HTTP client, robots.txt, normalization and config tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ihaveajob.config import ConfigError, append_source, load_sources
from ihaveajob.http import FetchError, HttpClient
from ihaveajob.models import SourceConfig, SourceFilters
from ihaveajob.normalization import canonical_url, content_hash, html_to_text, parse_datetime
from ihaveajob.robots import RobotsPolicy


# --- HTTP ------------------------------------------------------------------
class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, n, decode_content=True):
        return self.body[:n]


class _Resp:
    def __init__(self, status, body=b"ok", headers=None):
        self.status_code, self.headers, self.raw = status, headers or {}, _Raw(body)
        self._content = None

    @property
    def text(self):
        return self._content.decode()

    def json(self):
        import json
        return json.loads(self.text)

    def close(self):
        pass


class _Session:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client_with(responses, **kw):
    sleeps = []
    session = _Session(responses)
    return HttpClient(session=session, sleep=sleeps.append, min_interval_per_host=0, **kw), session, sleeps


def test_retries_server_errors_then_succeeds():
    client, session, sleeps = client_with([_Resp(503), _Resp(200, b'{"a": 1}')])
    assert client.get_json("https://x.test/a") == {"a": 1}
    assert session.calls == 2 and sleeps == [2.0]


def test_honours_retry_after_on_429():
    client, session, sleeps = client_with([_Resp(429, headers={"Retry-After": "7"}), _Resp(200)])
    assert client.get_text("https://x.test/a") == "ok" and sleeps == [7.0]


def test_long_retry_after_fails_fast_instead_of_blocking_the_run():
    client, session, _ = client_with([_Resp(429, headers={"Retry-After": "3600"})])
    with pytest.raises(FetchError) as exc:
        client.get_text("https://x.test/a")
    assert exc.value.kind == "rate_limited" and session.calls == 1


def test_client_errors_are_not_retried():
    client, session, _ = client_with([_Resp(404)])
    with pytest.raises(FetchError) as exc:
        client.get_text("https://x.test/a")
    assert exc.value.status == 404 and session.calls == 1


def test_gives_up_after_max_attempts_on_timeouts():
    import requests
    client, session, _ = client_with([requests.Timeout("t")] * 3)
    with pytest.raises(FetchError) as exc:
        client.get_text("https://x.test/a")
    assert exc.value.kind == "timeout" and session.calls == 3


def test_oversized_responses_are_rejected():
    client, _, _ = client_with([_Resp(200, b"x" * 101)], max_bytes=100)
    with pytest.raises(FetchError) as exc:
        client.get_text("https://x.test/a")
    assert exc.value.kind == "too_large"


def test_robots_disallow_blocks_fetch_before_requesting_page():
    robots = b"User-agent: *\nDisallow: /private\n"
    client, session, _ = client_with([_Resp(200, robots)])
    with pytest.raises(FetchError) as exc:
        client.get_text("https://x.test/private/jobs", check_robots=True)
    assert exc.value.kind == "robots_disallowed" and session.calls == 1


def test_missing_robots_txt_means_allowed():
    client, _, _ = client_with([_Resp(404), _Resp(200, b"jobs")])
    assert client.get_text("https://x.test/jobs", check_robots=True) == "jobs"


# --- robots.txt evaluation ---------------------------------------------------
GOOGLE_LIKE = """
User-agent: Yandex
Disallow: /about/careers/applications/jobs/results

User-agent: *
Disallow: /about/careers/applications/candidate-prep
Disallow: /about/careers/applications/jobs/results?page=
Disallow: /about/careers/applications/jobs/results?*&page=
Allow: /about/careers/applications/jobs/results/special$
"""


@pytest.mark.parametrize("path,allowed", [
    ("/about/careers/applications/jobs/results?q=sustainability", True),
    ("/about/careers/applications/jobs/results?q=sustainability&page=2", False),
    ("/about/careers/applications/jobs/results?page=2", False),
    ("/about/careers/applications/jobs/results/123-title", True),
    ("/about/careers/applications/candidate-prep/x", False),
])
def test_robots_wildcards(path, allowed):
    policy = RobotsPolicy(GOOGLE_LIKE)
    assert policy.is_allowed("https://www.google.com" + path, "IhaveaJobBot/0.1") is allowed


def test_robots_specific_agent_group_takes_precedence():
    policy = RobotsPolicy(GOOGLE_LIKE)
    assert not policy.is_allowed("https://www.google.com/about/careers/applications/jobs/results?q=x", "YandexBot")


# --- normalization -----------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("http://WWW.Example.com/jobs/1/?utm_source=x&gh_jid=5#apply", "https://example.com/jobs/1?gh_jid=5"),
    ("https://www.google.com/about/careers/applications/jobs/results/1-x?q=climate",
     "https://www.google.com/about/careers/applications/jobs/results/1-x"),
    (None, None),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


@pytest.mark.parametrize("raw,expected,precision", [
    ("2026-10-02", datetime(2026, 10, 2, tzinfo=timezone.utc), "date"),
    ("2026-09-23T16:50:04-04:00", datetime(2026, 9, 23, 20, 50, 4, tzinfo=timezone.utc), "second"),
    ("2026-09-01T10:00:00Z", datetime(2026, 9, 1, 10, tzinfo=timezone.utc), "second"),
    (1790000000, datetime.fromtimestamp(1790000000, timezone.utc), "second"),
    (1790000000000, datetime.fromtimestamp(1790000000, timezone.utc), "second"),
    ("not a date", None, None), (None, None, None),
])
def test_parse_datetime(raw, expected, precision):
    assert parse_datetime(raw) == (expected, precision)


def test_content_hash_ignores_whitespace_and_case_only_changes():
    a = {"title": "Data  Engineer", "description_text": "Build\npipelines"}
    b = {"title": "data engineer", "description_text": "Build pipelines "}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({**a, "title": "Senior Data Engineer"})


def test_html_to_text_strips_scripts_and_keeps_structure():
    text = html_to_text("<p>Hello&nbsp;<b>world</b></p><script>steal()</script><ul><li>A</li><li>B</li></ul>")
    assert "steal" not in text and text.splitlines() == ["Hello world", "A", "B"]


# --- config --------------------------------------------------------------------
def test_repository_config_is_valid():
    keys = [s.key for s in load_sources()]
    assert "watershed" in keys


def test_append_source_roundtrip_and_duplicate_rejected(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("# comment kept\nsources: []\n")
    src = SourceConfig(key="acme-data", company="Acme", adapter="lever", url="https://jobs.lever.co/acme",
                       filters=SourceFilters(keywords_any=["data"]), options={"site": "acme"})
    append_source(src, path)
    loaded = load_sources(path)
    assert loaded[0].filters.keywords_any == ["data"] and "# comment kept" in path.read_text()
    with pytest.raises(ConfigError):
        append_source(src, path)


@pytest.mark.parametrize("yaml_text,message", [
    ("sources:\n  - key: a\n", "missing"),
    ("sources:\n  - {key: Bad Key, company: X, adapter: lever, url: u}\n", "invalid key"),
    ("sources:\n  - {key: ab, company: X, adapter: nope, url: u}\n", "unknown adapter"),
    ("nothing: true\n", "sources:"),
])
def test_invalid_configs_are_rejected(tmp_path, yaml_text, message):
    path = tmp_path / "c.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ConfigError, match=message):
        load_sources(path)
