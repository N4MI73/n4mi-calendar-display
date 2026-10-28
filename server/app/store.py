"""Reading and writing calendar data: people, categories, events, reminders and settings.

Validation lives here too, so the editor and (later) the display API share
the same rules. Functions take an open connection and return plain dicts.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import recurrence, timeutil

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_TITLE = 120
MAX_LOCATION = 200
MAX_NOTES = 2000
MAX_NAME = 40
MAX_REMINDER = 7 * 24 * 60  # one week, in minutes
MAX_REMINDER_TEXT = 80

# Default settings, used if a key is somehow missing from the config table.
DEFAULT_CONFIG = {
    "upcoming_days": "14",
    "stale_minutes": "30",
    "dark_start": "23:00",
    "dark_end": "06:00",
    "wake_seconds": "120",
}

# Suggested colors for new people, in order; the first one not yet used is offered.
PERSON_PALETTE = ["#4C9AFF", "#F06BA8", "#2BB3A3", "#F5A524", "#A78BFA", "#7DD3FC", "#FB7185"]


class ValidationError(Exception):
    """Raised with a dict of field -> message when input is not acceptable."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class InUseError(Exception):
    """Raised when deleting a person or category that events still use."""

    def __init__(self, count: int):
        super().__init__(f"used by {count} event(s)")
        self.count = count


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: str | None) -> str:
    return (text or "").strip()


# --------------------------------------------------------------------- people


def list_people(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM event_people ep WHERE ep.person_id = p.id) AS event_count "
        "FROM people p ORDER BY p.sort_order, p.name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_person(conn: sqlite3.Connection, person_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    return dict(row) if row else None


def suggest_person_color(conn: sqlite3.Connection) -> str:
    used = {r["color"].upper() for r in conn.execute("SELECT color FROM people")}
    for c in PERSON_PALETTE:
        if c.upper() not in used:
            return c
    return PERSON_PALETTE[0]


def _validate_name_color(name: str, color: str) -> dict[str, str]:
    errors = {}
    if not name:
        errors["name"] = "Enter a name."
    elif len(name) > MAX_NAME:
        errors["name"] = f"Keep the name to {MAX_NAME} characters or fewer."
    if not HEX_RE.match(color):
        errors["color"] = "Choose a color."
    return errors


def save_person(conn: sqlite3.Connection, name: str, color: str, person_id: int | None = None) -> int:
    name, color = _clean(name), _clean(color)
    errors = _validate_name_color(name, color)
    if not errors:
        clash = conn.execute(
            "SELECT id FROM people WHERE name = ? COLLATE NOCASE AND id IS NOT ?", (name, person_id)
        ).fetchone()
        if clash:
            errors["name"] = "Someone with that name already exists."
    if errors:
        raise ValidationError(errors)
    if person_id is None:
        order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM people").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO people (name, color, sort_order) VALUES (?, ?, ?)", (name, color.upper(), order)
        )
        conn.commit()
        return cur.lastrowid
    conn.execute("UPDATE people SET name = ?, color = ? WHERE id = ?", (name, color.upper(), person_id))
    conn.commit()
    return person_id


def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    count = conn.execute("SELECT COUNT(*) FROM event_people WHERE person_id = ?", (person_id,)).fetchone()[0]
    if count:
        raise InUseError(count)
    conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    conn.commit()


# ----------------------------------------------------------------- categories


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM events e WHERE e.category_id = c.id) AS event_count "
        "FROM categories c ORDER BY c.sort_order, c.name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_category(conn: sqlite3.Connection, category_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    return dict(row) if row else None


def _parse_reminder(value: str | None, field_name: str, errors: dict[str, str], allow_blank: bool) -> int | None:
    value = _clean(value)
    if value == "":
        if allow_blank:
            return None
        errors[field_name] = "Enter the number of minutes."
        return None
    try:
        minutes = int(value)
    except ValueError:
        errors[field_name] = "Use a whole number of minutes."
        return None
    if minutes < 0 or minutes > MAX_REMINDER:
        errors[field_name] = "Use between 0 minutes and one week (10080 minutes)."
        return None
    return minutes


