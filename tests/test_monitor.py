"""Change detection, baseline, dedup and false-closure protection."""

from __future__ import annotations

import copy
import json

from sqlalchemy import select

from conftest import FakeClient, fixture_text
from ihaveajob.database.models import Job, JobChange, Source, SourceRun
from ihaveajob.http import FetchError
from ihaveajob.models import SourceConfig, SourceFilters
from ihaveajob.services.monitor import run_monitor

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/acme"


def ashby_job(i: int, **overrides):
    job = {"id": f"job-{i}", "title": f"Climate Analyst {i}", "location": "Remote", "department": "Data",
           "isListed": True, "publishedAt": "2026-09-01T00:00:00Z", "jobUrl": f"https://jobs.ashbyhq.com/acme/job-{i}",
           "descriptionPlain": f"Synthetic description {i} about emissions data."}
    job.update(overrides)
    return job


def cfg(**filters) -> list[SourceConfig]:
    return [SourceConfig(key="acme", company="Acme", adapter="ashby", url="https://jobs.ashbyhq.com/acme",
                         options={"board": "acme"}, filters=SourceFilters(**filters))]


class Harness:
    def __init__(self, sessions, settings, clock, configs=None):
        self.sessions, self.settings, self.clock = sessions, settings, clock
        self.configs = configs or cfg()
        self.routes: dict = {}

    def run(self, payload=None):
        if payload is not None:
            self.routes[ASHBY_API] = payload
        return run_monitor(self.sessions, self.configs, self.settings,
                           client_factory=lambda: FakeClient(self.routes), now_fn=self.clock)

    def jobs(self):
        with self.sessions() as s:
            return {j.external_id: j for j in s.scalars(select(Job))}

    def changes(self, kind=None):
        with self.sessions() as s:
            q = select(JobChange)
            if kind:
                q = q.where(JobChange.change_type == kind)
            return list(s.scalars(q))


def test_first_run_is_baseline_and_never_reports_new(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    summary = h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    src = summary.sources[0]
    assert src.baseline and src.new == 0 and src.fetched == 2
    assert not h.changes("new") and len(h.changes("baseline")) == 2
    assert all(j.is_baseline for j in h.jobs().values())


def test_second_run_reports_only_genuinely_new_jobs(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    clock.advance(minutes=30)
    summary = h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    assert summary.sources[0].new == 1
    new = h.changes("new")
    assert len(new) == 1 and h.jobs()["job-2"].is_baseline is False
    # Re-running with the same data is idempotent: nothing new or updated.
    clock.advance(minutes=30)
    again = h.run({"jobs": [ashby_job(1), ashby_job(2)]}).sources[0]
    assert (again.new, again.updated) == (0, 0)


def test_publication_time_is_separate_from_first_detected(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1, publishedAt="2026-08-15T08:00:00Z")]})
    job = h.jobs()["job-1"]
    assert job.published_at.isoformat() == "2026-08-15T08:00:00+00:00"
    assert job.first_detected_at == clock.now and job.published_at_source == "ashby.publishedAt"


