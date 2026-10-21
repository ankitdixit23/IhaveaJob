from datetime import datetime, timezone

import pytest

from conftest import FakeClient, fixture_text
from ihaveajob.collectors import CollectContext, UnsupportedUrl, detect_source
from ihaveajob.collectors.ats import AshbyCollector, GreenhouseCollector, LeverCollector
from ihaveajob.collectors.google_careers import GoogleCareersCollector
from ihaveajob.collectors.watershed import WatershedCollector
from ihaveajob.http import FetchError
from ihaveajob.models import SourceConfig, SourceFilters

WS_URL = "https://watershed.com/careers"


def ws_source(**filters):
    return SourceConfig(key="watershed", company="Watershed", adapter="watershed", url=WS_URL,
                        filters=SourceFilters(**filters))


# --- Watershed -------------------------------------------------------------
def test_watershed_listing_parses_jobs_departments_locations():
    jobs = WatershedCollector().parse_listing(fixture_text("watershed_listing.html"), WS_URL, "Watershed")
    assert [j.external_id for j in jobs] == ["aaaa0001", "aaaa0002", "aaaa0003"]
    first = jobs[0]
    assert first.title == "Fixture data engineer"
    assert first.department == "Engineering"
    assert first.locations == ["San Francisco", "New York City"]
    assert first.url == "https://watershed.com/careers/aaaa0001-fixture-data-engineer"
    assert jobs[2].department == "Sustainability Advisory and Services"


def test_watershed_detail_uses_jsonld_date_posted_not_valid_through():
    collector = WatershedCollector()
    job = collector.parse_listing(fixture_text("watershed_listing.html"), WS_URL, "Watershed")[0]
    collector.apply_detail(job, collector.parse_detail(fixture_text("watershed_job.html")))
    assert job.published_at == datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert (job.published_at_source, job.published_at_precision) == ("jsonld.datePosted", "date")
    assert job.employment_type == "Full-time"
    assert "climate data pipelines" in job.description_text
    assert "alert(" not in job.description_text  # scripts stripped from untrusted HTML


def test_watershed_fetch_respects_detail_cap_and_structural_filters():
    routes = {WS_URL: fixture_text("watershed_listing.html")}
    for ext, slug in [("aaaa0001", "fixture-data-engineer"), ("aaaa0002", "fixture-ml-engineer"),
                      ("aaaa0003", "fixture-sustainability-advisor")]:
        routes[f"{WS_URL}/{ext}-{slug}"] = fixture_text("watershed_job.html")
    client = FakeClient(routes)
    result = WatershedCollector().fetch(ws_source(locations=["London", "Remote"]), client,
                                        CollectContext(max_detail_fetches=1))
    assert result.complete and len(result.jobs) == 3  # all listed jobs returned (presence tracking)
    detail_requests = [u for u in client.requested if u != WS_URL]
    assert detail_requests == [f"{WS_URL}/aaaa0002-fixture-ml-engineer"]  # SF job filtered, cap=1
    assert any("deferred" in n for n in result.notes)


def test_watershed_empty_listing_is_a_parse_error_not_an_empty_complete_result():
    client = FakeClient({WS_URL: "<html><body>Redesigned page</body></html>"})
    with pytest.raises(FetchError) as exc:
        WatershedCollector().fetch(ws_source(), client, CollectContext())
    assert exc.value.kind == "parse_error"


# --- Google ----------------------------------------------------------------
def test_google_parses_embedded_records_including_short_ones():
    result = GoogleCareersCollector().parse(fixture_text("google_results.html"), "Google")
    assert result.complete and result.total_reported == 2
    first, second = result.jobs
    assert first.external_id == "111111111111111111"
    assert first.locations == ["New York, NY, USA", "Seattle, WA, USA"]
    assert first.published_at == datetime.fromtimestamp(1790000000, timezone.utc)
    assert first.source_updated_at == datetime.fromtimestamp(1790500000, timezone.utc)
    assert first.published_at_source == "google.created_ts.inferred"
    assert "Responsibilities" in first.description_text and "Bachelor" in first.description_text
    assert first.url.endswith("/111111111111111111-fixture-sustainability-program-manager")
    assert first.application_url == first.url
    assert second.title == "Fixture Climate Data Analyst"  # 20-field record parsed


def test_google_capped_results_are_incomplete():
    html = fixture_text("google_results.html").replace("]],null,2,20]", "]],null,57,20]")
    result = GoogleCareersCollector().parse(html, "Google")
    assert not result.complete and result.total_reported == 57


