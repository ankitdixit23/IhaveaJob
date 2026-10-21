"""Send each recipient one digest of everything that changed since they were
last told. Idempotent: delivered items are recorded per recipient and never
sent twice; a failed send records nothing, so the next run retries.

The first time a recipient is seen, a "monitoring started" email is sent and
a start marker recorded; history from before that point is not emailed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..database.models import (
    Job, JobChange, Notification, NotificationItem, PageSnapshot, Source, SourceRun,
)
from ..notifications.digest import build_digest, fmt_time
from ..notifications.email import NotificationError, Notifier

log = logging.getLogger("ihaveajob.notify")

ALERT_TYPES = ("new", "updated", "closed", "reopened")
HEALTH_THRESHOLD = 3


def recipient_key(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:32]


@dataclass
class NotifyOutcome:
    recipient: str
    status: str  # started | sent | nothing | failed | dry_run
    items: int = 0
    subject: str | None = None
    error: str | None = None


def _start_marker(session: Session, key: str) -> Notification | None:
    return session.scalars(select(Notification).where(Notification.recipient_key == key,
                                                      Notification.status == "started")
                           .order_by(Notification.id).limit(1)).first()


def _delivered(session: Session, key: str, item_type: str) -> set[int]:
    return set(session.scalars(select(NotificationItem.item_id).where(
        NotificationItem.recipient_key == key, NotificationItem.item_type == item_type)))


def pending(session: Session, key: str, since: datetime):
    sent_changes = _delivered(session, key, "job_change")
    changes = [
        (c, j, s) for c, j, s in session.execute(
            select(JobChange, Job, Source).join(Job, JobChange.job_id == Job.id).join(Source, Job.source_id == Source.id)
            .where(JobChange.change_type.in_(ALERT_TYPES), JobChange.detected_at > since,
                   Job.matches_filters.is_(True))
            .order_by(Source.key, JobChange.change_type, JobChange.id))
        if c.id not in sent_changes
    ]
    sent_snaps = _delivered(session, key, "page_snapshot")
    snapshots = [
        (p, s) for p, s in session.execute(
            select(PageSnapshot, Source).join(Source, PageSnapshot.source_id == Source.id)
            .where(PageSnapshot.is_baseline.is_(False), PageSnapshot.captured_at > since)
            .order_by(PageSnapshot.id))
        if p.id not in sent_snaps and (p.added or p.removed)
    ]
    sent_health = _delivered(session, key, "health")
    health = []
    for src in session.scalars(select(Source).where(Source.enabled.is_(True),
                                                    Source.consecutive_failures >= HEALTH_THRESHOLD)):
        last = session.scalars(select(SourceRun).where(SourceRun.source_id == src.id)
                               .order_by(SourceRun.id.desc()).limit(1)).first()
        # One alert per failure streak: keyed by the run where the streak hit the threshold.
        streak_runs = list(session.scalars(select(SourceRun.id).where(SourceRun.source_id == src.id)
                                           .order_by(SourceRun.id.desc()).limit(src.consecutive_failures)))
        anchor = streak_runs[-1] if streak_runs else None
        if last and anchor and anchor not in sent_health and last.started_at > since:
            msg = (f"failed {src.consecutive_failures} runs in a row — {last.error_kind}: "
                   f"{(last.error_message or '')[:200]}")
            health.append((anchor, src, msg))
    return changes, snapshots, health


def _welcome(session: Session, now: datetime) -> tuple[str, str]:
    lines = ["IhaveaJob is now monitoring these careers pages for you. From now on you'll get one email per run",
             "(hourly) whenever something changes: new jobs, edits (with what changed), and jobs removed from the page.",
             ""]
    for src in session.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.key)):
        open_jobs = session.scalar(select(func.count()).where(Job.source_id == src.id, Job.status != "closed",
                                                              Job.matches_filters.is_(True)))
        mode = (src.state or {}).get("mode", "not read yet")
        lines.append(f"• {src.company.name}: {open_jobs} matching jobs tracked — {src.url} (read via {mode})")
    lines += ["", f"Started {fmt_time(now)}. Existing jobs were recorded as a baseline and won't be re-sent."]
    return "IhaveaJob: monitoring started", "\n".join(lines)


def send_digests(
    sessions: sessionmaker[Session],
    notifier: Notifier | None,
    recipients: list[str],
    *,
    dry_run: bool = False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    printer: Callable[[str], None] | None = None,
) -> list[NotifyOutcome]:
    outcomes = []
    for address in recipients:
        key = recipient_key(address)
        now = now_fn()
        with sessions() as session:
            marker = _start_marker(session, key)
            if marker is None:
                subject, text = _welcome(session, now)
                if dry_run:
                    (printer or print)(f"Subject: {subject}\n\n{text}")
                    outcomes.append(NotifyOutcome(address, "dry_run", 0, subject))
                    continue
                try:
                    if notifier is not None:
                        notifier.send(address, subject, text)
                except NotificationError as exc:
                    session.add(Notification(channel="email", recipient_key=key, created_at=now, status="failed",
                                             subject=subject, error=str(exc)[:2000]))
                    session.commit()
                    outcomes.append(NotifyOutcome(address, "failed", 0, subject, str(exc)))
                    log.error("notify_failed", extra={"error": str(exc)})
                    continue
                session.add(Notification(channel="email", recipient_key=key, created_at=now, status="started",
                                         subject=subject))
                session.commit()
                outcomes.append(NotifyOutcome(address, "started", 0, subject))
                continue

            changes, snapshots, health = pending(session, key, marker.created_at)
            digest = build_digest(changes, snapshots, health, now)
            if digest is None:
                outcomes.append(NotifyOutcome(address, "nothing"))
                continue
            count = len(digest.change_ids) + len(digest.snapshot_ids) + len(digest.health_ids)
            if dry_run:
                (printer or print)(f"Subject: {digest.subject}\n\n{digest.text}")
                outcomes.append(NotifyOutcome(address, "dry_run", count, digest.subject))
                continue
            try:
                if notifier is None:
                    raise NotificationError("Email is not configured (set SMTP_HOST and ALERT_EMAIL_TO)")
                notifier.send(address, digest.subject, digest.text, digest.html)
            except NotificationError as exc:
                session.add(Notification(channel="email", recipient_key=key, created_at=now, status="failed",
                                         subject=digest.subject, item_count=count, error=str(exc)[:2000]))
                session.commit()
                outcomes.append(NotifyOutcome(address, "failed", count, digest.subject, str(exc)))
                log.error("notify_failed", extra={"error": str(exc), "items": count})
                continue
            note = Notification(channel="email", recipient_key=key, created_at=now, status="sent",
                                subject=digest.subject, item_count=count)
            session.add(note)
            session.flush()
            for item_type, ids in (("job_change", digest.change_ids), ("page_snapshot", digest.snapshot_ids),
                                   ("health", digest.health_ids)):
                session.add_all(NotificationItem(notification_id=note.id, recipient_key=key, item_type=item_type,
                                                 item_id=i) for i in ids)
            session.commit()
            outcomes.append(NotifyOutcome(address, "sent", count, digest.subject))
            log.info("notify_sent", extra={"items": count, "subject": digest.subject})
    return outcomes