def save_category(
    conn: sqlite3.Connection,
    name: str,
    color: str,
    default_reminder: str | None,
    category_id: int | None = None,
) -> int:
    """default_reminder: minutes as text, or blank for 'no reminder'."""
    name, color = _clean(name), _clean(color)
    errors = _validate_name_color(name, color)
    minutes = _parse_reminder(default_reminder, "default_reminder", errors, allow_blank=True)
    if "name" not in errors:
        clash = conn.execute(
            "SELECT id FROM categories WHERE name = ? COLLATE NOCASE AND id IS NOT ?", (name, category_id)
        ).fetchone()
        if clash:
            errors["name"] = "A category with that name already exists."
    if errors:
        raise ValidationError(errors)
    if category_id is None:
        order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO categories (name, color, default_reminder_minutes, sort_order) VALUES (?, ?, ?, ?)",
            (name, color.upper(), minutes, order),
        )
        conn.commit()
        return cur.lastrowid
    conn.execute(
        "UPDATE categories SET name = ?, color = ?, default_reminder_minutes = ? WHERE id = ?",
        (name, color.upper(), minutes, category_id),
    )
    conn.commit()
    return category_id


def delete_category(conn: sqlite3.Connection, category_id: int) -> None:
    count = conn.execute("SELECT COUNT(*) FROM events WHERE category_id = ?", (category_id,)).fetchone()[0]
    if count:
        raise InUseError(count)
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()


# --------------------------------------------------------------------- events


@dataclass
class EventInput:
    """Raw form values for an event, as text."""

    title: str = ""
    location: str = ""
    notes: str = ""
    all_day: bool = False
    start_date: str = ""
    start_time: str = ""
    end_date: str = ""
    end_time: str = ""
    category_id: str = ""
    person_ids: list[str] = field(default_factory=list)
    reminder_mode: str = "default"
    reminder_minutes: str = ""
    # Repeats (Phase 2). See recurrence.Pattern.
    repeat: str = "none"
    interval: str = "1"
    weekdays: list[str] = field(default_factory=list)
    ordinals: list[str] = field(default_factory=list)
    nth_weekday: str = ""
    until: str = ""
    custom_rrule: str = ""

    def pattern(self) -> recurrence.Pattern:
        return recurrence.Pattern(
            repeat=self.repeat, interval=self.interval, weekdays=list(self.weekdays),
            ordinals=list(self.ordinals), nth_weekday=self.nth_weekday, until=self.until,
            custom_rrule=self.custom_rrule,
        )


def _validate_when(all_day: bool, start_date: str, start_time: str, end_date: str, end_time: str,
                   errors: dict[str, str]) -> tuple[str | None, str | None, date | None]:
    """Check dates and times. Returns (start_local, end_local, start date)."""
    start_local = end_local = None
    try:
        start_d = timeutil.parse_date(start_date)
    except ValueError:
        start_d = None
        errors["start_date"] = "Enter a start date."
    try:
        end_d = timeutil.parse_date(end_date) if _clean(end_date) else start_d
    except ValueError:
        end_d = None
        errors["end_date"] = "Enter a valid end date."

    if all_day:
        if start_d and end_d:
            if end_d < start_d:
                errors["end_date"] = "The end date can't be before the start date."
            else:
                start_local = start_d.strftime(timeutil.DATE_FMT)
                end_local = end_d.strftime(timeutil.DATE_FMT)
    else:
        st = et = None
        if not TIME_RE.match(_clean(start_time)):
            errors["start_time"] = "Enter a start time."
        else:
            st = timeutil.parse_time(start_time)
        if _clean(end_time) and not TIME_RE.match(_clean(end_time)):
            errors["end_time"] = "Enter a valid end time."
        elif _clean(end_time):
            et = timeutil.parse_time(end_time)
        if start_d and end_d and st:
            start_dt = datetime.combine(start_d, st)
            # A blank end time means one hour after the start.
            end_dt = datetime.combine(end_d, et) if et else start_dt + timedelta(hours=1)
            if end_dt < start_dt:
                errors["end_time"] = "The end can't be before the start."
            else:
                start_local = start_dt.strftime(timeutil.DT_FMT)
                end_local = end_dt.strftime(timeutil.DT_FMT)
    return start_local, end_local, start_d


