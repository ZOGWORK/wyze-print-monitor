"""Scheduled prints: the queue's data and timing rules (pure, no Qt).

A scheduled print is "press Print in eufyMake Studio at this time, on this
printer". Studio holds one sliced plate, so each job prints whatever is
sliced in its Slice tab when the job comes due — nothing is exported or
stored by us. :mod:`ui.print_schedule` does the pressing.

Jobs live in ``Settings.print_queue`` as plain dicts, times as local
ISO strings ("2026-10-28T22:00:00").
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta

# A job that comes due more than this late (the app was closed, the PC
# asleep) is not started: printing hours after the planned time is a
# surprise nobody asked for.
MISSED_AFTER = timedelta(minutes=15)
HISTORY_KEEP = 12

SCHEDULED = "scheduled"
SENDING = "sending"
STARTED = "started"
MISSED = "missed"
FAILED = "failed"
CANCELLED = "cancelled"
PENDING_STATES = (SCHEDULED, SENDING)

STATE_LABELS = {
    SCHEDULED: "Scheduled",
    SENDING: "Sending…",
    STARTED: "Sent",
    MISSED: "Missed",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
}


@dataclass
class ScheduledPrint:
    id: str
    at: str  # local ISO time it should start
    printer_row: int = 1  # 1-based row in Studio's printer list
    label: str = ""
    state: str = SCHEDULED
    detail: str = ""  # why it failed / was missed
    finished: str = ""  # local ISO time it left the queue

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)

    @property
    def pending(self) -> bool:
        return self.state in PENDING_STATES

    def finish(self, state: str, now: datetime, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.finished = now.isoformat(timespec="seconds")


def new_job(at: datetime, printer_row: int, label: str = "") -> ScheduledPrint:
    return ScheduledPrint(
        id=uuid.uuid4().hex[:12],
        at=at.replace(microsecond=0).isoformat(),
        printer_row=max(1, int(printer_row)),
        label=label.strip(),
    )


def from_dicts(items) -> list[ScheduledPrint]:
    """Jobs from settings; anything malformed is dropped, not fatal."""
    names = {f.name for f in fields(ScheduledPrint)}
    jobs = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            job = ScheduledPrint(**{k: v for k, v in item.items() if k in names})
            job.when  # noqa: B018 - validates the time
        except (TypeError, ValueError):
            continue
        jobs.append(job)
    return jobs


def to_dicts(jobs: list[ScheduledPrint]) -> list[dict]:
    return [asdict(job) for job in jobs]


def upcoming(jobs: list[ScheduledPrint]) -> list[ScheduledPrint]:
    return sorted((j for j in jobs if j.pending), key=lambda j: j.when)


def history(jobs: list[ScheduledPrint], limit: int = HISTORY_KEEP) -> list[ScheduledPrint]:
    done = [j for j in jobs if not j.pending]
    return sorted(done, key=lambda j: j.finished or j.at, reverse=True)[:limit]


def prune(jobs: list[ScheduledPrint], keep: int = HISTORY_KEEP) -> list[ScheduledPrint]:
    """Pending jobs plus the most recent ``keep`` finished ones."""
    kept = {j.id for j in history(jobs, keep)}
    return [j for j in jobs if j.pending or j.id in kept]


def due(jobs: list[ScheduledPrint], now: datetime) -> tuple[ScheduledPrint | None, list[ScheduledPrint]]:
    """(the job to start now, jobs that came due too late to start).

    Only one job starts per call — the earliest — since each press sends
    whatever Studio has sliced; the next waits for the following tick.
    """
    start = None
    missed = []
    for job in upcoming(jobs):
        if job.state != SCHEDULED or job.when > now:
            continue
        if now - job.when > MISSED_AFTER:
            missed.append(job)
        elif start is None:
            start = job
    return start, missed


def clashes(jobs: list[ScheduledPrint], at: datetime, printer_row: int, ignore: str = "") -> ScheduledPrint | None:
    """Another pending job for the same printer within a minute of ``at``."""
    for job in upcoming(jobs):
        if job.id != ignore and job.printer_row == printer_row and abs(job.when - at) < timedelta(minutes=1):
            return job
    return None


def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def describe_when(at: datetime, now: datetime) -> str:
    """"Today 3:00 PM", "Tomorrow 8:00 AM", "Wed Oct 30, 9:00 AM"."""
    days = (at.date() - now.date()).days
    if days == 0:
        return f"Today {_clock(at)}"
    if days == 1:
        return f"Tomorrow {_clock(at)}"
    if days == -1:
        return f"Yesterday {_clock(at)}"
    return f"{at.strftime('%a %b')} {at.day}, {_clock(at)}"


def describe_in(at: datetime, now: datetime) -> str:
    """"in 5 min", "in 2 h 10 min", "in 3 days", or "now"."""
    minutes = int((at - now).total_seconds() // 60)
    if minutes <= 0:
        return "now"
    if minutes < 60:
        return f"in {minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours} h {mins} min" if mins else f"in {hours} h"
    days = round(hours / 24)
    return f"in {days} day{'s' if days != 1 else ''}"


def default_time(now: datetime) -> datetime:
    """The next full hour at least 15 minutes away."""
    t = (now + timedelta(minutes=15)).replace(second=0, microsecond=0)
    return t if t.minute == 0 else t.replace(minute=0) + timedelta(hours=1)
