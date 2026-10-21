"""Universal ("auto") reader and Workday collector, with synthetic fixtures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from conftest import FakeClient, fixture_text
from ihaveajob.collectors import CollectContext
from ihaveajob.collectors.generic import (
    GenericPageCollector, discover_board, extract_job_links, find_listing_link, looks_paginated,
)
from ihaveajob.collectors.workday import WorkdayCollector, _posted_on
from ihaveajob.config import load_sources
from ihaveajob.database.models import Job, JobChange, PageSnapshot, Source
from ihaveajob.http import FetchError
from ihaveajob.models import SourceConfig, SourceFilters
from ihaveajob.services.monitor import run_monitor

PAGE = "https://fixture.example.com/careers/search"


def auto_source(url=PAGE, **opts):
    return SourceConfig(key="fixture", company="Fixture", adapter="auto", url=url, options=opts)


# --- link extraction ---------------------------------------------------------
def test_extracts_job_cards_and_ignores_noise():
    jobs = {j.external_id: j for j in extract_job_links(fixture_text("generic_cards.html"), PAGE, "Fixture")}
    assert set(jobs) == {"1970000000000001", "200100200", "JR1234567"}  # no news/asset/nav/external links
    ms = jobs["1970000000000001"]
    assert ms.title == "Climate Data Analyst"
    assert ms.url == "https://fixture.example.com/careers/job/1970000000000001"  # not the apply link
    assert "New York" in ms.extra["card_text"]
    apple = jobs["200100200"]
    assert apple.title == "Sustainability Program Manager" and "locationPicker" not in apple.url
    assert jobs["JR1234567"].title == "Policy Researcher"  # from aria-label


def test_pagination_is_detected():
    html = fixture_text("generic_cards.html")
    assert looks_paginated(html, 3)
    assert not looks_paginated("<html><a href='/careers/job/12345'>Analyst</a></html>", 1)


def test_board_discovery_prefers_known_feed():
    html = ('<a href="https://rockymountain.wd1.myworkdayjobs.com/en-US/RMI?redirect=/en-US/RMI/userHome">Jobs</a>'
            '<a href="https://jobs.lever.co/acme/123">one</a>')
    assert discover_board(html) in ("https://rockymountain.wd1.myworkdayjobs.com/RMI", "https://jobs.lever.co/acme")
    assert discover_board("<p>no boards here</p>") is None


def test_listing_link_is_found():
    html = '<a href="/careers/benefits">Benefits</a><a href="/careers/jobs">Explore Job Openings</a>'
    assert find_listing_link(html, "https://www.example.org/careers") == "https://www.example.org/careers/jobs"


# --- the full resolution chain ---------------------------------------------
def test_auto_follows_listing_link_then_reads_jobs():
    landing = '<a href="/careers/jobs">Explore Job Openings</a>'
    listing = '<ul><li><a href="/careers/jobs/8765432/analyst">Analyst</a></li></ul>'
    client = FakeClient({"https://www.example.org/careers": landing, "https://www.example.org/careers/jobs": listing,
                         "https://www.example.org/careers/jobs/8765432/analyst": "<html>no jsonld</html>"})
    result = GenericPageCollector().fetch(auto_source("https://www.example.org/careers"), client, CollectContext())
    assert [j.title for j in result.jobs] == ["Analyst"] and result.complete
    assert result.resolved["mode"] == "links:linked listings page"
    assert any("Followed" in n for n in result.notes)


def test_auto_delegates_to_embedded_board():
    page = '<a href="https://jobs.lever.co/acme">See all jobs</a>'
    lever = [{"id": "l1", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/l1", "categories": {}}]
    client = FakeClient({PAGE: page, "https://api.lever.co/v0/postings/acme": lever})
    result = GenericPageCollector().fetch(auto_source(), client, CollectContext())
    assert [j.external_id for j in result.jobs] == ["l1"] and result.resolved["mode"] == "board:lever"


def test_auto_reads_jsonld_postings():
    page = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"JobPosting",'
            '"title":"Grid Analyst","url":"https://fixture.example.com/j/1","datePosted":"2026-10-01",'
            '"jobLocation":{"address":{"addressLocality":"Denver","addressRegion":"CO"}},'
            '"description":"<p>Model the grid.</p>"}]}</script>')
    result = GenericPageCollector().fetch(auto_source(), FakeClient({PAGE: page}), CollectContext())
    job = result.jobs[0]
    assert (job.title, job.locations, job.description_text) == ("Grid Analyst", ["Denver, CO"], "Model the grid.")
    assert job.published_at == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_auto_falls_back_to_page_text_when_no_jobs_identifiable():
    page = "<html><body><h1>Careers</h1><p>We are hiring a Climate Lead.</p><script>x()</script></body></html>"
    result = GenericPageCollector().fetch(auto_source(), FakeClient({PAGE: page}), CollectContext())
    assert result.jobs == [] and not result.complete
    assert result.page_text == ["Careers", "We are hiring a Climate Lead."]
    assert result.resolved["mode"] == "text:page"


@pytest.mark.parametrize("kind", ["not_found", "blocked", "robots_disallowed"])
def test_auto_reports_unreadable_pages_explicitly(kind):
    client = FakeClient({PAGE: FetchError(kind, "simulated")})
    with pytest.raises(FetchError) as exc:
        GenericPageCollector().fetch(auto_source(), client, CollectContext())
    assert exc.value.kind == kind


def test_url_only_config_entries(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("sources:\n  - url: https://jobs.careers.example.com/global/en/search?q=climate\n"
                    "  - url: https://acme.wd5.myworkdayjobs.com/en-US/External?q=data\n    name: Acme Corp\n")
    first, second = load_sources(path)
    assert (first.adapter, first.company, first.key) == ("auto", "Example", "example-climate")
    assert (second.adapter, second.company, second.options["search_text"]) == ("workday", "Acme Corp", "data")


# --- Workday ---------------------------------------------------------------
WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"


def wd_source(**opts):
    return SourceConfig(key="acme", company="Acme", adapter="workday", url="https://acme.wd5.myworkdayjobs.com/External",
                        options={"host": "acme.wd5.myworkdayjobs.com", "tenant": "acme", "site": "External", **opts})


def wd_page(n_total, start, count):
    return {"total": n_total, "jobPostings": [
        {"title": f"Analyst {i}", "externalPath": f"/job/US-Remote/Analyst-{i}_R{10000 + i}",
         "locationsText": "US Remote", "postedOn": "Posted 3 Days Ago", "bulletFields": [f"R{10000 + i}"]}
        for i in range(start, start + count)]}


def test_workday_url_detection_with_locale_search_and_facets():
    from urllib.parse import urlsplit
    det = WorkdayCollector.detect(urlsplit(
        "https://acme.wd5.myworkdayjobs.com/en-US/External?q=climate&locations=abc123&jobFamilyGroup=f1"))
    assert det.url == "https://acme.wd5.myworkdayjobs.com/External"
    assert det.options["search_text"] == "climate"
    assert det.options["facets"] == {"locations": ["abc123"], "jobFamilyGroup": ["f1"]}


def test_workday_paginates_and_reads_details():
    bodies = []

    def jobs_endpoint(body):
        bodies.append(body)
        return wd_page(25, body["offset"], 20 if body["offset"] == 0 else 5)

    routes = {WD + "/jobs": jobs_endpoint}
    for i in range(25):
        routes[f"{WD}/job/US-Remote/Analyst-{i}_R{10000 + i}"] = {"jobPostingInfo": {
            "id": "x", "title": f"Analyst {i}", "jobDescription": "<p>Decarbonize.</p>", "timeType": "Full time",
            "location": "US Remote", "startDate": "2026-10-10"}}
    result = WorkdayCollector().fetch(wd_source(search_text="climate"), FakeClient(routes),
                                      CollectContext(max_detail_fetches=3))
    assert len(result.jobs) == 25 and result.complete
    assert [b["offset"] for b in bodies] == [0, 20] and bodies[0]["searchText"] == "climate"
    detailed = [j for j in result.jobs if j.description_text]
    assert len(detailed) == 3 and detailed[0].published_at_source == "workday.startDate"
    assert result.jobs[0].external_id == "R10000"


def test_workday_page_cap_marks_incomplete():
    routes = {WD + "/jobs": lambda body: wd_page(500, body["offset"], 20)}
    result = WorkdayCollector().fetch(wd_source(max_pages=2), FakeClient(routes), CollectContext(max_detail_fetches=0))
    assert len(result.jobs) == 40 and not result.complete and result.total_reported == 500


def test_workday_relative_posted_dates():
    now = datetime(2026, 10, 21, 15, tzinfo=timezone.utc)
    assert _posted_on("Posted Today", now).date().isoformat() == "2026-10-21"
    assert _posted_on("Posted 3 Days Ago", now).date().isoformat() == "2026-10-18"
    assert _posted_on("Posted 30+ Days Ago", now) is None


# --- monitor integration -----------------------------------------------------
def _run(sessions, settings, clock, routes, configs):
    return run_monitor(sessions, configs, settings, client_factory=lambda: FakeClient(routes), now_fn=clock)


def test_page_text_changes_are_recorded_as_snapshots(sessions, settings, clock):
    routes = {PAGE: "<h1>Careers</h1><p>No openings right now.</p>"}
    cfg = [auto_source()]
    _run(sessions, settings, clock, routes, cfg)
    clock.advance(hours=1)
    routes[PAGE] = "<h1>Careers</h1><p>Now hiring: Climate Policy Lead.</p>"
    summary = _run(sessions, settings, clock, routes, cfg)
    assert summary.sources[0].page_changed
    with sessions() as s:
        snaps = list(s.scalars(select(PageSnapshot).order_by(PageSnapshot.id)))
    assert snaps[0].is_baseline and snaps[1].added == ["Now hiring: Climate Policy Lead."]
    assert snaps[1].removed == ["No openings right now."]
    clock.advance(hours=1)
    assert not _run(sessions, settings, clock, routes, cfg).sources[0].page_changed  # unchanged page


def test_reading_method_change_rebuilds_baseline_quietly(sessions, settings, clock):
    cfg = [auto_source()]
    routes = {PAGE: '<a href="/careers/job/1234567">Analyst</a>',
              "https://fixture.example.com/careers/job/1234567": "<html></html>"}
    _run(sessions, settings, clock, routes, cfg)
    clock.advance(hours=1)
    routes[PAGE] = '<a href="https://jobs.lever.co/fixture">All jobs</a>'
    routes["https://api.lever.co/v0/postings/fixture"] = [
        {"id": "a", "text": "Analyst", "hostedUrl": "https://jobs.lever.co/fixture/a", "categories": {}}]
    summary = _run(sessions, settings, clock, routes, cfg)
    assert summary.sources[0].baseline and summary.sources[0].new == 0
    with sessions() as s:
        assert s.scalars(select(Source)).one().state["mode"] == "board:lever"
        assert not list(s.scalars(select(JobChange).where(JobChange.change_type == "new")))


def test_description_diff_lists_added_and_removed_lines(sessions, settings, clock):
    from test_monitor import Harness, ashby_job
    h = Harness(sessions, settings, clock)
    h.run({"jobs": [ashby_job(1, descriptionPlain="Line A\nLine B\nLine C")]})
    clock.advance(hours=1)
    h.run({"jobs": [ashby_job(1, descriptionPlain="Line A\nLine B2\nLine C\nVisa sponsorship available")]})
    (change,) = h.changes("updated")
    d = change.changed_fields["description_text"]
    assert d["added"] == ["Line B2", "Visa sponsorship available"] and d["removed"] == ["Line B"]


def test_card_text_does_not_cause_false_updates(sessions, settings, clock):
    cfg = [auto_source()]
    page = '<li><a href="/careers/job/1234567">Analyst</a><span>Posted {} ago</span></li>'
    routes = {PAGE: page.format("2 hours"), "https://fixture.example.com/careers/job/1234567": "<html></html>"}
    _run(sessions, settings, clock, routes, cfg)
    clock.advance(hours=1)
    routes[PAGE] = page.format("3 hours")
    summary = _run(sessions, settings, clock, routes, cfg)
    assert summary.sources[0].updated == 0
    with sessions() as s:
        assert "3 hours" in s.scalars(select(Job)).one().extra["card_text"]  # kept fresh for the email
