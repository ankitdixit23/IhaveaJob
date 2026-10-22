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
_SECTION_ORDER = [("new", "New"), ("updated", "Updated"), ("reopened", "Reopened"), ("closed", "Removed (no longer listed)")]
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


# --- HTML email (table layout + inline styles: renders in Gmail, Outlook, Yahoo, Apple Mail) ---
_FOOTER = ("Automated monitoring can miss or misread pages; always confirm details on the employer's site. "
           "\u201cRemoved\u201d means the job disappeared from the page you monitor: it may be filled, closed, or moved.")
_INK, _MUTED, _FAINT, _LINE, _BRAND = "#1f2933", "#52606d", "#8a94a6", "#e6e9ee", "#0f3d2e"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_BADGES = {  # kind: (label, text colour, background)
    "new": ("NEW", "#146c34", "#e3f5e9"),
    "updated": ("UPDATED", "#0b5cad", "#e6f0fb"),
    "reopened": ("REOPENED", "#6b3fa0", "#f0e9fa"),
    "closed": ("REMOVED", "#5b6472", "#eef0f3"),
    "page changes": ("PAGE CHANGED", "#8a5a00", "#fdf3dc"),
}
_SUMMARY_ORDER = ["new", "updated", "reopened", "closed", "page changes"]
_SUMMARY_LABEL = {"new": "New", "updated": "Updated", "reopened": "Reopened", "closed": "Removed",
                  "page changes": "Page changes"}


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _badge(kind: str) -> str:
    label, fg, bg = _BADGES[kind]
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;letter-spacing:.04em">{label}</span>')


def _button(url: str, label: str = "View job") -> str:
    return (f'<a href="{_e(url)}" style="display:inline-block;margin-top:10px;padding:7px 14px;border-radius:6px;'
            f'background:{_BRAND};color:#ffffff;text-decoration:none;font-size:13px;font-weight:600">{label} &rarr;</a>')


def _change_table(change: JobChange) -> str:
    rows = []
    for name, diff in (change.changed_fields or {}).items():
        label = _FIELD_LABELS.get(name, name)
        if name == "description_text":
            added, removed = diff.get("added") or [], diff.get("removed") or []
            lines = "".join(f'<div style="color:#146c34">+ {_e(l)}</div>' for l in added) + "".join(
                f'<div style="color:#b42318;text-decoration:line-through">{_e(l)}</div>' for l in removed)
            if not lines:
                lines = f'<span style="color:{_MUTED}">Wording changed</span>'
            rows.append(f'<tr><td style="padding:6px 8px;color:{_MUTED};vertical-align:top;white-space:nowrap;'
                        f'font-size:12px">{_e(label)}</td><td colspan="2" style="padding:6px 8px;font-size:13px">{lines}</td></tr>')
        elif isinstance(diff, dict) and "old" in diff:
            rows.append(
                f'<tr><td style="padding:6px 8px;color:{_MUTED};vertical-align:top;white-space:nowrap;font-size:12px">'
                f'{_e(label)}</td><td style="padding:6px 8px;font-size:13px;color:#b42318;text-decoration:line-through">'
                f'{_e(diff.get("old") or "—")}</td><td style="padding:6px 8px;font-size:13px;color:#146c34;font-weight:600">'
                f'{_e(diff.get("new") or "—")}</td></tr>')
    if not rows:
        return ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;'
            f'border-collapse:collapse;background:#f8fafc;border-radius:6px">'
            f'<tr><td colspan="3" style="padding:6px 8px;font-size:11px;font-weight:700;color:{_MUTED};'
            f'letter-spacing:.04em">WHAT CHANGED</td></tr>{"".join(rows)}</table>')


