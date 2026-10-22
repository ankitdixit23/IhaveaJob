"""Command-line interface: ``ihaveajob <command>``.

Exit codes for ``run``: 0 = ok (including transient source failures),
2 = at least one source has failed 3+ runs in a row (needs attention),
1 = configuration/database error. ``notify``: 3 = an email failed to send.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from .collectors import CollectContext, UnsupportedUrl, detect_source, get_collector
from .config import DEFAULT_CONFIG_PATH, ConfigError, Settings, append_source, default_key, load_sources
from .database import make_engine, session_factory, upgrade
from .database.models import Job, JobChange, Source, SourceRun
from .filters import matches
from .http import FetchError, HttpClient
from .logging_setup import configure_logging
from .models import SourceConfig, SourceFilters
from .notifications.email import EmailSettings, NotificationError, SmtpEmailNotifier
from .services.monitor import run_monitor
from .services.notify import send_digests


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _filters_from_args(args: argparse.Namespace, base: SourceFilters | None = None) -> SourceFilters:
    f = base or SourceFilters()
    f.keywords_any += _csv(args.keywords)
    f.keywords_exclude += _csv(args.exclude)
    f.departments += _csv(args.departments)
    f.locations += _csv(args.locations)
    return f


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--keywords", help="comma-separated; job must mention at least one")
    p.add_argument("--exclude", help="comma-separated; drop jobs whose title/department mention any")
    p.add_argument("--departments", help="comma-separated department names (substring match)")
    p.add_argument("--locations", help="comma-separated locations, e.g. 'New York,Remote'")


def _sessions(settings: Settings):
    engine = make_engine(settings.database_url)
    upgrade(engine)
    return session_factory(engine)


def _source_from_url(args: argparse.Namespace) -> tuple[SourceConfig, list[str]]:
    det = detect_source(args.url)
    company = args.company or det.company_hint or "Company"
    filters = _filters_from_args(args, det.filters)
    key = args.key or default_key(company, args.url)
    return SourceConfig(key=key, company=company, adapter=det.adapter, url=det.url, filters=filters,
                        options=det.options), det.notes


def cmd_check_source(args: argparse.Namespace) -> int:
    source, notes = _source_from_url(args)
    print(f"Detected: adapter={source.adapter} company={source.company} url={source.url}")
    for n in notes:
        print(f"  note: {n}")
    client = HttpClient()
    try:
        result = get_collector(source.adapter).fetch(source, client, CollectContext(max_detail_fetches=args.max_details))
    except FetchError as exc:
        hint = {"not_found": "The page doesn't exist (404). Check the link in a browser.",
                "blocked": "The site refused automated access (bot protection or login). It can't be monitored.",
                "robots_disallowed": "The site's robots.txt forbids automated reading of this page.",
                }.get(exc.kind, "")
        print(f"FETCH FAILED ({exc.kind}): {exc}\n{hint}".rstrip(), file=sys.stderr)
        return 1
    hits = [j for j in result.jobs if matches(j, source.filters)]
    if result.resolved:
        print(f"Read via: {result.resolved.get('mode')}" + (f" ({result.resolved['url']})" if result.resolved.get('url') else ""))
    print(f"Fetched {len(result.jobs)} jobs (source reports {result.total_reported}); complete={result.complete}; "
          f"{len(hits)} match your filters; {client.request_count} HTTP requests")
    for n in result.notes:
        print(f"  note: {n}")
    if result.page_text is not None:
        print(f"Watching page text ({len(result.page_text)} lines). First lines:")
        for line in result.page_text[:15]:
            print(f"  | {line[:120]}")
    for j in hits[: args.limit]:
        pub = j.published_at.date().isoformat() if j.published_at else "unknown"
        print(f"- {j.title} | {j.location or 'location n/a'} | {j.department or '-'} | published {pub} | {j.url}")
    return 0


def cmd_add_source(args: argparse.Namespace) -> int:
    source, notes = _source_from_url(args)
    for n in notes:
        print(f"note: {n}")
    if args.dry_run:
        print(json.dumps({**source.__dict__, "filters": source.filters.to_dict()}, indent=2))
        return 0
    append_source(source, Path(args.config))
    print(f"Added source '{source.key}' ({source.adapter}) to {args.config}. "
          "Its first run builds a baseline: existing jobs are recorded but not announced as new.")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    for s in load_sources(Path(args.config)):
        flt = s.filters.to_dict() or "none"
        print(f"{s.key:28} {'on ' if s.enabled else 'off'} {s.adapter:15} {s.company:15} filters={flt}\n    {s.url}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    configs = load_sources(Path(args.config))
    summary = run_monitor(_sessions(settings), configs, settings, trigger=args.trigger,
                          only=set(args.source) if args.source else None)
    for s in summary.sources:
        line = (f"{s.key}: {s.status} fetched={s.fetched} matched={s.matched} new={s.new} updated={s.updated} "
                f"closed={s.closed} reopened={s.reopened}{' (baseline)' if s.baseline else ''} "
                f"closure_check={s.closure_check}")
        if s.error:
            line += f" error={s.error} consecutive_failures={s.consecutive_failures}"
        print(line)
    if summary.persistent_failures:
        print("ATTENTION: sources failing 3+ runs in a row: "
              + ", ".join(s.key for s in summary.persistent_failures), file=sys.stderr)
        return 2
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    sessions = _sessions(settings)
    with sessions() as session:
        for src in session.scalars(select(Source).order_by(Source.key)):
            counts = dict(session.execute(select(Job.status, func.count()).where(Job.source_id == src.id)
                                          .group_by(Job.status)).all())
            matched = session.scalar(select(func.count()).where(Job.source_id == src.id, Job.status != "closed",
                                                                Job.matches_filters.is_(True)))
            last = session.scalars(select(SourceRun).where(SourceRun.source_id == src.id)
                                   .order_by(SourceRun.id.desc()).limit(1)).first()
            print(f"{src.key}: enabled={src.enabled} open={counts.get('open', 0)} missing={counts.get('missing', 0)} "
                  f"closed={counts.get('closed', 0)} matching_filters={matched} "
                  f"baseline={'done' if src.baseline_completed_at else 'pending'} "
                  f"failures_in_a_row={src.consecutive_failures}")
            if last:
                print(f"    last run {last.started_at:%Y-%m-%d %H:%M} UTC: {last.status} fetched={last.fetched_count} "
                      f"new={last.new_count} closure_check={last.closure_check}"
                      + (f" error={last.error_kind}: {last.error_message}" if last.error_kind else ""))
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    types = _csv(args.types) or ["new", "updated", "closed", "reopened"]
    with _sessions(settings)() as session:
        q = (select(JobChange, Job).join(Job).where(JobChange.detected_at >= since, JobChange.change_type.in_(types))
             .order_by(JobChange.detected_at.desc()))
        if not args.all:
            q = q.where(Job.matches_filters.is_(True))
        rows = session.execute(q.limit(args.limit)).all()
        if not rows:
            print(f"No {', '.join(types)} changes in the last {args.hours}h.")
        for change, job in rows:
            pub = job.published_at.date().isoformat() if job.published_at else "unknown"
            print(f"[{change.change_type}] {change.detected_at:%Y-%m-%d %H:%M} {job.company_name}: {job.title} "
                  f"| {job.location or '-'} | published {pub} | {job.url}")
            if change.change_type == "updated":
                print(f"    changed: {', '.join(change.changed_fields)}")
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    try:
        email = EmailSettings.from_env()
    except NotificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if email is None and not args.dry_run:
        print("Email not configured (set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and ALERT_EMAIL_TO); skipping.")
        return 0
    recipients = email.recipients if email else (args.to or ["preview@example.invalid"])
    notifier = SmtpEmailNotifier(email) if email else None
    outcomes = send_digests(_sessions(Settings.from_env()), notifier, recipients, dry_run=args.dry_run)
    failed = False
    for o in outcomes:
        who = "recipient" if not args.dry_run else o.recipient
        print(f"{who}: {o.status} items={o.items}" + (f" subject={o.subject!r}" if o.subject else "")
              + (f" error={o.error}" if o.error else ""))
        failed |= o.status == "failed"
    return 3 if failed else 0


def cmd_sample_email(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .services.notify import build_sample

    with _sessions(Settings.from_env())() as session:
        digest = build_sample(session, datetime.now(timezone.utc), limit=args.limit)
    if digest is None:
        print("No tracked jobs yet: run the monitor first.")
        return 1
    if args.dry_run:
        print(f"Subject: {digest.subject}\n\n{digest.text}")
        return 0
    try:
        email = EmailSettings.from_env()
        if email is None:
            print("error: email is not configured (SMTP_HOST, ALERT_EMAIL_TO, ...)", file=sys.stderr)
            return 1
        for to in email.recipients:
            SmtpEmailNotifier(email).send(to, digest.subject, digest.text, digest.html)
    except NotificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Sample email sent to {len(email.recipients)} recipient(s).")
    return 0


def cmd_test_email(args: argparse.Namespace) -> int:
    try:
        email = EmailSettings.from_env()
        if email is None:
            print("error: set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and ALERT_EMAIL_TO first", file=sys.stderr)
            return 1
        notifier = SmtpEmailNotifier(email)
        for to in email.recipients:
            notifier.send(to, "IhaveaJob test email",
                          "If you can read this, IhaveaJob can send you job alerts.\n\n"
                          f"SMTP server: {email.host}:{email.port}")
    except NotificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Test email sent to {len(email.recipients)} recipient(s).")
    return 0


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    upgrade(make_engine(Settings.from_env().database_url))
    print("Database is up to date.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ihaveajob", description="Job intelligence monitor")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to companies.yaml")
    p.add_argument("--log-format", choices=["json", "text"], default=None)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("check-source", help="fetch a careers URL live and preview matches (no database writes)")
    s.add_argument("url"); s.add_argument("--company"); s.add_argument("--key")
    s.add_argument("--limit", type=int, default=20); s.add_argument("--max-details", type=int, default=5)
    _add_filter_args(s); s.set_defaults(func=cmd_check_source)

    s = sub.add_parser("add-source", help="add a careers URL (with your search/filter) to companies.yaml")
    s.add_argument("url"); s.add_argument("--company"); s.add_argument("--key")
    s.add_argument("--dry-run", action="store_true"); _add_filter_args(s); s.set_defaults(func=cmd_add_source)

    s = sub.add_parser("sources", help="list configured sources"); s.set_defaults(func=cmd_sources)

    s = sub.add_parser("run", help="run one monitoring pass")
    s.add_argument("--source", action="append", help="only this source key (repeatable)")
    s.add_argument("--trigger", default="manual"); s.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="per-source health and counts"); s.set_defaults(func=cmd_status)

    s = sub.add_parser("changes", help="recent new/updated/closed jobs")
    s.add_argument("--hours", type=float, default=24); s.add_argument("--types", help="e.g. new,closed")
    s.add_argument("--all", action="store_true", help="include jobs that don't match source filters")
    s.add_argument("--limit", type=int, default=100); s.set_defaults(func=cmd_changes)

    s = sub.add_parser("notify", help="email what changed since the last email (one digest per recipient)")
    s.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    s.add_argument("--to", action="append", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("test-email", help="send a test email using the SMTP settings"); s.set_defaults(func=cmd_test_email)

    s = sub.add_parser("sample-email", help="send a sample digest built from currently tracked jobs (records nothing)")
    s.add_argument("--limit", type=int, default=5); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sample_email)

    s = sub.add_parser("db-upgrade", help="apply database migrations"); s.set_defaults(func=cmd_db_upgrade)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(json_logs=None if args.log_format is None else args.log_format == "json")
    try:
        return args.func(args)
    except (ConfigError, UnsupportedUrl) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
