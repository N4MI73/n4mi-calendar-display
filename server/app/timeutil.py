"""Time helpers. Everything is local wall-clock time in America/New_York.

Events are stored as local text ('YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD'), never
converted to UTC, so a 2:30 PM appointment stays at 2:30 PM across daylight
saving changes. Conversion to an aware datetime happens only when a moment in
time is genuinely needed (for example, "is this reminder due yet?").
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

TZ_NAME = os.environ.get("TZ", "America/New_York") or "America/New_York"
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # unknown TZ value: fall back to the household's zone
    TZ = ZoneInfo("America/New_York")

DATE_FMT = "%Y-%m-%d"
DT_FMT = "%Y-%m-%dT%H:%M"


def now_local() -> datetime:
    """Current local time as a naive wall-clock datetime."""
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def parse_date(text: str) -> date:
    return datetime.strptime(text.strip(), DATE_FMT).date()


def parse_time(text: str) -> time:
    return datetime.strptime(text.strip(), "%H:%M").time()


def parse_local(text: str) -> datetime:
    """Parse stored local text. A bare date means midnight at the start of that day."""
    text = text.strip()
    if "T" in text:
        return datetime.strptime(text, DT_FMT)
    return datetime.strptime(text, DATE_FMT)


def to_aware(local_text: str) -> datetime:
    """The stored wall-clock time as an aware datetime in the household zone."""
    return parse_local(local_text).replace(tzinfo=TZ)


def fmt_time(t: time | datetime) -> str:
    """12-hour clock without a leading zero: '2:30 PM'."""
    return t.strftime("%I:%M %p").lstrip("0")


def fmt_day(d: date, today: date | None = None) -> str:
    today = today or now_local().date()
    delta = (d - today).days
    label = d.strftime("%A, %B ") + str(d.day)
    if delta == 0:
        return "Today · " + label
    if delta == 1:
        return "Tomorrow · " + label
    if delta == -1:
        return "Yesterday · " + label
    return label


def in_dark_hours(now: time, start: time, end: time) -> bool:
    """True when `now` falls in the dark period from `start` up to (not including) `end`.

    Handles a period that crosses midnight (for example 23:00 to 06:00).
    start == end means no dark hours.
    """
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end