@pytest.mark.parametrize("html", ["<html>no data</html>",
                                  "AF_initDataCallback({key: 'ds:1', hash: '2', data:[[[1,2]]], sideChannel: {}});</script>",
                                  "AF_initDataCallback({key: 'ds:1', hash: '2', data:{broken, sideChannel: {}});</script>"])
def test_google_malformed_pages_raise_parse_error(html):
    with pytest.raises(FetchError) as exc:
        GoogleCareersCollector().parse(html, "Google")
    assert exc.value.kind == "parse_error"


# --- ATS APIs (synthetic payloads) -------------------------------------------
def test_ashby_parse_skips_unlisted_and_reads_published_at():
    payload = {"jobs": [
        {"id": "a1", "title": "Climate Analyst", "location": "Remote", "secondaryLocations": [{"location": "NYC"}],
         "department": "Data", "employmentType": "FullTime", "isListed": True, "isRemote": True,
         "publishedAt": "2026-09-01T10:00:00.000+00:00", "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
         "descriptionPlain": "Analyze emissions."},
        {"id": "a2", "title": "Hidden", "isListed": False},
    ]}
    result = AshbyCollector().parse(payload, "Acme")
    assert [j.external_id for j in result.jobs] == ["a1"]
    job = result.jobs[0]
    assert job.locations == ["Remote", "NYC"] and job.workplace_type == "Remote"
    assert job.published_at == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def test_greenhouse_parse_unescapes_content_and_checks_meta_total():
    payload = {"jobs": [{"id": 7, "title": "Policy Researcher", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                         "location": {"name": "DC"}, "first_published": "2026-08-01T09:00:00-04:00",
                         "updated_at": "2026-09-01T09:00:00-04:00", "content": "&lt;p&gt;Research &amp;amp; policy&lt;/p&gt;",
                         "departments": [{"name": "Policy"}]}], "meta": {"total": 1}}
    result = GreenhouseCollector().parse(payload, "Acme")
    job = result.jobs[0]
    assert result.complete and job.description_text == "Research & policy"
    assert job.published_at == datetime(2026, 8, 1, 13, tzinfo=timezone.utc)
    assert GreenhouseCollector().parse({**payload, "meta": {"total": 5}}, "Acme").complete is False


def test_lever_parse_epoch_millis_and_lists():
    payload = [{"id": "l1", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/l1",
                "applyUrl": "https://jobs.lever.co/acme/l1/apply", "createdAt": 1756720800000,
                "categories": {"team": "Data", "commitment": "Full-time", "allLocations": ["Remote", "Berlin"]},
                "descriptionPlain": "Pipelines.", "lists": [{"text": "You have", "content": "<li>SQL</li>"}]}]
    job = LeverCollector().parse(payload, "Acme").jobs[0]
    assert job.published_at == datetime.fromtimestamp(1756720800, timezone.utc)
    assert "SQL" in job.description_text and job.locations == ["Remote", "Berlin"]


@pytest.mark.parametrize("collector,payload", [(AshbyCollector(), {"nope": []}), (GreenhouseCollector(), []),
                                                (LeverCollector(), {"jobs": []})])
def test_ats_malformed_payloads_raise(collector, payload):
    with pytest.raises(FetchError):
        collector.parse(payload, "Acme")


# --- URL detection -----------------------------------------------------------
@pytest.mark.parametrize("url,adapter,expected_url", [
    ("watershed.com/careers", "watershed", "https://watershed.com/careers"),
    ("https://watershed.com/en-GB/careers/", "watershed", "https://watershed.com/careers"),
    ("https://www.google.com/about/careers/applications/jobs/results?q=climate&location=New+York&page=4",
     "google_careers", "https://www.google.com/about/careers/applications/jobs/results?q=climate&location=New+York"),
    ("https://jobs.ashbyhq.com/acme/123", "ashby", "https://jobs.ashbyhq.com/acme"),
    ("https://job-boards.greenhouse.io/acme", "greenhouse", "https://boards.greenhouse.io/acme"),
    ("https://jobs.lever.co/acme?team=Data", "lever", "https://jobs.lever.co/acme"),
])
def test_detect_source(url, adapter, expected_url):
    det = detect_source(url)
    assert (det.adapter, det.url) == (adapter, expected_url)


def test_detect_lever_url_filters_become_source_filters():
    det = detect_source("https://jobs.lever.co/acme?team=Data&location=Remote")
    assert det.filters.departments == ["Data"] and det.filters.locations == ["Remote"]


def test_detect_unknown_site_is_explained():
    with pytest.raises(UnsupportedUrl, match="No collector supports"):
        detect_source("https://careers.example.com/jobs")