def _job_card(kind: str, change: JobChange, job: Job) -> str:
    url = _safe_url(job.application_url or job.url)
    title = (f'<a href="{_e(url)}" style="color:{_INK};text-decoration:none">{_e(job.title)}</a>'
             if url else _e(job.title))
    where = job.location or (job.extra or {}).get("card_text")
    meta = [m for m in (where, job.department) if m]
    meta_html = " &nbsp;·&nbsp; ".join(_e(m) for m in meta)
    posted = _published(job)
    extra = ""
    if kind == "updated":
        extra = _change_table(change)
    elif kind == "closed":
        extra = (f'<div style="margin-top:8px;font-size:12px;color:{_MUTED}">Last seen {_e(fmt_time(job.last_seen_at))}'
                 f' &nbsp;·&nbsp; first seen {_e(fmt_time(job.first_detected_at, False))}</div>')
    return (f'<tr><td style="padding:0 0 12px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {_LINE};border-radius:10px;border-collapse:separate"><tr><td style="padding:14px 16px">'
            f'{_badge(kind)}'
            f'<div style="margin-top:8px;font-size:16px;font-weight:600;line-height:1.35;color:{_INK}">{title}</div>'
            + (f'<div style="margin-top:4px;font-size:13px;color:{_MUTED}">{meta_html}</div>' if meta_html else "")
            + (f'<div style="margin-top:2px;font-size:12px;color:{_FAINT}">{_e(posted[:1].upper() + posted[1:])}</div>' if posted else "")
            + extra + (_button(url) if url and kind != "closed" else "")
            + '</td></tr></table></td></tr>')


def _page_change_card(snap: PageSnapshot) -> str:
    lines = "".join(f'<div style="color:#146c34">+ {_e(l)}</div>' for l in snap.added) + "".join(
        f'<div style="color:#b42318;text-decoration:line-through">{_e(l)}</div>' for l in snap.removed)
    return (f'<tr><td style="padding:0 0 12px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {_LINE};border-radius:10px;border-collapse:separate"><tr><td style="padding:14px 16px">'
            f'{_badge("page changes")}<div style="margin-top:6px;font-size:12px;color:{_FAINT}">'
            f'{_e(fmt_time(snap.captured_at))}</div><div style="margin-top:8px;font-size:13px;line-height:1.5">{lines}</div>'
            f'</td></tr></table></td></tr>')


def render_html(sections: list["SourceSection"], totals: dict[str, int], overflow: int,
                health: list[tuple[int, Source, str]], now: datetime, notice: str | None) -> str:
    chips = "".join(
        f'<td style="padding:0 6px 0 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="background:rgba(255,255,255,.12);border-radius:8px;padding:8px 12px;color:#ffffff">'
        f'<div style="font-size:20px;font-weight:700;line-height:1">{totals[k]}</div>'
        f'<div style="font-size:11px;opacity:.85;margin-top:3px">{_SUMMARY_LABEL[k]}</div></td></tr></table></td>'
        for k in _SUMMARY_ORDER if totals.get(k))
    header = (f'<tr><td style="background:{_BRAND};padding:22px 24px;border-radius:12px 12px 0 0">'
              f'<div style="font-size:13px;letter-spacing:.08em;color:#a7d7c0;font-weight:700">IHAVEAJOB</div>'
              f'<div style="font-size:22px;font-weight:700;color:#ffffff;margin-top:4px">Job updates</div>'
              f'<div style="font-size:13px;color:#cfe8dc;margin-top:2px">{_e(fmt_time(now))}</div>'
              + (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:16px"><tr>{chips}</tr></table>'
                 if chips else "") + '</td></tr>')
    body: list[str] = []
    if notice:
        body.append(f'<tr><td style="padding:16px 24px 0"><div style="background:#fff7e6;border:1px solid #f5c26b;'
                    f'border-radius:8px;padding:10px 12px;font-size:13px;color:#7a4d00">{_e(notice)}</div></td></tr>')
    for sec in sections:
        counts = " · ".join(f"{v} {_SUMMARY_LABEL.get(k, k).lower()}" for k, v in sec.counts().items())
        page = _safe_url(sec.source.url)
        body.append(
            f'<tr><td style="padding:22px 24px 10px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="font-size:18px;font-weight:700;color:{_INK}">{_e(sec.source.company.name)}</td>'
            + (f'<td align="right" style="font-size:12px"><a href="{_e(page)}" style="color:#0b5cad;text-decoration:none">'
               f'Careers page &#8599;</a></td>' if page else "")
            + f'</tr><tr><td colspan="2" style="font-size:12px;color:{_FAINT};padding-top:2px">{_e(counts)}</td></tr>'
            f'</table></td></tr><tr><td style="padding:0 24px"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">')
        for kind, _label in _SECTION_ORDER:
            for change, job in sec.changes.get(kind) or []:
                body.append(_job_card(kind, change, job))
        for snap in sec.snapshots:
            body.append(_page_change_card(snap))
        body.append('</table></td></tr>')
    if overflow:
        body.append(f'<tr><td style="padding:4px 24px;font-size:13px;color:{_MUTED}">…and {overflow} more changes '
                    f'(run <code>ihaveajob changes</code> to see everything).</td></tr>')
    if health:
        items = "".join(f'<li style="margin:4px 0">{_e(src.company.name)}: {_e(msg)}</li>' for _, src, msg in health)
        body.append(f'<tr><td style="padding:16px 24px 0"><div style="background:#fdecec;border:1px solid #f3b4b4;'
                    f'border-radius:8px;padding:10px 14px;font-size:13px;color:#8a1c1c"><b>Needs attention</b>'
                    f'<ul style="margin:6px 0 0;padding-left:18px">{items}</ul></div></td></tr>')
    footer = (f'<tr><td style="padding:20px 24px 24px"><div style="border-top:1px solid {_LINE};padding-top:14px;'
              f'font-size:11px;line-height:1.5;color:{_FAINT}">{_e(_FOOTER)}<br><br>Add or remove pages: GitHub &rarr; '
              f'Actions &rarr; <i>add careers page</i>, or edit <code>config/companies.yaml</code>.</div></td></tr>')
    return (f'<!doctype html><html><body style="margin:0;padding:0;background:#f3f4f6">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6">'
            f'<tr><td align="center" style="padding:24px 12px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;'
            f'background:#ffffff;border-radius:12px;font-family:{_FONT};color:{_INK}">'
            f'{header}{"".join(body)}{footer}</table></td></tr></table></body></html>')


