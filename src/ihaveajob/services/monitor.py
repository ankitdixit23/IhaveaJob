"""Monitoring run: fetch every enabled source, persist jobs, detect changes.

Safety rules (each covered by tests):

* Baseline: the first successful run of a source records jobs with change
  type ``baseline`` (never ``new``), so adding a company never triggers a
  flood of "new job" alerts.
* Closures only happen after a *complete* fetch (collector says it saw every
  job), that also passes a sanity check (no sudden >50% drop, not empty when
  jobs existed). A job must be absent from ``close_after_misses`` consecutive
  complete fetches AND unseen for ``close_after_hours`` before it is closed.
* A failed fetch changes nothing about stored jobs.
* Fields a collector did not provide (None) never erase stored values.
* Each source is committed separately: one broken site can't roll back others.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..collectors import CollectContext, get_collector
from ..config import Settings, slugify
from ..database.models import Company, Job, JobChange, MonitorRun, Source, SourceRun
from ..filters import matches
from ..http import FetchError, HttpClient
from ..models import FetchResult, NormalizedJob, SourceConfig, SourceFilters
from ..normalization import TRACKED_FIELDS, canonical_url, content_hash, normalize_for_hash

log = logging.getLogger("ihaveajob.monitor")

_MAX_DIFF_CHARS = 300


@dataclass
class SourceOutcome:
    key: str
    status: str
    fetched: int = 0
    matched: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    reopened: int = 0
    baseline: bool = False
    complete: bool = False
    closure_check: str | None = None
    error: str | None = None
    consecutive_failures: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: int
    status: str
    sources: list[SourceOutcome]

    @property
    def persistent_failures(self) -> list[SourceOutcome]:
        return [s for s in self.sources if s.status == "failed" and s.consecutive_failures >= 3]

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "status": self.status, "sources": [s.__dict__ for s in self.sources]}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sync_sources(session: Session, configs: list[SourceConfig]) -> dict[str, Source]:
    """Create/update DB rows for configured sources. Removed sources are kept
    (history is preserved) but disabled."""
    rows = {s.key: s for s in session.scalars(select(Source))}
    companies = {c.slug: c for c in session.scalars(select(Company))}
    for cfg in configs:
        slug = slugify(cfg.company)
        company = companies.get(slug)
        if company is None:
            company = Company(slug=slug, name=cfg.company)
            session.add(company)
            companies[slug] = company
        row = rows.get(cfg.key)
        if row is None:
            row = Source(key=cfg.key, company=company, adapter=cfg.adapter, url=cfg.url,
                         filters=cfg.filters.to_dict(), enabled=cfg.enabled)
            session.add(row)
            rows[cfg.key] = row
        else:
            if row.adapter != cfg.adapter or row.url != cfg.url:
                # A different page/adapter means a different job universe:
                # rebuild the baseline so we don't announce everything as new.
                row.baseline_completed_at = None
                row.last_complete_count = None
            row.company = company
            row.adapter, row.url, row.enabled = cfg.adapter, cfg.url, cfg.enabled
            row.filters = cfg.filters.to_dict()
    configured = {c.key for c in configs}
    for key, row in rows.items():
        if key not in configured:
            row.enabled = False
    session.flush()
    return rows


def _job_fields(job: Job | NormalizedJob) -> dict[str, object]:
    return {f: getattr(job, f) for f in TRACKED_FIELDS}


def _short(value: object) -> object:
    if isinstance(value, str) and len(value) > _MAX_DIFF_CHARS:
        return value[:_MAX_DIFF_CHARS] + "…"
    return value


def _diff(old: dict[str, object], new: dict[str, object]) -> dict[str, dict[str, object]]:
    out = {}
    for name in TRACKED_FIELDS:
        if normalize_for_hash(old.get(name)) != normalize_for_hash(new.get(name)):
            if name == "description_text":
                out[name] = {"old_len": len(old.get(name) or ""), "new_len": len(new.get(name) or "")}
            else:
                out[name] = {"old": _short(old.get(name)), "new": _short(new.get(name))}
    return out


def _closure_check(source: Source, result: FetchResult, baseline: bool) -> tuple[bool, str]:
    fetched = len(result.jobs)
    if baseline:
        return False, "baseline run"
    if not result.complete:
        return False, "incomplete fetch (capped/paginated/partial)"
    prev = source.last_complete_count
    if prev:
        if fetched == 0:
            return False, f"empty response but {prev} jobs last time; treated as suspicious"
        if prev >= 5 and fetched < prev * 0.5:
            return False, f"job count dropped {prev}->{fetched} (>50%); treated as suspicious"
    return True, "ok"


def _merge_into(row: Job, nj: NormalizedJob) -> None:
    """Copy collector values onto the row; None never erases stored values."""
    for name in ("title", "url", "application_url", "department", "employment_type", "workplace_type",
                 "description_text", "published_at", "published_at_source", "published_at_precision",
                 "source_updated_at"):
        value = getattr(nj, name)
        if value is not None:
            setattr(row, name, value)
    if nj.locations:
        row.locations = list(nj.locations)
        row.location = nj.location
    extra = {k: v for k, v in nj.extra.items() if k != "details_fetched"}
    if extra:
        row.extra = {**(row.extra or {}), **extra}
    row.company_name = nj.company or row.company_name
    row.canonical_url = canonical_url(nj.url) or row.canonical_url


def _as_normalized(row: Job) -> NormalizedJob:
    return NormalizedJob(
        external_id=row.external_id, title=row.title, company=row.company_name, url=row.url or "",
        locations=list(row.locations or []), department=row.department, employment_type=row.employment_type,
        workplace_type=row.workplace_type, description_text=row.description_text,
    )


def process_result(
    session: Session, source: Source, run: MonitorRun, result: FetchResult, settings: Settings,
    filters: SourceFilters, now: datetime,
) -> SourceOutcome:
    baseline = source.baseline_completed_at is None
    outcome = SourceOutcome(key=source.key, status="success" if result.complete else "partial",
                            baseline=baseline, complete=result.complete, notes=list(result.notes))
    rows = list(session.scalars(select(Job).where(Job.source_id == source.id)))
    by_id = {r.external_id: r for r in rows}
    by_url = {r.canonical_url: r for r in rows if r.canonical_url}
    seen: set[int] = set()
    unique: dict[str, NormalizedJob] = {}
    for nj in result.jobs:
        unique.setdefault(nj.external_id, nj)
    outcome.fetched = len(unique)

    for nj in unique.values():
        row = by_id.get(nj.external_id)
        curl = canonical_url(nj.url)
        if row is None and curl and curl in by_url and by_url[curl].id not in seen:
            row = by_url[curl]  # same posting, source changed its id
            old_id, row.external_id = row.external_id, nj.external_id
            outcome.notes.append(f"external id changed {old_id}->{nj.external_id} (matched by URL)")
        if row is None:
            row = Job(
                source_id=source.id, company_id=source.company_id, external_id=nj.external_id,
                title=nj.title, company_name=nj.company, locations=[], extra={},
                first_detected_at=now, last_seen_at=now, status="open", is_baseline=baseline, content_hash="",
            )
            _merge_into(row, nj)
            row.content_hash = content_hash(_job_fields(row))
            row.matches_filters = matches(_as_normalized(row), filters)
            if nj.extra.get("details_fetched"):
                row.details_fetched_at = now
            session.add(row)
            session.flush()
            session.add(JobChange(job_id=row.id, run_id=run.id, change_type="baseline" if baseline else "new",
                                  changed_fields={}, detected_at=now))
            by_id[row.external_id] = row
            if curl:
                by_url[curl] = row
            if not baseline:
                outcome.new += 1
        else:
            before = _job_fields(row)
            _merge_into(row, nj)
            new_hash = content_hash(_job_fields(row))
            if new_hash != row.content_hash:
                diff = _diff(before, _job_fields(row))
                row.content_hash = new_hash
                if diff:
                    session.add(JobChange(job_id=row.id, run_id=run.id, change_type="updated",
                                          changed_fields=diff, detected_at=now))
                    outcome.updated += 1
            if row.status == "closed":
                session.add(JobChange(job_id=row.id, run_id=run.id, change_type="reopened",
                                      changed_fields={"closed_at": row.closed_at.isoformat() if row.closed_at else None},
                                      detected_at=now))
                outcome.reopened += 1
            row.status, row.missing_count, row.closed_at = "open", 0, None
            row.last_seen_at = now
            row.matches_filters = matches(_as_normalized(row), filters)
            if nj.extra.get("details_fetched"):
                row.details_fetched_at = now
        seen.add(row.id)
        if row.matches_filters:
            outcome.matched += 1

    ok, reason = _closure_check(source, result, baseline)
    outcome.closure_check = reason
    if ok:
        min_age = timedelta(hours=settings.close_after_hours)
        for row in by_id.values():
            if row.id in seen or row.status == "closed":
                continue
            row.missing_count += 1
            row.status = "missing"
            if row.missing_count >= settings.close_after_misses and now - row.last_seen_at >= min_age:
                row.status, row.closed_at = "closed", now
                session.add(JobChange(job_id=row.id, run_id=run.id, change_type="closed",
                                      changed_fields={"missing_count": row.missing_count,
                                                      "last_seen_at": row.last_seen_at.isoformat()},
                                      detected_at=now))
                outcome.closed += 1
        source.last_complete_count = outcome.fetched
    elif result.complete and baseline:
        source.last_complete_count = outcome.fetched

    if baseline:
        source.baseline_completed_at = now
    source.last_success_at = now
    source.consecutive_failures = 0
    return outcome


def run_monitor(
    sessions: sessionmaker[Session],
    configs: list[SourceConfig],
    settings: Settings,
    *,
    client_factory: Callable[[], HttpClient] = HttpClient,
    trigger: str = "manual",
    only: set[str] | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
) -> RunSummary:
    with sessions() as session:
        sync_sources(session, configs)
        run = MonitorRun(started_at=now_fn(), trigger=trigger, status="running", summary={})
        session.add(run)
        session.commit()
        run_id = run.id

    cfg_by_key = {c.key: c for c in configs}
    outcomes: list[SourceOutcome] = []
    for cfg in configs:
        if not cfg.enabled or (only and cfg.key not in only):
            continue
        outcomes.append(_run_source(sessions, run_id, cfg_by_key[cfg.key], settings, client_factory, now_fn))

    failed = sum(o.status == "failed" for o in outcomes)
    status = "success" if not failed else ("failed" if failed == len(outcomes) else "partial")
    summary = RunSummary(run_id=run_id, status=status, sources=outcomes)
    with sessions() as session:
        run = session.get(MonitorRun, run_id)
        run.status, run.finished_at, run.summary = status, now_fn(), summary.to_dict()
        session.commit()
    log.info("monitor_run_finished", extra={"run_id": run_id, "status": status,
                                            "sources": [o.__dict__ for o in outcomes]})
    return summary


def _run_source(
    sessions: sessionmaker[Session], run_id: int, cfg: SourceConfig, settings: Settings,
    client_factory: Callable[[], HttpClient], now_fn: Callable[[], datetime],
) -> SourceOutcome:
    client = client_factory()
    collector = get_collector(cfg.adapter)
    started = time.monotonic()
    with sessions() as session:
        source = session.scalars(select(Source).where(Source.key == cfg.key)).one()
        refresh_before = now_fn() - timedelta(hours=settings.detail_refresh_hours)
        detail_state = {
            ext: (fetched_at, has_desc)
            for ext, fetched_at, has_desc in session.execute(
                select(Job.external_id, Job.details_fetched_at, Job.description_text.is_not(None))
                .where(Job.source_id == source.id))
        }

        def needs_details(ext_id: str) -> bool:
            state = detail_state.get(ext_id)
            return state is None or not state[1] or state[0] is None or state[0] < refresh_before

        ctx = CollectContext(needs_details=needs_details, max_detail_fetches=settings.max_detail_fetches)
        run = session.get(MonitorRun, run_id)
        source_run = SourceRun(run_id=run_id, source_id=source.id, started_at=now_fn(), status="failed")
        try:
            result = collector.fetch(cfg, client, ctx)
            outcome = process_result(session, source, run, result, settings, cfg.filters, now_fn())
        except FetchError as exc:
            session.rollback()
            source = session.scalars(select(Source).where(Source.key == cfg.key)).one()
            source.consecutive_failures += 1
            outcome = SourceOutcome(key=cfg.key, status="failed", error=f"{exc.kind}: {exc}",
                                    consecutive_failures=source.consecutive_failures)
            source_run = SourceRun(run_id=run_id, source_id=source.id, started_at=now_fn(), status="failed",
                                   error_kind=exc.kind, error_message=str(exc)[:2000])
            log.error("source_failed", extra={"source": cfg.key, "kind": exc.kind, "error": str(exc),
                                              "consecutive_failures": source.consecutive_failures})
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            session.rollback()
            source = session.scalars(select(Source).where(Source.key == cfg.key)).one()
            source.consecutive_failures += 1
            outcome = SourceOutcome(key=cfg.key, status="failed", error=f"unexpected: {exc!r}",
                                    consecutive_failures=source.consecutive_failures)
            source_run = SourceRun(run_id=run_id, source_id=source.id, started_at=now_fn(), status="failed",
                                   error_kind="unexpected", error_message=repr(exc)[:2000])
            log.exception("source_crashed", extra={"source": cfg.key})
        else:
            log.info("source_ok", extra={"source": cfg.key, **{k: v for k, v in outcome.__dict__.items() if k != "notes"},
                                         "notes": outcome.notes})
        source_run.duration_ms = int((time.monotonic() - started) * 1000)
        source_run.http_requests = client.request_count
        for attr in ("fetched", "matched", "new", "updated", "closed", "reopened"):
            setattr(source_run, f"{attr}_count", getattr(outcome, attr))
        if outcome.status != "failed":
            source_run.status = outcome.status
            source_run.complete = outcome.complete
            source_run.closure_check = outcome.closure_check
        source_run.notes = outcome.notes[:50]
        session.add(source_run)
        session.commit()
    return outcome