def _validate_repeat(p: recurrence.Pattern, start_d: date | None, errors: dict[str, str]) -> tuple[str | None, str | None]:
    """Check a repeat pattern. Returns (rrule, recurrence_end)."""
    if p.repeat == "none" or start_d is None:
        return None, None
    rule = recurrence.build_rrule(p, start_d, errors)
    until = None
    if _clean(p.until):
        try:
            until_d = timeutil.parse_date(p.until)
        except ValueError:
            errors["until"] = "Enter a valid end date for the repeats."
        else:
            if until_d < start_d:
                errors["until"] = "The repeats can't end before the first date."
            else:
                until = until_d.strftime(timeutil.DATE_FMT)
    return rule, until


def validate_event(conn: sqlite3.Connection, data: EventInput) -> dict:
    """Check an event form. Returns clean values or raises ValidationError."""
    errors: dict[str, str] = {}
    title = _clean(data.title)
    location = _clean(data.location)
    notes = _clean(data.notes)

    if not title:
        errors["title"] = "Enter a title."
    elif len(title) > MAX_TITLE:
        errors["title"] = f"Keep the title to {MAX_TITLE} characters or fewer."
    if len(location) > MAX_LOCATION:
        errors["location"] = f"Keep the location to {MAX_LOCATION} characters or fewer."
    if len(notes) > MAX_NOTES:
        errors["notes"] = f"Keep the notes to {MAX_NOTES} characters or fewer."

    # People: at least one, all real (D36).
    person_ids: list[int] = []
    for raw in data.person_ids:
        try:
            person_ids.append(int(raw))
        except (TypeError, ValueError):
            errors["people"] = "Choose who this is for."
    person_ids = sorted(set(person_ids))
    if not person_ids:
        errors["people"] = "Tick at least one person."
    else:
        known = {
            r[0]
            for r in conn.execute(
                f"SELECT id FROM people WHERE id IN ({','.join('?' * len(person_ids))})", person_ids
            )
        }
        if known != set(person_ids):
            errors["people"] = "One of the people no longer exists. Please choose again."

    # Category.
    category = None
    try:
        category = get_category(conn, int(data.category_id))
    except (TypeError, ValueError):
        pass
    if category is None:
        errors["category"] = "Choose a category."

    # Dates, times and repeats.
    start_local, end_local, start_d = _validate_when(
        data.all_day, data.start_date, data.start_time, data.end_date, data.end_time, errors
    )
    rrule, recurrence_end = _validate_repeat(data.pattern(), start_d, errors)

    # Reminder (D27): category default, none, or a custom number of minutes.
    mode = data.reminder_mode if data.reminder_mode in ("default", "none", "custom") else None
    reminder_minutes = None
    if mode is None:
        errors["reminder"] = "Choose a reminder option."
    elif mode == "custom":
        reminder_minutes = _parse_reminder(data.reminder_minutes, "reminder", errors, allow_blank=False)

    if errors:
        raise ValidationError(errors)
    return {
        "title": title,
        "location": location or None,
        "notes": notes or None,
        "all_day": 1 if data.all_day else 0,
        "start_local": start_local,
        "end_local": end_local,
        "category_id": category["id"],
        "person_ids": person_ids,
        "reminder_mode": mode,
        "reminder_minutes": reminder_minutes,
        "rrule": rrule,
        "recurrence_end": recurrence_end,
    }


