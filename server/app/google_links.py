""""Not in Google" and the Add to Google link (Phase 5b, decisions D6 and D53).

The calendar never writes to Google. Instead, a local event that has no Google
twin gets a link that opens Google Calendar's new-event page already filled
in; Dan checks it and presses Save himself. On his next fetch the event shows
under "Already here?" and he links it, which takes it off the list.

The list is relative to the last fetch: until Google has been read at least
once, nothing can be called "not in Google".
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from . import ical_import, store, timeutil

GOOGLE_NEW_EVENT = "https://calendar.google.com/calendar/render"


def last_fetch(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(last_fetch) FROM import_sources").fetchone()
    return row[0] if row else None


def not_in_google(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Local events with a date from today through 12 months ahead that aren't tied to a Google
    event and weren't marked "not needed". Each once, with its next date."""
    hi = today + timedelta(days=ical_import.WINDOW_DAYS)
    seen: dict[int, dict] = {}
    for occ in store.list_events(conn, today, hi):
        if occ["id"] in seen or occ.get("google_uid") or occ.get("not_needed_in_google"):
            continue
        seen[occ["id"]] = occ
    out = []
    for occ in seen.values():
        ev = store.get_event(conn, occ["id"])
        out.append({"event": ev, "next": occ})
    out.sort(key=lambda x: (x["next"]["start_local"], x["event"]["title"]))
    return out


def set_not_needed(conn: sqlite3.Connection, event_id: int, value: bool = True) -> None:
    conn.execute("UPDATE events SET not_needed_in_google = ? WHERE id = ?", (1 if value else 0, event_id))
    conn.commit()


def list_not_needed(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id FROM events WHERE not_needed_in_google = 1 ORDER BY title COLLATE NOCASE")
    return [store.get_event(conn, r[0]) for r in rows]


def _stamp(text: str, all_day: bool) -> str:
    t = timeutil.parse_local(text)
    return t.strftime("%Y%m%d") if all_day else t.strftime("%Y%m%dT%H%M%S")


def add_to_google_url(event: dict) -> str:
    """Google Calendar's new-event page, pre-filled from one of this calendar's events.

    Times are local wall-clock times with ctz=America/New_York; an all-day event's
    end date is exclusive in Google's format. A repeating event carries its rule
    (Google's `recur` field; its skipped and changed dates are not carried over).
    """
    all_day = bool(event["all_day"])
    start = _stamp(event["start_local"], all_day)
    if all_day:
        end = (timeutil.parse_local(event["end_local"]) + timedelta(days=1)).strftime("%Y%m%d")
    else:
        end = _stamp(event["end_local"], False)
    params = [("action", "TEMPLATE"), ("text", event["title"]), ("dates", f"{start}/{end}"),
              ("ctz", str(timeutil.TZ))]
    if event.get("notes"):
        params.append(("details", event["notes"]))
    if event.get("location"):
        params.append(("location", event["location"]))
    if event.get("rrule"):
        rule = event["rrule"]
        if event.get("recurrence_end"):
            rule += ";UNTIL=" + timeutil.parse_date(event["recurrence_end"]).strftime("%Y%m%d")
        params.append(("recur", "RRULE:" + rule))
    return GOOGLE_NEW_EVENT + "?" + urlencode(params, quote_via=quote)