def test_updates_are_recorded_with_field_diff(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    clock.advance(hours=1)
    summary = h.run({"jobs": [ashby_job(1, title="Senior Climate Analyst 1", descriptionPlain="Rewritten.")]})
    assert summary.sources[0].updated == 1
    (change,) = h.changes("updated")
    assert change.changed_fields["title"] == {"old": "Climate Analyst 1", "new": "Senior Climate Analyst 1"}
    assert "old_len" in change.changed_fields["description_text"]


def test_failed_fetch_changes_nothing_and_counts_failures(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    for _ in range(5):
        clock.advance(hours=3)
        h.routes[ASHBY_API] = FetchError("timeout", "simulated timeout")
        summary = h.run()
    assert summary.sources[0].status == "failed" and summary.sources[0].consecutive_failures == 5
    assert summary.persistent_failures
    assert all(j.status == "open" for j in h.jobs().values()) and not h.changes("closed")
    with sessions() as s:
        assert s.scalars(select(SourceRun).order_by(SourceRun.id.desc())).first().error_kind == "timeout"


def test_malformed_response_does_not_close_jobs(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    for _ in range(4):
        clock.advance(hours=3)
        summary = h.run({"unexpected": "shape"})
    assert summary.sources[0].error.startswith("parse_error")
    assert h.jobs()["job-1"].status == "open"


def test_closure_requires_repeated_misses_and_minimum_age(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    for expected_missing in (1, 2, 3):
        clock.advance(minutes=30)
        h.run({"jobs": [ashby_job(1)]})
        assert h.jobs()["job-2"].missing_count == expected_missing
    # 3 misses but only 1.5h since last seen (< 6h): still not closed.
    assert h.jobs()["job-2"].status == "missing" and not h.changes("closed")
    clock.advance(hours=5)
    summary = h.run({"jobs": [ashby_job(1)]})
    assert summary.sources[0].closed == 1 and h.jobs()["job-2"].status == "closed"


def test_closed_job_that_reappears_is_reopened(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    for _ in range(4):
        clock.advance(hours=3)
        h.run({"jobs": [ashby_job(1)]})
    assert h.jobs()["job-2"].status == "closed"
    clock.advance(hours=1)
    summary = h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    assert summary.sources[0].reopened == 1 and summary.sources[0].new == 0
    job = h.jobs()["job-2"]
    assert job.status == "open" and job.closed_at is None and job.missing_count == 0


def test_temporary_absence_resets_missing_count(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    clock.advance(hours=3); h.run({"jobs": [ashby_job(1)]})
    clock.advance(hours=3); h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    job = h.jobs()["job-2"]
    assert job.status == "open" and job.missing_count == 0 and not h.changes("reopened")


def test_sudden_large_drop_is_treated_as_suspicious(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(i) for i in range(10)]})
    for _ in range(4):
        clock.advance(hours=3)
        summary = h.run({"jobs": [ashby_job(0), ashby_job(1)]})
    assert "suspicious" in summary.sources[0].closure_check
    assert all(j.missing_count == 0 for j in h.jobs().values()) and not h.changes("closed")


def test_empty_response_after_jobs_existed_is_suspicious(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    clock.advance(hours=3)
    summary = h.run({"jobs": []})
    assert "suspicious" in summary.sources[0].closure_check and h.jobs()["job-1"].status == "open"


def test_incomplete_fetch_never_closes(sessions, settings, clock):
    configs = [SourceConfig(key="g", company="Google", adapter="google_careers",
                            url="https://www.google.com/about/careers/applications/jobs/results?q=x")]
    h = Harness(sessions, settings, clock, configs)
    url = configs[0].url
    full = fixture_text("google_results.html")
    capped_one = full.replace("]],null,2,20]", "]],null,90,20]")
    # Remove the second record to simulate it falling off a capped first page.
    start = capped_one.index(',["222222222222222222"')
    end = capped_one.index("]],null,90,20]") + 1  # keep the closing bracket of the jobs list
    capped_one = capped_one[:start] + capped_one[end:]
    h.routes[url] = full
    h.run()
    for _ in range(5):
        clock.advance(hours=3)
        h.routes[url] = capped_one
        summary = h.run()
    assert summary.sources[0].status == "partial" and not summary.sources[0].complete
    assert h.jobs()["222222222222222222"].status == "open" and not h.changes("closed")


def test_dedup_by_canonical_url_when_source_changes_id(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1, jobUrl="https://jobs.ashbyhq.com/acme/stable?utm_source=x")]})
    clock.advance(hours=1)
    summary = h.run({"jobs": [ashby_job(1, id="job-1-renumbered", jobUrl="https://jobs.ashbyhq.com/acme/stable/")]})
    jobs = h.jobs()
    assert list(jobs) == ["job-1-renumbered"] and summary.sources[0].new == 0
    assert any("external id changed" in n for n in summary.sources[0].notes)


def test_duplicate_ids_in_one_response_are_stored_once(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    summary = h.run({"jobs": [ashby_job(1), copy.deepcopy(ashby_job(1))]})
    assert summary.sources[0].fetched == 1 and len(h.jobs()) == 1


def test_missing_fields_never_erase_stored_values(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    clock.advance(hours=1)
    summary = h.run({"jobs": [ashby_job(1, descriptionPlain=None, department=None, publishedAt=None)]})
    job = h.jobs()["job-1"]
    assert job.description_text and job.department == "Data" and job.published_at is not None
    assert summary.sources[0].updated == 0


def test_filters_mark_matching_jobs_without_dropping_presence(sessions, settings, clock):
    h = Harness(sessions, settings, clock, cfg(keywords_any=["policy"]))
    h.run({"jobs": [ashby_job(1), ashby_job(2, title="Climate Policy Lead")]})
    jobs = h.jobs()
    assert jobs["job-2"].matches_filters and not jobs["job-1"].matches_filters
    assert len(jobs) == 2  # non-matching jobs are still tracked so closures/updates stay correct


def test_changing_source_url_rebuilds_baseline(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    h.configs = [SourceConfig(key="acme", company="Acme", adapter="ashby", url="https://jobs.ashbyhq.com/acme2",
                              options={"board": "acme"})]
    clock.advance(hours=1)
    summary = h.run({"jobs": [ashby_job(1), ashby_job(2)]})
    assert summary.sources[0].baseline and summary.sources[0].new == 0


def test_one_failing_source_does_not_block_others(sessions, settings, clock):
    configs = cfg() + [SourceConfig(key="other", company="Other", adapter="lever", url="https://jobs.lever.co/other",
                                    options={"site": "other"})]
    h = Harness(sessions, settings, clock, configs)
    h.routes["https://api.lever.co/v0/postings/other"] = FetchError("http_status", "HTTP 503", status=503)
    summary = h.run({"jobs": [ashby_job(1)]})
    statuses = {s.key: s.status for s in summary.sources}
    assert statuses == {"acme": "success", "other": "failed"} and summary.status == "partial"
    assert "job-1" in h.jobs()


def test_removed_source_is_disabled_but_history_kept(sessions, settings, clock):
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1)]})
    h.configs = []
    h.run()
    with sessions() as s:
        assert s.scalars(select(Source)).one().enabled is False
    assert "job-1" in h.jobs()


def test_watershed_details_fetched_once_then_refreshed_after_interval(sessions, settings, clock):
    url = "https://watershed.com/careers"
    configs = [SourceConfig(key="watershed", company="Watershed", adapter="watershed", url=url)]
    h = Harness(sessions, settings, clock, configs)
    h.routes[url] = fixture_text("watershed_listing.html")
    for ext, slug in [("aaaa0001", "fixture-data-engineer"), ("aaaa0002", "fixture-ml-engineer"),
                      ("aaaa0003", "fixture-sustainability-advisor")]:
        h.routes[f"{url}/{ext}-{slug}"] = fixture_text("watershed_job.html")
    clients = []

    def run():
        def factory():
            clients.append(FakeClient(h.routes))
            return clients[-1]
        return run_monitor(sessions, configs, settings, client_factory=factory, now_fn=clock)

    run()
    assert len(clients[-1].requested) == 4  # listing + 3 details
    job = h.jobs()["aaaa0001"]
    assert job.published_at.date().isoformat() == "2026-09-15" and job.description_text
    clock.advance(hours=1)
    run()
    assert clients[-1].requested == [url]  # details are fresh: listing only
    clock.advance(hours=24)
    run()
    assert len(clients[-1].requested) == 4  # daily refresh
    with sessions() as s:
        run_row = s.scalars(select(SourceRun).order_by(SourceRun.id.desc())).first()
        assert run_row.http_requests == 4 and json.dumps(run_row.notes)
