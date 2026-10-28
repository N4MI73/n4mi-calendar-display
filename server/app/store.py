"""Reading and writing calendar data: people, categories, events and settings.

Validation lives here too, so the editor and (later) the display API share
the same rules. Functions take an open connection and return plain dicts.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import timeutil

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_TITLE = 120
MAX_LOCATION = 200
MAX_NOTES = 2000
MAX_NAME = 40
MAX_REMINDER = 7 * 24 * 60  # one week, in minutes

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

    # Dates and times.
    start_local = end_local = None
    try:
        start_d = timeutil.parse_date(data.start_date)
    except ValueError:
        start_d = None
        errors["start_date"] = "Enter a start date."
    try:
        end_d = timeutil.parse_date(data.end_date) if _clean(data.end_date) else start_d
    except ValueError:
        end_d = None
        errors["end_date"] = "Enter a valid end date."

    if data.all_day:
        if start_d and end_d:
            if end_d < start_d:
                errors["end_date"] = "The end date can't be before the start date."
            else:
                start_local = start_d.strftime(timeutil.DATE_FMT)
                end_local = end_d.strftime(timeutil.DATE_FMT)
    else:
        st = et = None
        if not TIME_RE.match(_clean(data.start_time)):
            errors["start_time"] = "Enter a start time."
        else:
            st = timeutil.parse_time(data.start_time)
        if _clean(data.end_time) and not TIME_RE.match(_clean(data.end_time)):
            errors["end_time"] = "Enter a valid end time."
        elif _clean(data.end_time):
            et = timeutil.parse_time(data.end_time)
        if start_d and end_d and st:
            start_dt = datetime.combine(start_d, st)
            # A blank end time means one hour after the start.
            end_dt = datetime.combine(end_d, et) if et else start_dt + timedelta(hours=1)
            if end_dt < start_dt:
                errors["end_time"] = "The end can't be before the start."
            else:
                start_local = start_dt.strftime(timeutil.DT_FMT)
                end_local = end_dt.strftime(timeutil.DT_FMT)

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
    }


def save_event(conn: sqlite3.Connection, data: EventInput, event_id: int | None = None) -> int:
    clean = validate_event(conn, data)
    now = _now_text()
    cols = (
        clean["title"], clean["location"], clean["notes"], clean["all_day"],
        clean["start_local"], clean["end_local"], clean["category_id"],
        clean["reminder_mode"], clean["reminder_minutes"],
    )
    try:
        if event_id is None:
            cur = conn.execute(
                "INSERT INTO events (title, location, notes, all_day, start_local, end_local, category_id,"
                " reminder_mode, reminder_minutes, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                cols + (now, now),
            )
            event_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE events SET title=?, location=?, notes=?, all_day=?, start_local=?, end_local=?,"
                " category_id=?, reminder_mode=?, reminder_minutes=?, updated=?,"
                " locally_modified = CASE WHEN source = 'google' THEN 1 ELSE locally_modified END"
                " WHERE id=?",
                cols + (now, event_id),
            )
            conn.execute("DELETE FROM event_people WHERE event_id = ?", (event_id,))
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
    ids = [e["id"] for e in events]
    rows = conn.execute(
        f"SELECT ep.event_id, p.id, p.name, p.color FROM event_people ep JOIN people p ON p.id = ep.person_id"
        f" WHERE ep.event_id IN ({','.join('?' * len(ids))}) ORDER BY p.sort_order, p.name",
        ids,
    ).fetchall()
    by_event: dict[int, list[dict]] = {i: [] for i in ids}
    for r in rows:
        by_event[r["event_id"]].append({"id": r["id"], "name": r["name"], "color": r["color"]})
    for e in events:
        e["people"] = by_event[e["id"]]
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


def list_events(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    """Events that overlap the days start..end (inclusive).

    Ordered by start day; within a day, all-day events first, then by time.
    """
    lo = start.strftime(timeutil.DATE_FMT)
    hi_exclusive = (end + timedelta(days=1)).strftime(timeutil.DATE_FMT)
    rows = conn.execute(
        _EVENT_SELECT + " WHERE e.start_local < ? AND substr(e.end_local, 1, 10) >= ?"
        " ORDER BY substr(e.start_local, 1, 10), e.all_day DESC, e.start_local, e.title",
        (hi_exclusive, lo),
    ).fetchall()
    return _attach_people(conn, [dict(r) for r in rows])


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
    )


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
