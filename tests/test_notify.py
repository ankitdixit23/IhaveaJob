"""Email digest: start marker, content, dedup, retries, escaping, health alerts."""

from __future__ import annotations

from sqlalchemy import select

from conftest import FakeClient
from ihaveajob.database.models import Notification, NotificationItem
from ihaveajob.http import FetchError
from ihaveajob.notifications.email import EmailSettings, NotificationError
from ihaveajob.services.notify import recipient_key, send_digests
from test_monitor import ASHBY_API, Harness, ashby_job, cfg

ME = "me@example.com"


class FakeNotifier:
    channel = "email"

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def send(self, to, subject, text, html=None):
        if self.fail:
            raise NotificationError("SMTP down")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})


def notify(h, notifier):
    return send_digests(h.sessions, notifier, [ME], now_fn=h.clock)


def test_first_notify_sends_welcome_and_skips_history(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})  # a "new" change before notifications were enabled
    n = FakeNotifier()
    (out,) = notify(h, n)
    assert out.status == "started" and "monitoring started" in n.sent[0]["subject"]
    assert "Acme" in n.sent[0]["text"]
    clock.advance(minutes=5)
    assert notify(h, n)[0].status == "nothing"  # history before the start marker is not emailed


def test_digest_lists_new_updated_and_removed_with_details(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    n = FakeNotifier()
    notify(h, n)
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1, title="Senior Climate Analyst 1"), ashby_job(2), ashby_job(3)]})
    (out,) = notify(h, n)
    mail = n.sent[-1]
    assert out.status == "sent" and out.items == 2
    assert "1 new" in mail["subject"] and "1 updated" in mail["subject"]
    assert "Climate Analyst 3" in mail["text"]
    assert "Title: Climate Analyst 1 → Senior Climate Analyst 1" in mail["text"]
    assert "https://jobs.ashbyhq.com/acme/job-3" in mail["html"]
    # Closures are reported once they are confirmed.
    for _ in range(4):
        clock.advance(hours=3)
        h.run({"jobs": [ashby_job(1, title="Senior Climate Analyst 1"), ashby_job(3)]})
    notify(h, n)
    assert "No longer listed".upper() in n.sent[-1]["text"] and "Climate Analyst 2" in n.sent[-1]["text"]


def test_same_change_is_never_sent_twice(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    n = FakeNotifier()
    notify(h, n)
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    assert notify(h, n)[0].status == "sent"
    assert notify(h, n)[0].status == "nothing"
    with sessions() as s:
        items = list(s.scalars(select(NotificationItem)))
    assert len(items) == 1 and items[0].recipient_key == recipient_key(ME)


def test_failed_send_is_retried_next_time(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    notify(h, FakeNotifier())
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    assert notify(h, FakeNotifier(fail=True))[0].status == "failed"
    ok = FakeNotifier()
    assert notify(h, ok)[0].status == "sent" and "Climate Analyst 2" in ok.sent[0]["text"]


def test_non_matching_jobs_are_not_emailed(sessions, settings, clock):
    h = Harness(sessions, settings, clock, cfg(keywords_any=["policy"]))
    h.run({"jobs": [ashby_job(1)]})
    n = FakeNotifier()
    notify(h, n)
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1), ashby_job(2), ashby_job(3, title="Climate Policy Lead")]})
    notify(h, n)
    assert "Climate Policy Lead" in n.sent[-1]["text"] and "Climate Analyst 2" not in n.sent[-1]["text"]


def test_untrusted_job_text_is_escaped_in_html(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    n = FakeNotifier()
    notify(h, n)
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1), ashby_job(2, title='<img src=x onerror=alert(1)>Analyst',
                                            jobUrl="javascript:alert(1)")]})
    notify(h, n)
    html = n.sent[-1]["html"]
    assert "<img" not in html and "&lt;img" in html and "javascript:" not in html


def test_persistent_source_failure_alerts_once(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    n = FakeNotifier()
    notify(h, n)
    for _ in range(4):
        clock.advance(hours=1)
        h.routes[ASHBY_API] = FetchError("blocked", "HTTP 403")
        h.run()
        notify(h, n)
    alerts = [m for m in n.sent if "need attention" in m["subject"]]
    assert len(alerts) == 1 and "failed 3 runs in a row" in alerts[0]["text"]


def test_email_settings_from_env(monkeypatch):
    for k in ("SMTP_HOST", "ALERT_EMAIL_TO", "SMTP_USERNAME", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    assert EmailSettings.from_env() is None
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.com, b@example.com")
    s = EmailSettings.from_env()
    assert s.port == 587 and s.recipients == ["a@example.com", "b@example.com"] and "sender@example.com" in s.sender


def test_no_email_addresses_are_stored(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    notify(h, FakeNotifier())
    with sessions() as s:
        for note in s.scalars(select(Notification)):
            assert ME not in (note.recipient_key + (note.subject or ""))


def test_sample_email_uses_real_jobs_and_records_nothing(sessions, settings, clock):
    from ihaveajob.services.notify import build_sample
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    with sessions() as s:
        digest = build_sample(s, clock.now)
    assert digest.subject.startswith("[SAMPLE]") and "Climate Analyst 1" in digest.text and "SAMPLE" in digest.html
    with sessions() as s:
        assert not list(s.scalars(select(NotificationItem))) and not list(s.scalars(select(Notification)))
