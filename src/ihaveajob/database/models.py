"""SQLAlchemy ORM models.

Phase 1 holds only PUBLIC job data (companies, sources, jobs, change history,
run logs). Private per-user tables arrive in later migrations and must never
be written to a database stored in a public location.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC (portable across SQLite/Postgres), returns aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    sources: Mapped[list["Source"]] = relationship(back_populates="company")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    baseline_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_complete_count: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How the source was last read (e.g. {"mode": "links:headless browser"}) and recent error info.
    state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    company: Mapped[Company] = relationship(back_populates="sources")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_canonical_url", "canonical_url"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_first_detected_at", "first_detected_at"),
        Index("ix_jobs_published_at", "published_at"),
        Index("ix_jobs_company_id", "company_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    application_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(80))
    workplace_type: Mapped[str | None] = mapped_column(String(80))
    description_text: Mapped[str | None] = mapped_column(Text)
    # Publication time as reported by the employer/source, kept separate from
    # first_detected_at (when *we* first saw it).
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    published_at_source: Mapped[str | None] = mapped_column(String(80))
    published_at_precision: Mapped[str | None] = mapped_column(String(16))
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    details_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # open | missing (absent from a complete fetch, not yet confirmed) | closed
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # True if found while creating the source's baseline: never alert on these as "new".
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Whether the job currently passes the source's filters.
    matches_filters: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    changes: Mapped[list["JobChange"]] = relationship(back_populates="job", order_by="JobChange.id")


class MonitorRun(Base):
    __tablename__ = "monitor_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    trigger: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class SourceRun(Base):
    __tablename__ = "source_runs"
    __table_args__ = (Index("ix_source_runs_source_started", "source_id", "started_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("monitor_runs.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # success | partial | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closure_check: Mapped[str | None] = mapped_column(String(200))
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class JobChange(Base):
    __tablename__ = "job_changes"
    __table_args__ = (
        Index("ix_job_changes_job_id", "job_id"),
        Index("ix_job_changes_detected_type", "detected_at", "change_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("monitor_runs.id"))
    # baseline | new | updated | missing | closed | reopened
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    changed_fields: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    job: Mapped[Job] = relationship(back_populates="changes")


class PageSnapshot(Base):
    """Visible-text snapshot for pages where individual jobs can't be identified."""

    __tablename__ = "page_snapshots"
    __table_args__ = (Index("ix_page_snapshots_source_captured", "source_id", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("monitor_runs.id"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lines: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    added: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    removed: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Notification(Base):
    """One delivery attempt (e.g. an hourly digest email).

    ``recipient_key`` is a hash of the address, so the (possibly public) job
    database never contains email addresses.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_recipient_created", "recipient_key", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    # started (subscription marker) | sent | failed | dry_run
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class NotificationItem(Base):
    """What a recipient has already been told about: prevents duplicates."""

    __tablename__ = "notification_items"
    __table_args__ = (UniqueConstraint("recipient_key", "item_type", "item_id", name="uq_notification_items"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"), nullable=False)
    recipient_key: Mapped[str] = mapped_column(String(64), nullable=False)
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)  # job_change | page_snapshot
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
