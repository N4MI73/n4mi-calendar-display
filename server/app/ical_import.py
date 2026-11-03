"""Reading a Google calendar's private iCal feed (Phase 5a, decisions D4, D54).

fetch() downloads the feed; parse() turns it into plain dicts the importer
compares with the calendar. Nothing here writes anywhere, and nothing here
ever puts the feed's address into a message: the address is a credential, so
errors name only what went wrong ("timed out", "HTTP 404").

Times are converted to local wall-clock text in the household zone, the same
form the rest of the calendar stores ('YYYY-MM-DDTHH:MM', or 'YYYY-MM-DD' for
all-day events with an inclusive end date). A repeating Google event becomes
one series: its RRULE (without UNTIL, which becomes `recurrence_end`), its
excluded dates, and its changed dates (RECURRENCE-ID overrides).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from icalendar import Calendar

from . import recurrence, timeutil

FETCH_TIMEOUT = 20                # seconds
MAX_BYTES = 10 * 1024 * 1024      # a household calendar feed is far smaller
WINDOW_DAYS = 365                 # today through 12 months ahead (D58)
MAX_TITLE, MAX_LOCATION, MAX_NOTES = 120, 200, 2000


class FetchError(Exception):
    """The feed couldn't be read. The message never contains the address."""


def fetch(url: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    if not url or not url.startswith("https://"):
        raise FetchError("The address isn't set, or doesn't start with https://.")
    req = urllib.request.Request(url, headers={"User-Agent": "N4MI-Calendar-Display"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        hint = " (the address may have been reset in Google)" if exc.code in (401, 403, 404) else ""
        raise FetchError(f"Google answered HTTP {exc.code}{hint}.") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"Couldn't connect ({type(exc.reason).__name__}).") from None
    except TimeoutError:
        raise FetchError("Timed out waiting for Google.") from None
    except Exception as exc:  # never let a message carry the address
        raise FetchError(f"Couldn't read the feed ({type(exc).__name__}).") from None
    if len(data) > MAX_BYTES:
        raise FetchError("The feed is unexpectedly large, so it wasn't read.")
    if b"BEGIN:VCALENDAR" not in data[:4096]:
        raise FetchError("That address didn't return a calendar file.")
    return data


# ----------------------------------------------------------------- parsing


_TAG_RE = re.compile(r"<[^>]+>")


def _text(value, limit: int) -> str:
    """Plain text: Google descriptions can carry simple HTML."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _local(value) -> datetime | date:
    """A DTSTART/DTEND value as local wall-clock time (or a plain date for all-day)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timeutil.TZ).replace(tzinfo=None)
        return value                     # "floating" time: already wall-clock
    return value                         # a date: all-day


def _when(comp) -> tuple[bool, str, str] | None:
    """(all_day, start_local, end_local) for a VEVENT, or None if it has no start."""
    if comp.get("DTSTART") is None:
        return None
    start = _local(comp.decoded("DTSTART"))
    end = _local(comp.decoded("DTEND")) if comp.get("DTEND") is not None else None
    if end is None and comp.get("DURATION") is not None:
        end = start + comp.decoded("DURATION")
    if not isinstance(start, datetime):
        # All-day: iCal's end date is exclusive; the calendar stores an inclusive one.
        last = (end - timedelta(days=1)) if isinstance(end, date) and not isinstance(end, datetime) else start
        if last < start:
            last = start
        return True, start.strftime(timeutil.DATE_FMT), last.strftime(timeutil.DATE_FMT)
    if end is None or not isinstance(end, datetime) or end < start:
        end = start
    return False, start.strftime(timeutil.DT_FMT), end.strftime(timeutil.DT_FMT)


def _rule_and_end(comp) -> tuple[str | None, str | None]:
    """RRULE text without UNTIL, and UNTIL as a local last date."""
    if comp.get("RRULE") is None:
        return None, None
    rr = comp.get("RRULE")
    if isinstance(rr, list):            # more than one RRULE is rare; the first one is used
        rr = rr[0]
    parts = dict(rr)
    until = parts.pop("UNTIL", None)
    parts.pop("DTSTART", None)
    end = None
    if until:
        u = until[0] if isinstance(until, list) else until
        u = _local(u)
        end = (u.date() if isinstance(u, datetime) else u).strftime(timeutil.DATE_FMT)
    order = ["FREQ", "INTERVAL", "COUNT", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"]
    keys = [k for k in order if k in parts] + sorted(k for k in parts if k not in order)
    from icalendar.prop import vRecur
    text = vRecur({k: parts[k] for k in keys}).to_ical().decode()
    return text, end


def _dates(comp, name: str) -> list[str]:
    """EXDATE values as local dates (the dates the occurrences originally fell on)."""
    out = []
    raw = comp.get(name)
    if raw is None:
        return out
    for group in (raw if isinstance(raw, list) else [raw]):
        for d in group.dts:
            v = _local(d.dt)
            out.append((v.date() if isinstance(v, datetime) else v).strftime(timeutil.DATE_FMT))
    return sorted(set(out))


def _base(comp) -> dict:
    return {
        "title": _text(comp.get("SUMMARY"), MAX_TITLE) or "(No title)",
        "location": _text(comp.get("LOCATION"), MAX_LOCATION),
        "notes": _text(comp.get("DESCRIPTION"), MAX_NOTES),
    }


def _occurs_in(ev: dict, lo: date, hi: date) -> bool:
    """Does an event (or any date of a series) fall in lo..hi?"""
    start = timeutil.parse_local(ev["start_local"])
    end = timeutil.parse_local(ev["end_local"])
    if not ev["rrule"]:
        return start.date() <= hi and end.date() >= lo
    span = end - start
    lo_dt = datetime.combine(lo, datetime.min.time()) - span
    hi_dt = datetime.combine(hi, datetime.max.time())
    skipped = set(ev["exdates"])
    for occ in recurrence.occurrences(ev["rrule"], start, ev["recurrence_end"], lo_dt, hi_dt):
        if occ.date().strftime(timeutil.DATE_FMT) not in skipped:
            return True
    # A changed date may have moved into the window.
    return any(timeutil.parse_local(o["start_local"]).date() <= hi and
               timeutil.parse_local(o["end_local"]).date() >= lo for o in ev["overrides"].values())


def parse(data: bytes, today: date, window_days: int = WINDOW_DAYS) -> dict:
    """Events in the feed.

    Returns {"events": [...in the window...], "uids": set of every UID in the feed}.
    Each event: uid, title, location, notes, all_day, start_local, end_local, rrule,
    recurrence_end, exdates (list of original dates), overrides ({original date:
    {start_local, end_local, title, location, notes}}).
    `uids` covers the whole feed, so an event that simply moved beyond the window
    isn't mistaken for one removed from Google.
    """
    try:
        cal = Calendar.from_ical(data)
    except Exception:
        raise FetchError("The calendar file couldn't be read.") from None
    masters: dict[str, dict] = {}
    overrides: dict[str, list] = {}
    all_uids: set[str] = set()
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID") or "").strip()
        if not uid:
            continue
        all_uids.add(uid)
        cancelled = str(comp.get("STATUS") or "").upper() == "CANCELLED"
        when = _when(comp)
        if when is None:
            continue
        if comp.get("RECURRENCE-ID") is not None:
            rid = _local(comp.decoded("RECURRENCE-ID"))
            key = (rid.date() if isinstance(rid, datetime) else rid).strftime(timeutil.DATE_FMT)
            overrides.setdefault(uid, []).append((key, cancelled, when, _base(comp)))
            continue
        if cancelled:
            continue
        all_day, start_local, end_local = when
        rrule, rec_end = _rule_and_end(comp)
        masters[uid] = dict(_base(comp), uid=uid, all_day=all_day, start_local=start_local,
                            end_local=end_local, rrule=rrule, recurrence_end=rec_end,
                            exdates=_dates(comp, "EXDATE") if rrule else [], overrides={})
    # Attach changed and cancelled dates to their series.
    for uid, items in overrides.items():
        master = masters.get(uid)
        for key, cancelled, (all_day, start_local, end_local), base in items:
            if master is not None and master["rrule"]:
                if cancelled:
                    master["exdates"] = sorted(set(master["exdates"]) | {key})
                else:
                    master["overrides"][key] = dict(base, start_local=start_local, end_local=end_local)
            elif not cancelled and master is None:
                # A changed date whose series isn't in the feed: treat it as a one-off.
                masters[f"{uid}::{key}"] = dict(base, uid=f"{uid}::{key}", all_day=all_day,
                                                start_local=start_local, end_local=end_local, rrule=None,
                                                recurrence_end=None, exdates=[], overrides={})
                all_uids.add(f"{uid}::{key}")
    for m in masters.values():
        # A changed date that is identical to the normal one isn't a change.
        for key in list(m["overrides"]):
            if key in m["exdates"]:
                del m["overrides"][key]
    lo, hi = today, today + timedelta(days=window_days)
    events = [m for m in masters.values() if _occurs_in(m, lo, hi)]
    events.sort(key=lambda e: (e["start_local"], e["title"]))
    return {"events": events, "uids": all_uids}
