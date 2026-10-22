"""Read-only summaries of the job database for the web app."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models import Job, JobChange, Source, SourceRun

_MODE_LABELS = {
    "board": "job-board feed", "jsonld": "structured job data", "links": "job links on the page",
    "text": "page text (no individual jobs found)",
}


def describe_mode(state: dict | None, adapter: str) -> str:
    mode = (state or {}).get("mode")
    if not mode:
        return {"workday": "Workday feed", "ashby": "Ashby feed", "greenhouse": "Greenhouse feed",
                "lever": "Lever feed", "google_careers": "Google Careers data",
                "watershed": "Watershed careers page"}.get(adapter, "not checked yet")
    family, _, how = mode.partition(":")
    label = _MODE_LABELS.get(family, family)
    if family == "board":
        label = f"{how.title()} feed (found on the page)"
    elif how and family != "text":
        label += f" ({how})"
    return label


def source_status(session: Session) -> dict[str, dict]:
    """Per source key: counts, last check, health and how it is read."""
    out: dict[str, dict] = {}
    for src in session.scalars(select(Source)):
        counts = dict(session.execute(select(Job.status, func.count()).where(Job.source_id == src.id)
                                      .group_by(Job.status)).all())
        matching = session.scalar(select(func.count()).where(
            Job.source_id == src.id, Job.status != "closed", Job.matches_filters.is_(True))) or 0
        last = session.scalars(select(SourceRun).where(SourceRun.source_id == src.id)
                               .order_by(SourceRun.id.desc()).limit(1)).first()
        out[src.key] = {
            "open": counts.get("open", 0) + counts.get("missing", 0),
            "matching": matching,
            "last_checked": last.started_at if last else None,
            "last_status": last.status if last else None,
            "error": f"{last.error_kind}: {last.error_message}" if last and last.error_kind else None,
            "failures_in_a_row": src.consecutive_failures,
            "read_via": describe_mode(src.state, src.adapter),
            "baseline_done": src.baseline_completed_at is not None,
        }
    return out


def recent_changes(session: Session, hours: float = 48, limit: int = 200) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.execute(
        select(JobChange, Job, Source).join(Job, JobChange.job_id == Job.id).join(Source, Job.source_id == Source.id)
        .where(JobChange.detected_at >= since, JobChange.change_type.in_(("new", "updated", "closed", "reopened")),
               Job.matches_filters.is_(True))
        .order_by(JobChange.detected_at.desc()).limit(limit)).all()
    labels = {"new": "New", "updated": "Updated", "closed": "Removed", "reopened": "Reopened"}
    return [{"when": c.detected_at, "change": labels[c.change_type], "company": s.company.name, "title": j.title,
             "location": j.location or (j.extra or {}).get("card_text") or "", "link": j.application_url or j.url,
             "what_changed": ", ".join(c.changed_fields or {}) if c.change_type == "updated" else ""}
            for c, j, s in rows]