def save_event(conn: sqlite3.Connection, data: EventInput, event_id: int | None = None) -> int:
    clean = validate_event(conn, data)
    now = _now_text()
    cols = (
        clean["title"], clean["location"], clean["notes"], clean["all_day"],
        clean["start_local"], clean["end_local"], clean["category_id"],
        clean["reminder_mode"], clean["reminder_minutes"], clean["rrule"], clean["recurrence_end"],
    )
    try:
        if event_id is None:
            cur = conn.execute(
                "INSERT INTO events (title, location, notes, all_day, start_local, end_local, category_id,"
                " reminder_mode, reminder_minutes, rrule, recurrence_end, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                cols + (now, now),
            )
            event_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE events SET title=?, location=?, notes=?, all_day=?, start_local=?, end_local=?,"
                " category_id=?, reminder_mode=?, reminder_minutes=?, rrule=?, recurrence_end=?, updated=?,"
                " locally_modified = CASE WHEN source = 'google' THEN 1 ELSE locally_modified END"
                " WHERE id=?",
                cols + (now, event_id),
            )
            conn.execute("DELETE FROM event_people WHERE event_id = ?", (event_id,))
            if clean["rrule"] is None:
                # No longer repeats: per-date skips and changes no longer mean anything.
                conn.execute("DELETE FROM event_exceptions WHERE event_id = ?", (event_id,))
        conn.executemany(
            "INSERT INTO event_people (event_id, person_id) VALUES (?, ?)",
            [(event_id, pid) for pid in clean["person_ids"]],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return event_id


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()


def _attach_people(conn: sqlite3.Connection, events: list[dict]) -> list[dict]:
    if not events:
        return events
    ids = sorted({e["id"] for e in events})
    rows = conn.execute(
        f"SELECT ep.event_id, p.id, p.name, p.color FROM event_people ep JOIN people p ON p.id = ep.person_id"
        f" WHERE ep.event_id IN ({','.join('?' * len(ids))}) ORDER BY p.sort_order, p.name",
        ids,
    ).fetchall()
    by_event: dict[int, list[dict]] = {i: [] for i in ids}
    for r in rows:
        by_event[r["event_id"]].append({"id": r["id"], "name": r["name"], "color": r["color"]})
    for e in events:
        e["people"] = list(by_event[e["id"]])
    return events


_EVENT_SELECT = (
    "SELECT e.*, c.name AS category_name, c.color AS category_color,"
    " c.default_reminder_minutes AS category_reminder FROM events e JOIN categories c ON c.id = e.category_id"
)


def get_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute(_EVENT_SELECT + " WHERE e.id = ?", (event_id,)).fetchone()
    if not row:
        return None
    return _attach_people(conn, [dict(row)])[0]


def get_exceptions(conn: sqlite3.Connection, event_id: int) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM event_exceptions WHERE event_id = ? ORDER BY occ_date", (event_id,))
    return {r["occ_date"]: dict(r) for r in rows}


def _span(event: dict) -> timedelta:
    return timeutil.parse_local(event["end_local"]) - timeutil.parse_local(event["start_local"])


def _fmt_local(dt: datetime, all_day: bool) -> str:
    return dt.strftime(timeutil.DATE_FMT if all_day else timeutil.DT_FMT)


def _occurrence(event: dict, start: datetime, exc: dict | None) -> dict:
    """One occurrence of a series, with any change applied."""
    occ = dict(event)
    occ["occ_date"] = start.date().strftime(timeutil.DATE_FMT)
    occ["recurring"] = True
    occ["changed"] = False
    occ["start_local"] = _fmt_local(start, event["all_day"])
    occ["end_local"] = _fmt_local(start + _span(event), event["all_day"])
    if exc and not exc["skipped"]:
        occ["changed"] = True
        for key in ("start_local", "end_local", "title", "location", "notes"):
            if exc[key] is not None or key in ("location", "notes"):
                occ[key] = exc[key]
    return occ


def _overlaps(item: dict, lo: date, hi: date) -> bool:
    start = timeutil.parse_local(item["start_local"]).date()
    end = timeutil.parse_local(item["end_local"]).date()
    return start <= hi and end >= lo


def expand_event(conn: sqlite3.Connection, event: dict, lo: date, hi: date) -> list[dict]:
    """Occurrences of one repeating event that overlap the days lo..hi (inclusive)."""
    span = _span(event)
    dtstart = timeutil.parse_local(event["start_local"])
    window_lo = datetime.combine(lo, datetime.min.time()) - span
    window_hi = datetime.combine(hi, datetime.max.time())
    excs = get_exceptions(conn, event["id"])
    out = []
    seen = set()
    for start in recurrence.occurrences(event["rrule"], dtstart, event["recurrence_end"], window_lo, window_hi):
        key = start.date().strftime(timeutil.DATE_FMT)
        seen.add(key)
        exc = excs.get(key)
        if exc and exc["skipped"]:
            continue
        occ = _occurrence(event, start, exc)
        if _overlaps(occ, lo, hi):
            out.append(occ)
    # A changed occurrence may have moved into the window from outside it.
    for key, exc in excs.items():
        if key in seen or exc["skipped"] or not exc["start_local"]:
            continue
        original = recurrence.is_occurrence(
            event["rrule"], dtstart, event["recurrence_end"], timeutil.parse_date(key)
        )
        if original:
            occ = _occurrence(event, original, exc)
            if _overlaps(occ, lo, hi):
                out.append(occ)
    return out


def _sort_key(e: dict):
    return (e["start_local"][:10], -e["all_day"], e["start_local"], e["title"])


def list_events(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    """Events and repeat occurrences that overlap the days start..end (inclusive).

    Ordered by start day; within a day, all-day events first, then by time.
    Each occurrence of a repeating event is its own item, with `recurring`,
    `occ_date` (the date it originally fell on) and `changed` set.
    """
    lo = start.strftime(timeutil.DATE_FMT)
    hi_exclusive = (end + timedelta(days=1)).strftime(timeutil.DATE_FMT)
    single = [
        dict(r, recurring=False, changed=False, occ_date=None)
        for r in conn.execute(
            _EVENT_SELECT + " WHERE e.rrule IS NULL AND e.start_local < ? AND substr(e.end_local, 1, 10) >= ?",
            (hi_exclusive, lo),
        )
    ]
    series = [
        dict(r)
        for r in conn.execute(
            _EVENT_SELECT + " WHERE e.rrule IS NOT NULL AND e.start_local < ?"
            " AND (e.recurrence_end IS NULL OR e.recurrence_end >= ?)",
            (hi_exclusive, lo),
        )
    ]
    items = single
    for ev in series:
        items.extend(expand_event(conn, ev, start, end))
    items.sort(key=_sort_key)
    # People are the same for every occurrence; attach once per event id.
    _attach_people(conn, items)
    return items


def get_occurrence(conn: sqlite3.Connection, event_id: int, occ_date: str) -> dict | None:
    """One occurrence of a repeating event by its original date (skipped ones included, marked)."""
    event = get_event(conn, event_id)
    if not event or not event["rrule"]:
        return None
    try:
        day = timeutil.parse_date(occ_date)
    except ValueError:
        return None
    start = recurrence.is_occurrence(
        event["rrule"], timeutil.parse_local(event["start_local"]), event["recurrence_end"], day
    )
    if not start:
        return None
    exc = get_exceptions(conn, event_id).get(occ_date)
    occ = _occurrence(event, start, exc)
    occ["skipped"] = bool(exc and exc["skipped"])
    occ["original_start_local"] = _fmt_local(start, event["all_day"])
    return occ


def skip_occurrence(conn: sqlite3.Connection, event_id: int, occ_date: str) -> None:
    conn.execute(
        "INSERT INTO event_exceptions (event_id, occ_date, skipped, updated) VALUES (?, ?, 1, ?)"
        " ON CONFLICT(event_id, occ_date) DO UPDATE SET skipped = 1, start_local = NULL, end_local = NULL,"
        " title = NULL, location = NULL, notes = NULL, updated = excluded.updated",
        (event_id, occ_date, _now_text()),
    )
    conn.commit()


def restore_occurrence(conn: sqlite3.Connection, event_id: int, occ_date: str) -> None:
    """Undo a skip or a change: the occurrence follows the series again."""
    conn.execute("DELETE FROM event_exceptions WHERE event_id = ? AND occ_date = ?", (event_id, occ_date))
    conn.commit()


@dataclass
class OccurrenceInput:
    """Form values for changing one occurrence."""

    title: str = ""
    location: str = ""
    notes: str = ""
    start_date: str = ""
    start_time: str = ""
    end_date: str = ""
    end_time: str = ""


def change_occurrence(conn: sqlite3.Connection, event: dict, occ_date: str, data: OccurrenceInput) -> None:
    errors: dict[str, str] = {}
    title, location, notes = _clean(data.title), _clean(data.location), _clean(data.notes)
    if not title:
        errors["title"] = "Enter a title."
    elif len(title) > MAX_TITLE:
        errors["title"] = f"Keep the title to {MAX_TITLE} characters or fewer."
    if len(location) > MAX_LOCATION:
        errors["location"] = f"Keep the location to {MAX_LOCATION} characters or fewer."
    if len(notes) > MAX_NOTES:
        errors["notes"] = f"Keep the notes to {MAX_NOTES} characters or fewer."
    start_local, end_local, _ = _validate_when(
        bool(event["all_day"]), data.start_date, data.start_time, data.end_date, data.end_time, errors
    )
    if errors:
        raise ValidationError(errors)
    conn.execute(
        "INSERT INTO event_exceptions (event_id, occ_date, skipped, start_local, end_local, title, location,"
        " notes, updated) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(event_id, occ_date) DO UPDATE SET skipped = 0, start_local = excluded.start_local,"
        " end_local = excluded.end_local, title = excluded.title, location = excluded.location,"
        " notes = excluded.notes, updated = excluded.updated",
        (event["id"], occ_date, start_local, end_local, title, location or None, notes or None, _now_text()),
    )
    conn.commit()


def occurrence_to_input(occ: dict) -> OccurrenceInput:
    start = timeutil.parse_local(occ["start_local"])
    end = timeutil.parse_local(occ["end_local"])
    return OccurrenceInput(
        title=occ["title"], location=occ["location"] or "", notes=occ["notes"] or "",
        start_date=start.strftime(timeutil.DATE_FMT),
        start_time="" if occ["all_day"] else start.strftime("%H:%M"),
        end_date=end.strftime(timeutil.DATE_FMT),
        end_time="" if occ["all_day"] else end.strftime("%H:%M"),
    )


def effective_reminder_minutes(event: dict) -> int | None:
    """Minutes before the start that the banner should appear, or None (D27).

    All-day events never get a timed reminder.
    """
    if event["all_day"]:
        return None
    if event["reminder_mode"] == "none":
        return None
    if event["reminder_mode"] == "custom":
        return event["reminder_minutes"]
    return event.get("category_reminder")


def event_to_input(event: dict) -> EventInput:
    """Turn a stored event back into form values for editing."""
    start = timeutil.parse_local(event["start_local"])
    end = timeutil.parse_local(event["end_local"])
    return EventInput(
        title=event["title"],
        location=event["location"] or "",
        notes=event["notes"] or "",
        all_day=bool(event["all_day"]),
        start_date=start.strftime(timeutil.DATE_FMT),
        start_time="" if event["all_day"] else start.strftime("%H:%M"),
        end_date=end.strftime(timeutil.DATE_FMT),
        end_time="" if event["all_day"] else end.strftime("%H:%M"),
        category_id=str(event["category_id"]),
        person_ids=[str(p["id"]) for p in event["people"]],
        reminder_mode=event["reminder_mode"],
        reminder_minutes="" if event["reminder_minutes"] is None else str(event["reminder_minutes"]),
        **_pattern_fields(recurrence.pattern_from_rrule(event["rrule"], event["recurrence_end"], start.date())),
    )


def _pattern_fields(p: recurrence.Pattern) -> dict:
    return {
        "repeat": p.repeat, "interval": p.interval, "weekdays": p.weekdays, "ordinals": p.ordinals,
        "nth_weekday": p.nth_weekday, "until": p.until, "custom_rrule": p.custom_rrule,
    }


def repeat_defaults(start_date: str) -> dict:
    """Pre-filled pattern choices for a new event or reminder starting on start_date."""
    try:
        d = timeutil.parse_date(start_date)
    except ValueError:
        d = timeutil.now_local().date()
    return _pattern_fields(recurrence.pattern_from_rrule(None, None, d))


# ------------------------------------------------- household reminders (D37)


@dataclass
class ReminderInput:
    """Raw form values for a household "I did it" reminder."""

    text: str = ""
    start_date: str = ""
    appear_time: str = ""      # blank = end of dark hours
    carry_over: bool = False
    active: bool = True
    repeat: str = "weekly"
    interval: str = "1"
    weekdays: list[str] = field(default_factory=list)
    ordinals: list[str] = field(default_factory=list)
    nth_weekday: str = ""
    until: str = ""
    custom_rrule: str = ""

    def pattern(self) -> recurrence.Pattern:
        return recurrence.Pattern(
            repeat=self.repeat, interval=self.interval, weekdays=list(self.weekdays),
            ordinals=list(self.ordinals), nth_weekday=self.nth_weekday, until=self.until,
            custom_rrule=self.custom_rrule,
        )


def save_reminder(conn: sqlite3.Connection, data: ReminderInput, reminder_id: int | None = None) -> int:
    errors: dict[str, str] = {}
    text = _clean(data.text)
    if not text:
        errors["text"] = "Enter the reminder."
    elif len(text) > MAX_REMINDER_TEXT:
        errors["text"] = f"Keep it to one short line ({MAX_REMINDER_TEXT} characters or fewer)."
    try:
        start_d = timeutil.parse_date(data.start_date)
    except ValueError:
        start_d = None
        errors["start_date"] = "Enter the first date."
    appear = _clean(data.appear_time)
    if appear and not TIME_RE.match(appear):
        errors["appear_time"] = "Enter a time such as 07:00, or leave it blank."
    if data.repeat == "none":
        errors["repeat"] = "Choose how often this reminder comes round."
        rule, until = None, None
    else:
        rule, until = _validate_repeat(data.pattern(), start_d, errors)
    if errors:
        raise ValidationError(errors)
    now = _now_text()
    values = (text, rule, start_d.strftime(timeutil.DATE_FMT), until, appear or None,
              1 if data.carry_over else 0, 1 if data.active else 0)
    if reminder_id is None:
        cur = conn.execute(
            "INSERT INTO household_reminders (text, rrule, start_date, recurrence_end, appear_time, carry_over,"
            " active, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
            values + (now, now),
        )
        conn.commit()
        return cur.lastrowid
    conn.execute(
        "UPDATE household_reminders SET text=?, rrule=?, start_date=?, recurrence_end=?, appear_time=?,"
        " carry_over=?, active=?, updated=? WHERE id=?",
        values + (now, reminder_id),
    )
    conn.commit()
    return reminder_id


def get_reminder(conn: sqlite3.Connection, reminder_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM household_reminders WHERE id = ?", (reminder_id,)).fetchone()
    return dict(row) if row else None


def delete_reminder(conn: sqlite3.Connection, reminder_id: int) -> None:
    conn.execute("DELETE FROM household_reminders WHERE id = ?", (reminder_id,))
    conn.commit()


def reminder_to_input(rem: dict) -> ReminderInput:
    start = timeutil.parse_date(rem["start_date"])
    return ReminderInput(
        text=rem["text"], start_date=rem["start_date"], appear_time=rem["appear_time"] or "",
        carry_over=bool(rem["carry_over"]), active=bool(rem["active"]),
        **_pattern_fields(recurrence.pattern_from_rrule(rem["rrule"], rem["recurrence_end"], start)),
    )


def _reminder_dtstart(rem: dict, dark_end: str) -> datetime:
    t = timeutil.parse_time(rem["appear_time"] or dark_end)
    return datetime.combine(timeutil.parse_date(rem["start_date"]), t)


def reminder_status(rem: dict, now: datetime, dark_end: str) -> dict:
    """Is the reminder showing at `now`? Adds `showing`, `due_occ` (date text), `next_at`.

    Showing means: its latest occurrence has reached its appear time, nobody has
    ticked that occurrence (or a later one), and either carry-over is on or the
    occurrence is today. Only one item ever shows per reminder, so occurrences
    never stack.
    """
    status = dict(rem, showing=False, due_occ=None, next_at=None)
    status["description"] = recurrence.describe(
        rem["rrule"], timeutil.parse_date(rem["start_date"]), rem["recurrence_end"]
    )
    dtstart = _reminder_dtstart(rem, dark_end)
    status["appears"] = timeutil.fmt_time(dtstart)
    nxt = recurrence.next_after(rem["rrule"], dtstart, rem["recurrence_end"], now)
    status["next_at"] = nxt
    if not rem["active"]:
        return status
    latest = recurrence.latest_on_or_before(rem["rrule"], dtstart, rem["recurrence_end"], now)
    if latest is None:
        return status
    occ = latest.date().strftime(timeutil.DATE_FMT)
    done = rem["last_done_occ"] is not None and rem["last_done_occ"] >= occ
    if done:
        return status
    if rem["carry_over"] or latest.date() == now.date():
        status["showing"] = True
        status["due_occ"] = occ
    return status


def list_reminders(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    now = now or timeutil.now_local()
    dark_end = get_config(conn)["dark_end"]
    rows = conn.execute("SELECT * FROM household_reminders ORDER BY active DESC, text COLLATE NOCASE")
    return [reminder_status(dict(r), now, dark_end) for r in rows]


def reminders_showing(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """The reminders that should be on the display right now."""
    return [r for r in list_reminders(conn, now) if r["showing"]]


def mark_reminder_done(conn: sqlite3.Connection, reminder_id: int, now: datetime | None = None) -> bool:
    """"I did it": tick the occurrence that is showing. Returns False if nothing was showing."""
    now = now or timeutil.now_local()
    rem = get_reminder(conn, reminder_id)
    if not rem:
        return False
    status = reminder_status(rem, now, get_config(conn)["dark_end"])
    if not status["showing"]:
        return False
    conn.execute(
        "UPDATE household_reminders SET prev_done_occ = last_done_occ, prev_done_at = last_done_at,"
        " last_done_occ = ?, last_done_at = ? WHERE id = ?",
        (status["due_occ"], now.strftime(timeutil.DT_FMT), reminder_id),
    )
    conn.commit()
    return True


def undo_reminder_done(conn: sqlite3.Connection, reminder_id: int) -> bool:
    """Put back the "last done" values from before the most recent "I did it"."""
    rem = get_reminder(conn, reminder_id)
    if not rem or rem["last_done_at"] is None:
        return False
    conn.execute(
        "UPDATE household_reminders SET last_done_occ = prev_done_occ, last_done_at = prev_done_at,"
        " prev_done_occ = NULL, prev_done_at = NULL WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()
    return True


# ------------------------------------------------------------------- settings


def get_config(conn: sqlite3.Connection) -> dict[str, str]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config")})
    return cfg


def save_config(conn: sqlite3.Connection, form: dict[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}

    def int_field(key: str, lo: int, hi: int, label: str) -> None:
        raw = _clean(form.get(key))
        try:
            value = int(raw)
        except ValueError:
            errors[key] = f"Enter {label} as a whole number."
            return
        if not lo <= value <= hi:
            errors[key] = f"Use a value from {lo} to {hi}."
            return
        clean[key] = str(value)

    int_field("upcoming_days", 1, 60, "the number of days")
    int_field("stale_minutes", 5, 1440, "the number of minutes")
    int_field("wake_seconds", 10, 900, "the number of seconds")
    for key in ("dark_start", "dark_end"):
        raw = _clean(form.get(key))
        if TIME_RE.match(raw):
            clean[key] = raw
        else:
            errors[key] = "Enter a time such as 23:00."
    if errors:
        raise ValidationError(errors)
    conn.executemany(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(clean.items()),
    )
    conn.commit()
    return clean
