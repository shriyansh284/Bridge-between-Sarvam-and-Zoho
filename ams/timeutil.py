"""Time helpers. Files store naive-UTC ISO strings; people see IST."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import settings

LOCAL_TZ = ZoneInfo(settings.timezone)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def stamp() -> str:
    """Naive-UTC ISO time with microseconds, for ordering events that happen within the same second."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(text) if text else None


def to_local(dt_utc: Optional[datetime]) -> Optional[datetime]:
    if dt_utc is None:
        return None
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def local_to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=LOCAL_TZ)
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_local(dt_utc, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    if isinstance(dt_utc, str):
        dt_utc = parse_iso(dt_utc)
    local = to_local(dt_utc)
    return local.strftime(fmt) if local else ""


def next_allowed_time(dt_utc: datetime) -> datetime:
    """Earliest moment >= dt_utc inside the calling window (local time, allowed days)."""
    local = to_local(dt_utc)
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() not in settings.call_days:
            continue
        start = datetime.combine(day, settings.call_window_start, LOCAL_TZ)
        end = datetime.combine(day, settings.call_window_end, LOCAL_TZ)
        candidate = max(local, start) if offset == 0 else start
        if candidate < end:
            return dt_utc if candidate == local else candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_utc
