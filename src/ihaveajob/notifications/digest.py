"""Build the "what changed" digest (plain text + HTML) from pending changes.

All job/page text is external, untrusted content: it is HTML-escaped and
links are only rendered for http(s) URLs.
"""

from __future__ import annotations

import html
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..database.models import Job, JobChange, PageSnapshot, Source

MAX_JOB_ITEMS = 150
_SECTION_ORDER = [("new", "New"), ("updated", "Updated"), ("reopened", "Reopened"), ("closed", "No longer listed")]
_FIELD_LABELS = {"title": "Title", "location": "Location", "department": "Department",
                 "employment_type": "Employment type", "workplace_type": "Workplace", "application_url": "Apply link",
                 "description_text": "Description"}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("IHAVEAJOB_TIMEZONE") or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def fmt_time(dt: datetime | None, with_time: bool = True) -> str:
    if dt is None:
        return "unknown"
    local = dt.astimezone(_tz())
    return local.strftime("%b %d, %Y %I:%M %p %Z" if with_time else "%b %d, %Y").replace(" 0", " ")


def _safe_url(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


@dataclass
class SourceSection:
    source: Source
    changes: dict[str, list[tuple[JobChange, Job]]] = field(default_factory=lambda: defaultdict(list))
    snapshots: list[PageSnapshot] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {k: len(v) for k, v in self.changes.items() if v}
        if self.snapshots:
            c["page changes"] = len(self.snapshots)
        return c


@dataclass
class Digest:
    subject: str
    text: str
    html: str
    change_ids: list[int]
    snapshot_ids: list[int]
    health_ids: list[int]


def _published(job: Job) -> str:
    if not job.published_at:
        return ""
    approx = " (approx.)" if (job.published_at_source or "").endswith((".inferred", ".relative")) else ""
    return f"posted {fmt_time(job.published_at, with_time=False)}{approx}"


def _job_meta(job: Job) -> str:
    bits = [job.location or (job.extra or {}).get("card_text"), job.department, _published(job)]
    return " · ".join(b for b in bits if b)


def _change_lines(change: JobChange) -> list[str]:
    out = []
    for name, diff in (change.changed_fields or {}).items():
        label = _FIELD_LABELS.get(name, name)
        if name == "description_text":
            added, removed = diff.get("added") or [], diff.get("removed") or []
            out.append(f"{label} changed ({diff.get('old_len', '?')} → {diff.get('new_len', '?')} characters)")
            out += [f"+ {line}" for line in added] + [f"− {line}" for line in removed]
        elif isinstance(diff, dict) and "old" in diff:
            out.append(f"{label}: {diff.get('old') or '—'} → {diff.get('new') or '—'}")
    return out


def build_digest(
    changes: list[tuple[JobChange, Job, Source]],
    snapshots: list[tuple[PageSnapshot, Source]],
    health: list[tuple[int, Source, str]],
    now: datetime,
) -> Digest | None:
    if not changes and not snapshots and not health:
        return None
    sections: dict[int, SourceSection] = {}
    shown_changes: list[int] = []
    overflow = 0
    for change, job, source in changes:
        sec = sections.setdefault(source.id, SourceSection(source))
        if len(shown_changes) >= MAX_JOB_ITEMS:
            overflow += 1
        else:
            sec.changes[change.change_type].append((change, job))
        shown_changes.append(change.id)  # mark all as delivered; overflow is summarised
    for snap, source in snapshots:
        sections.setdefault(source.id, SourceSection(source)).snapshots.append(snap)

    totals: dict[str, int] = defaultdict(int)
    for sec in sections.values():
        for k, v in sec.counts().items():
            totals[k] += v
    summary = ", ".join(f"{v} {k}" for k, v in totals.items()) or "source health alert"
    companies = sorted({sec.source.company.name for sec in sections.values()})
    subject = f"IhaveaJob: {summary} · {', '.join(companies[:3])}{' +' + str(len(companies) - 3) if len(companies) > 3 else ''}"
    if not sections:
        subject = f"IhaveaJob: {len(health)} source(s) need attention"

    text: list[str] = [f"What changed on your monitored careers pages (as of {fmt_time(now)})", ""]
    h: list[str] = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
                    'max-width:680px;color:#1f2933;line-height:1.45">',
                    f'<p style="color:#52606d;margin:0 0 16px">What changed on your monitored careers pages '
                    f'(as of {html.escape(fmt_time(now))})</p>']
    for sec in sorted(sections.values(), key=lambda s: (s.source.company.name, s.source.key)):
        counts = ", ".join(f"{v} {k}" for k, v in sec.counts().items())
        head = f"{sec.source.company.name} — {counts}"
        text += ["=" * len(head), head, sec.source.url, ""]
        h.append(f'<h2 style="font-size:18px;margin:24px 0 2px">{html.escape(sec.source.company.name)}'
                 f'<span style="font-weight:400;color:#52606d;font-size:14px"> · {html.escape(counts)}</span></h2>'
                 f'<div style="font-size:12px;color:#7b8794;margin-bottom:8px">{html.escape(sec.source.url)}</div>')
        for kind, label in _SECTION_ORDER:
            items = sec.changes.get(kind) or []
            if not items:
                continue
            text.append(f"{label.upper()} ({len(items)})")
            h.append(f'<h3 style="font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#3e4c59;'
                     f'margin:14px 0 6px">{label} ({len(items)})</h3><ul style="padding-left:18px;margin:0">')
            for change, job in items:
                url = _safe_url(job.application_url or job.url)
                meta = _job_meta(job)
                text.append(f"• {job.title}" + (f" — {meta}" if meta else ""))
                if url:
                    text.append(f"  {url}")
                title_html = (f'<a href="{html.escape(url, quote=True)}" style="color:#0b6bcb;font-weight:600">'
                              f'{html.escape(job.title)}</a>' if url else f"<b>{html.escape(job.title)}</b>")
                h.append(f'<li style="margin:0 0 10px">{title_html}'
                         + (f'<div style="font-size:13px;color:#52606d">{html.escape(meta)}</div>' if meta else ""))
                if kind == "updated":
                    lines = _change_lines(change)
                    text += [f"    {line}" for line in lines]
                    h.append('<div style="font-size:13px;margin-top:4px">' + "".join(
                        f'<div style="color:{"#1b7f3b" if l.startswith("+ ") else "#b42318" if l.startswith("− ") else "#3e4c59"};'
                        f'white-space:pre-wrap">{html.escape(l)}</div>' for l in lines) + "</div>")
                if kind == "closed":
                    note = f"Not seen since {fmt_time(job.last_seen_at)}; first seen {fmt_time(job.first_detected_at, False)}."
                    text.append(f"    {note}")
                    h.append(f'<div style="font-size:13px;color:#52606d">{html.escape(note)}</div>')
                h.append("</li>")
            text.append("")
            h.append("</ul>")
        for snap in sec.snapshots:
            text.append(f"PAGE TEXT CHANGED ({fmt_time(snap.captured_at)})")
            text += [f"  + {l}" for l in snap.added] + [f"  − {l}" for l in snap.removed] + [""]
            h.append('<h3 style="font-size:13px;text-transform:uppercase;color:#3e4c59;margin:14px 0 6px">Page text '
                     f'changed</h3><div style="font-size:13px">' + "".join(
                         f'<div style="color:#1b7f3b;white-space:pre-wrap">+ {html.escape(l)}</div>' for l in snap.added)
                     + "".join(f'<div style="color:#b42318;white-space:pre-wrap">− {html.escape(l)}</div>'
                               for l in snap.removed) + "</div>")
    if overflow:
        text.append(f"…and {overflow} more changes. Run `ihaveajob changes` to see everything.")
        h.append(f"<p>…and {overflow} more changes. Run <code>ihaveajob changes</code> to see everything.</p>")
    if health:
        text += ["", "NEEDS ATTENTION"] + [f"• {src.company.name} ({src.key}): {msg}" for _, src, msg in health]
        h.append('<h3 style="font-size:13px;text-transform:uppercase;color:#b42318;margin:20px 0 6px">Needs attention</h3><ul>'
                 + "".join(f"<li>{html.escape(src.company.name)} ({html.escape(src.key)}): {html.escape(msg)}</li>"
                           for _, src, msg in health) + "</ul>")
    foot = ("Automated monitoring can miss or misread pages. Always confirm details on the employer's site. "
            "'No longer listed' means the job disappeared from the page you monitor; it may be filled, closed, or moved.")
    text += ["", "—", foot]
    h.append(f'<p style="font-size:12px;color:#7b8794;margin-top:24px;border-top:1px solid #e4e7eb;padding-top:10px">'
             f"{html.escape(foot)}</p></div>")
    return Digest(subject=subject, text="\n".join(text), html="".join(h), change_ids=shown_changes,
                  snapshot_ids=[s.id for s, _src in snapshots], health_ids=[i for i, _, _ in health])