def build_digest(
    changes: list[tuple[JobChange, Job, Source]],
    snapshots: list[tuple[PageSnapshot, Source]],
    health: list[tuple[int, Source, str]],
    now: datetime,
    notice: str | None = None,
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
    summary = ", ".join(f"{totals[k]} {_SUMMARY_LABEL[k].lower()}" for k in _SUMMARY_ORDER if totals.get(k)) \
        or "source health alert"
    companies = sorted({sec.source.company.name for sec in sections.values()})
    subject = f"IhaveaJob: {summary} · {', '.join(companies[:3])}{' +' + str(len(companies) - 3) if len(companies) > 3 else ''}"
    if not sections:
        subject = f"IhaveaJob: {len(health)} source(s) need attention"

    ordered = sorted(sections.values(), key=lambda sec: (sec.source.company.name, sec.source.key))
    text: list[str] = [f"What changed on your monitored careers pages (as of {fmt_time(now)})", ""]
    if notice:
        text = [notice, ""] + text
    for sec in ordered:
        counts = ", ".join(f"{v} {_SUMMARY_LABEL.get(k, k).lower()}" for k, v in sec.counts().items())
        head = f"{sec.source.company.name} — {counts}"
        text += ["=" * len(head), head, sec.source.url, ""]
        for kind, label in _SECTION_ORDER:
            items = sec.changes.get(kind) or []
            if not items:
                continue
            text.append(f"{label.upper()} ({len(items)})")
            for change, job in items:
                url = _safe_url(job.application_url or job.url)
                meta = _job_meta(job)
                text.append(f"• {job.title}" + (f" — {meta}" if meta else ""))
                if url:
                    text.append(f"  {url}")
                if kind == "updated":
                    text += [f"    {line}" for line in _change_lines(change)]
                if kind == "closed":
                    text.append(f"    Last seen {fmt_time(job.last_seen_at)}; first seen {fmt_time(job.first_detected_at, False)}.")
            text.append("")
        for snap in sec.snapshots:
            text.append(f"PAGE TEXT CHANGED ({fmt_time(snap.captured_at)})")
            text += [f"  + {l}" for l in snap.added] + [f"  − {l}" for l in snap.removed] + [""]
    if overflow:
        text.append(f"…and {overflow} more changes. Run `ihaveajob changes` to see everything.")
    if health:
        text += ["", "NEEDS ATTENTION"] + [f"• {src.company.name} ({src.key}): {msg}" for _, src, msg in health]
    text += ["", "—", _FOOTER]
    html_body = render_html(ordered, totals, overflow, health, now, notice)
    return Digest(subject=subject, text="\n".join(text), html=html_body, change_ids=shown_changes,
                  snapshot_ids=[s.id for s, _src in snapshots], health_ids=[i for i, _, _ in health])
