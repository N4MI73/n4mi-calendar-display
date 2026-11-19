"""The display's data: what GET /api/v1/display returns (Phase 3).

The server does the formatting and the recurrence; the display page only lays
the data out and works out the banner from it. The response covers today plus
the Upcoming range plus CACHE_EXTRA_DAYS, so the display can keep rolling
forward from its saved copy for about a week if the server can't be reached
(decision D41).

Every event carries `remind_at`: the local time its banner starts (from the
event's or category's reminder, D27/D43/D44), already moved out of dark hours,
so the page needs no reminder rules of its own. The display never computes
recurrence (D8).

The response is versioned (`api`); fields are only ever added, so a second
display client (the bedside companion) can use it later.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, time, timedelta

from . import holidays, recurrence, store, timeutil

API_VERSION = 1
CACHE_EXTRA_DAYS = 7
INFO_CATEGORY = "info"  # events in this category are shown lighter, without person pills


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _out_of_dark(at: datetime, dark_start: time, dark_end: time) -> datetime:
    """The first moment at or after `at` that is outside dark hours."""
    if not timeutil.in_dark_hours(at.time(), dark_start, dark_end):
        return at
    end = datetime.combine(at.date(), dark_end)
    return end if end > at else end + timedelta(days=1)


def remind_at(event: dict, dark_start: time, dark_end: time) -> datetime | None:
    """When the banner for this event (or occurrence) should start, or None for no reminder."""
    minutes = store.effective_reminder_minutes(event)
    if minutes is None:
        return None
    start = timeutil.parse_local(event["start_local"])
    if event["all_day"]:
        # Whole days before, shown from the end of dark hours that morning (D44).
        days = math.ceil(minutes / 1440)
        morning = dark_end if dark_start != dark_end else time(0, 0)
        return datetime.combine(start.date() - timedelta(days=days), morning)
    return _out_of_dark(start - timedelta(minutes=minutes), dark_start, dark_end)


def _fmt_local(dt: datetime) -> str:
    return dt.strftime(timeutil.DT_FMT)


def _event_json(e: dict, dark_start: time, dark_end: time) -> dict:
    start = timeutil.parse_local(e["start_local"])
    end = timeutil.parse_local(e["end_local"])
    at = remind_at(e, dark_start, dark_end)
    info = (e.get("category_name") or "").strip().lower() == INFO_CATEGORY
    return {
        "id": e["id"],
        "occ": e.get("occ_date"),
        "title": e["title"],
        "location": e["location"] or "",
        "all_day": bool(e["all_day"]),
        "start": _fmt_local(start),
        "end": _fmt_local(end),
        "time": "" if e["all_day"] else timeutil.fmt_time(start),
        "end_time": "" if e["all_day"] else timeutil.fmt_time(end),
        "people": [{"name": p["name"], "color": p["color"]} for p in e.get("people", [])],
        "category": {"name": e["category_name"], "color": e["category_color"]},
        "info": info,
        "remind_at": _fmt_local(at) if at else None,
        "repeats": bool(e.get("recurring")),
        # Phase 4 (D48, D50): the details panel.
        "notes": e["notes"] or "",
        "repeat_text": recurrence.describe(e["rrule"], timeutil.parse_local(e["start_local"]).date(),
                                           e["recurrence_end"]) if e.get("recurring") else "",
        "changed": bool(e.get("changed")),
    }


def _day_json(d: date) -> dict:
    return {
        "date": d.strftime(timeutil.DATE_FMT),
        "weekday": d.strftime("%A"),
        "weekday_short": d.strftime("%a"),
        "month": d.strftime("%B"),
        "month_short": d.strftime("%b"),
        "day": d.day,
        "events": [],
        "holidays": [],      # Phase 5b (D59): names of the holidays on this day, if shown
    }


def _fill_days(conn: sqlite3.Connection, cfg: dict, first: date, last: date, carry_in: bool = True) -> dict:
    """Days `first`..`last` (inclusive) with their events and holidays, keyed by date.

    `carry_in`: list events that began before `first` and are still on (the main
    schedule, whose first day is today). The Coming up panel leaves them out:
    they are already in Today, or in an earlier part of the panel.
    """
    dark_start = timeutil.parse_time(cfg["dark_start"])
    dark_end = timeutil.parse_time(cfg["dark_end"])
    days = {}
    d = first
    while d <= last:
        days[d] = _day_json(d)
        d += timedelta(days=1)

    for e in store.list_events(conn, first, last):
        item = _event_json(e, dark_start, dark_end)
        start = timeutil.parse_local(e["start_local"]).date()
        end = timeutil.parse_local(e["end_local"]).date()
        if start < first and not carry_in:
            continue
        if e["all_day"]:
            # A multi-day event appears on each of its days, with "day N of M" (D41).
            count = (end - start).days + 1
            d = max(start, first)
            while d <= min(end, last):
                entry = dict(item, day_index=(d - start).days + 1, day_count=count,
                             until=end.strftime("%a") if count > 1 else "")
                days[d]["events"].append(entry)
                d += timedelta(days=1)
        else:
            # A timed event is listed on the day it starts (or the first day, if it began earlier and is still on).
            days[max(start, first)]["events"].append(dict(item, day_index=1, day_count=1, until=""))

    for hd, names in holidays.between(first, last, holidays.chosen(cfg)).items():
        days[hd]["holidays"] = names
    return days


# The Coming up panel (v0.6.2): the shelf asks for the days after the main
# schedule's window, 30 at a time, up to 12 months ahead.
PANEL_LIMIT_DAYS = 365      # the furthest day offered: today + this
PANEL_MAX_DAYS = 366        # most days in one request (a reload after a change asks for all loaded so far)


def days_range(conn: sqlite3.Connection, now: datetime, first: date, count: int) -> dict:
    """Days from `first` for `count` days, clipped to the 12-month limit.

    Raises ValueError for a start before today or past the limit, or a bad count.
    """
    today = now.date()
    limit = today + timedelta(days=PANEL_LIMIT_DAYS)
    if not 1 <= count <= PANEL_MAX_DAYS:
        raise ValueError("bad_days")
    if first < today or first > limit:
        raise ValueError("bad_from")
    last = min(first + timedelta(days=count - 1), limit)
    cfg = store.get_config(conn)
    days = _fill_days(conn, cfg, first, last, carry_in=False)
    return {
        "ok": True,
        "api": API_VERSION,
        "generated": _fmt_local(now),
        "from": first.strftime(timeutil.DATE_FMT),
        "to": last.strftime(timeutil.DATE_FMT),
        "limit": limit.strftime(timeutil.DATE_FMT),
        "more": last < limit,
        "days": [days[k] for k in sorted(days)],
    }


def build(conn: sqlite3.Connection, now: datetime) -> dict:
    """The display payload for local time `now`."""
    cfg = store.get_config(conn)
    upcoming = _int(cfg["upcoming_days"], 14)
    today = now.date()
    last = today + timedelta(days=upcoming + CACHE_EXTRA_DAYS)
    days = _fill_days(conn, cfg, today, last)

    reminders = [
        {"id": r["id"], "text": r["text"], "carry_over": bool(r["carry_over"]), "due": r["due_occ"]}
        for r in store.reminders_showing(conn, now)
    ]

    return {
        "ok": True,
        "api": API_VERSION,
        "generated": _fmt_local(now),
        "config": {
            "upcoming_days": upcoming,
            "stale_minutes": _int(cfg["stale_minutes"], 30),
            "dark_start": cfg["dark_start"],
            "dark_end": cfg["dark_end"],
            "wake_seconds": _int(cfg["wake_seconds"], 120),
        },
        "title": cfg.get("display_title", ""),   # Phase 6a (D65): empty hides it
        "days": [days[k] for k in sorted(days)],
        "reminders": reminders,
        # Phase 4: choices for the Add form (D48, D49).
        "people": [{"id": p["id"], "name": p["name"], "color": p["color"]} for p in store.list_people(conn)],
        "categories": [{"id": c["id"], "name": c["name"], "color": c["color"]} for c in store.list_categories(conn)],
        "quick_titles": [{"title": q["title"], "category_id": q["category_id"]}
                         for q in store.list_quick_titles(conn)[:store.MAX_QUICK_TITLES_SHOWN]],
    }
