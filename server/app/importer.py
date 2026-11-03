"""Google import: comparing a fetched calendar with this one, and Dan's choices (Phase 5a).

The rules (decisions D5, D39, D54–D58):
- Nothing changes on its own. A fetch only produces review items; each
  change to the calendar happens when Dan chooses it on the review page.
- A Google event is recognised by its UID. An imported or linked event keeps
  a snapshot of Google's version as last accepted, so a later fetch can tell
  "changed in Google" (Google differs from the snapshot) from "edited here"
  (this calendar differs from the snapshot).
- People, category and reminder are this calendar's own and are never touched
  by an import.
- The calendar's private address comes from the Portainer variable
  CAL_SOURCE_<slot>_URL and is never stored, shown or logged.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import ical_import, recurrence, store, timeutil

MAX_SLOTS = 5
KINDS = ("new", "duplicate", "changed", "conflict", "removed", "same_uid")
FIELDS = ("title", "location", "notes", "all_day", "start_local", "end_local", "rrule", "recurrence_end")
FIELD_NAMES = {"title": "Title", "location": "Location", "notes": "Notes", "all_day": "All day",
               "start_local": "Start", "end_local": "End", "rrule": "Repeats", "recurrence_end": "Repeats until",
               "exdates": "Skipped dates", "overrides": "Changed dates"}


class ImportError_(Exception):
    """An import action that can't be done, with a plain-language reason."""


def _now() -> str:
    return timeutil.now_local().strftime("%Y-%m-%dT%H:%M:%S")


# ----------------------------------------------------------------- sources


def source_url(slot: int) -> str:
    return (os.environ.get(f"CAL_SOURCE_{slot}_URL") or "").strip()


def configured_slots() -> list[int]:
    return [n for n in range(1, MAX_SLOTS + 1) if source_url(n)]


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    """Every configured source (creating its settings row on first sight), plus rows for
    sources whose variable has since been removed (marked not configured)."""
    slots = configured_slots()
    for n in slots:
        conn.execute("INSERT OR IGNORE INTO import_sources (slot, name) VALUES (?, ?)", (n, f"Google calendar {n}"))
    conn.commit()
    out = []
    for r in conn.execute("SELECT * FROM import_sources ORDER BY slot"):
        s = dict(r)
        s["configured"] = s["slot"] in slots
        s["people_ids"] = json.loads(s["default_people"] or "[]")
        s["pending"] = conn.execute("SELECT COUNT(*) FROM import_items WHERE slot = ?", (s["slot"],)).fetchone()[0]
        s["skipped"] = conn.execute("SELECT COUNT(*) FROM import_skips WHERE slot = ?", (s["slot"],)).fetchone()[0]
        out.append(s)
    return out


def get_source(conn: sqlite3.Connection, slot: int) -> dict | None:
    return next((s for s in list_sources(conn) if s["slot"] == slot), None)


def save_source(conn: sqlite3.Connection, slot: int, name: str, person_ids: list, category_id) -> None:
    errors = {}
    name = (name or "").strip()
    if not name:
        errors["name"] = "Give the source a name."
    elif len(name) > 60:
        errors["name"] = "Keep the name to 60 characters or fewer."
    people = []
    for p in person_ids or []:
        try:
            pid = int(p)
        except (TypeError, ValueError):
            continue
        if store.get_person(conn, pid):
            people.append(pid)
    cat = None
    if category_id not in (None, ""):
        try:
            cat = int(category_id)
        except (TypeError, ValueError):
            cat = -1
        if store.get_category(conn, cat) is None:
            errors["category"] = "That category no longer exists."
    if errors:
        raise store.ValidationError(errors)
    conn.execute("INSERT OR IGNORE INTO import_sources (slot, name) VALUES (?, ?)", (slot, name))
    conn.execute("UPDATE import_sources SET name = ?, default_people = ?, default_category_id = ? WHERE slot = ?",
                 (name, json.dumps(sorted(set(people))), cat, slot))
    conn.commit()


# --------------------------------------------------------------- versions


def _norm(v: dict) -> dict:
    """A comparable version of an event: Google's, a snapshot, or this calendar's."""
    return {
        "title": (v.get("title") or "").strip(),
        "location": (v.get("location") or "").strip(),
        "notes": (v.get("notes") or "").strip(),
        "all_day": bool(v.get("all_day")),
        "start_local": v.get("start_local"),
        "end_local": v.get("end_local"),
        "rrule": v.get("rrule") or None,
        "recurrence_end": v.get("recurrence_end") or None,
        "exdates": sorted(v.get("exdates") or []) if v.get("rrule") else [],
        "overrides": {k: {f: (o.get(f) or "").strip() if f in ("title", "location", "notes") else o.get(f)
                          for f in ("title", "location", "notes", "start_local", "end_local")}
                      for k, o in sorted((v.get("overrides") or {}).items())} if v.get("rrule") else {},
    }


def local_version(conn: sqlite3.Connection, event: dict) -> dict:
    v = {k: event[k] for k in FIELDS}
    excs = store.get_exceptions(conn, event["id"])
    v["exdates"] = [d for d, x in excs.items() if x["skipped"]]
    v["overrides"] = {d: {"title": x["title"] or event["title"], "location": x["location"] or "",
                          "notes": x["notes"] or "", "start_local": x["start_local"], "end_local": x["end_local"]}
                      for d, x in excs.items() if not x["skipped"] and x["start_local"]}
    return _norm(v)


def differences(a: dict, b: dict) -> list[str]:
    """Names of the fields that differ between two versions."""
    a, b = _norm(a), _norm(b)
    return [FIELD_NAMES[k] for k in a if a[k] != b[k]]


# ------------------------------------------------------------- matching


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def similar(a: str, b: str) -> bool:
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if min(len(ka), len(kb)) >= 4 and (ka in kb or kb in ka):
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.8


def first_date(g: dict, today: date) -> tuple[datetime, datetime] | None:
    """Start and end of the event's first date on or after today (or its only date)."""
    start = timeutil.parse_local(g["start_local"])
    end = timeutil.parse_local(g["end_local"])
    if not g["rrule"]:
        return start, end
    span = end - start
    lo = datetime.combine(today, datetime.min.time()) - span
    hi = datetime.combine(today + timedelta(days=ical_import.WINDOW_DAYS), datetime.max.time())
    for occ in recurrence.occurrences(g["rrule"], start, g["recurrence_end"], lo, hi):
        key = occ.date().strftime(timeutil.DATE_FMT)
        if key in g["exdates"]:
            continue
        o = g["overrides"].get(key)
        if o:
            return timeutil.parse_local(o["start_local"]), timeutil.parse_local(o["end_local"])
        return occ, occ + span
    return None


def _same_time(g: dict, gs: datetime, ge: datetime, e: dict) -> bool:
    es, ee = timeutil.parse_local(e["start_local"]), timeutil.parse_local(e["end_local"])
    if g["all_day"] or e["all_day"]:
        return bool(g["all_day"]) == bool(e["all_day"]) and gs.date() <= ee.date() and es.date() <= ge.date()
    if gs == es:
        return True
    return gs < ee and es < ge


def find_duplicate(conn: sqlite3.Connection, g: dict, today: date) -> dict | None:
    """A local event (not yet linked to Google) that looks like the same thing (D56)."""
    when = first_date(g, today)
    if when is None:
        return None
    gs, ge = when
    best, best_score = None, 0.0
    for e in store.list_events(conn, gs.date(), max(ge.date(), gs.date())):
        if e.get("google_uid") or not _same_time(g, gs, ge, e) or not similar(g["title"], e["title"]):
            continue
        score = difflib.SequenceMatcher(None, _key(g["title"]), _key(e["title"])).ratio()
        if score > best_score:
            best, best_score = e, score
    return best


def overlaps(conn: sqlite3.Connection, g: dict, today: date) -> list[str]:
    """Timed local events that overlap the Google event's first date (a notice only)."""
    when = first_date(g, today)
    if when is None or g["all_day"]:
        return []
    gs, ge = when
    out = []
    for e in store.list_events(conn, gs.date(), gs.date()):
        if e["all_day"]:
            continue
        es, ee = timeutil.parse_local(e["start_local"]), timeutil.parse_local(e["end_local"])
        if gs < ee and es < ge and e.get("google_uid") != g["uid"]:
            out.append(f"{e['title']} ({timeutil.fmt_time(es)})")
    return out


# --------------------------------------------------------------- fetching


def _add_item(conn, slot, uid, kind, data, event_id=None):
    conn.execute("INSERT OR REPLACE INTO import_items (slot, uid, kind, event_id, data, created) VALUES (?,?,?,?,?,?)",
                 (slot, uid, kind, event_id, json.dumps(data), _now()))


def fetch_source(conn: sqlite3.Connection, slot: int, today: date | None = None, fetcher=None) -> dict:
    """Read one Google calendar and rebuild its review items. Changes nothing else
    except bookkeeping (when each linked event was last seen). Returns counts."""
    today = today or timeutil.now_local().date()
    url = source_url(slot)
    fetcher = fetcher or ical_import.fetch
    list_sources(conn)   # make sure the source row exists
    try:
        parsed = ical_import.parse(fetcher(url), today)
    except ical_import.FetchError as exc:
        conn.execute("UPDATE import_sources SET last_fetch = ?, last_result = ? WHERE slot = ?",
                     (_now(), f"Couldn't read it: {exc}", slot))
        conn.commit()
        raise
    try:
        conn.execute("DELETE FROM import_items WHERE slot = ?", (slot,))
        skips = {r[0] for r in conn.execute("SELECT uid FROM import_skips WHERE slot = ?", (slot,))}
        linked = {}
        others = {}
        for r in conn.execute("SELECT id, google_uid, google_source FROM events WHERE google_uid IS NOT NULL"):
            (linked if r["google_source"] == str(slot) else others)[r["google_uid"]] = r["id"]
        counts = dict.fromkeys(KINDS, 0)
        seen = []
        for g in parsed["events"]:
            uid = g["uid"]
            if uid in skips:
                continue
            if uid in linked:
                ev = store.get_event(conn, linked[uid])
                seen.append(ev["id"])
                snap = json.loads(ev["google_snapshot"] or "{}")
                gv = _norm(g)
                if gv == snap:
                    continue
                if gv == local_version(conn, ev):
                    # Google now matches what's here: nothing to decide, just remember it.
                    conn.execute("UPDATE events SET google_snapshot = ? WHERE id = ?", (json.dumps(gv), ev["id"]))
                    continue
                kind = "conflict" if local_version(conn, ev) != snap else "changed"
                _add_item(conn, slot, uid, kind, g, ev["id"])
            elif uid in others:
                _add_item(conn, slot, uid, "same_uid", g, others[uid])
            else:
                dup = find_duplicate(conn, g, today)
                _add_item(conn, slot, uid, "duplicate" if dup else "new", g, dup["id"] if dup else None)
            counts[conn.execute("SELECT kind FROM import_items WHERE slot = ? AND uid = ?",
                                (slot, uid)).fetchone()[0]] += 1
        # Previously imported events that are no longer in Google at all, and still current here.
        current = None
        for uid, eid in linked.items():
            if uid in parsed["uids"]:
                continue
            if current is None:
                current = {e["id"] for e in store.list_events(
                    conn, today, today + timedelta(days=ical_import.WINDOW_DAYS))}
            ev = store.get_event(conn, eid)
            if ev and eid in current:
                _add_item(conn, slot, uid, "removed", {}, eid)
                counts["removed"] += 1
        if seen:
            conn.execute(f"UPDATE events SET google_last_seen = ? WHERE id IN ({','.join('?' * len(seen))})",
                         [_now()] + seen)
        total = sum(counts.values())
        summary = (f"{len(parsed['events'])} events in the next 12 months; "
                   + (f"{total} to review." if total else "nothing new to review."))
        conn.execute("UPDATE import_sources SET last_fetch = ?, last_result = ? WHERE slot = ?",
                     (_now(), summary, slot))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    counts["events"] = len(parsed["events"])
    return counts


# ----------------------------------------------------------- review items


def list_items(conn: sqlite3.Connection, slot: int | None = None) -> list[dict]:
    sql = "SELECT * FROM import_items" + (" WHERE slot = ?" if slot else "") + " ORDER BY id"
    out = []
    for r in conn.execute(sql, (slot,) if slot else ()):
        it = dict(r)
        it["g"] = json.loads(it["data"])
        out.append(it)
    out.sort(key=lambda it: (KINDS.index(it["kind"]), it["g"].get("start_local") or "", it["id"]))
    return out


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()
    if not r:
        return None
    it = dict(r)
    it["g"] = json.loads(it["data"])
    return it


def _drop(conn, item_id):
    conn.execute("DELETE FROM import_items WHERE id = ?", (item_id,))


def _write_exceptions(conn, event_id: int, g: dict) -> None:
    conn.execute("DELETE FROM event_exceptions WHERE event_id = ?", (event_id,))
    if not g.get("rrule"):
        return
    now = _now()
    for d in g["exdates"]:
        conn.execute("INSERT OR REPLACE INTO event_exceptions (event_id, occ_date, skipped, updated) VALUES (?, ?, 1, ?)",
                     (event_id, d, now))
    for d, o in g["overrides"].items():
        conn.execute(
            "INSERT OR REPLACE INTO event_exceptions (event_id, occ_date, skipped, start_local, end_local, title,"
            " location, notes, updated) VALUES (?,?,0,?,?,?,?,?,?)",
            (event_id, d, o["start_local"], o["end_local"], o["title"], o["location"] or None, o["notes"] or None, now),
        )


def _attach(conn, event_id: int, slot: int, g: dict, imported: bool) -> None:
    conn.execute(
        "UPDATE events SET google_source = ?, google_uid = ?, google_last_seen = ?, google_snapshot = ?,"
        " locally_modified = 0" + (", source = 'google'" if imported else "") + " WHERE id = ?",
        (str(slot), g["uid"], _now(), json.dumps(_norm(g)), event_id),
    )
    conn.execute("DELETE FROM import_skips WHERE slot = ? AND uid = ?", (slot, g["uid"]))


def _create(conn, slot: int, g: dict, people: list[int], category_id: int) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO events (title, location, notes, all_day, start_local, end_local, category_id, reminder_mode,"
        " rrule, recurrence_end, created, updated) VALUES (?,?,?,?,?,?,?,'default',?,?,?,?)",
        (g["title"], g["location"] or None, g["notes"] or None, 1 if g["all_day"] else 0, g["start_local"],
         g["end_local"], category_id, g["rrule"], g["recurrence_end"], now, now),
    )
    eid = cur.lastrowid
    conn.executemany("INSERT INTO event_people (event_id, person_id) VALUES (?, ?)", [(eid, p) for p in people])
    _write_exceptions(conn, eid, g)
    _attach(conn, eid, slot, g, imported=True)
    return eid


def add_with_defaults(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Add the ticked new events using each source's default people and category (D55)."""
    added = 0
    try:
        for iid in item_ids:
            it = get_item(conn, iid)
            if not it or it["kind"] != "new":
                continue
            src = get_source(conn, it["slot"])
            people = [p for p in (src["people_ids"] if src else []) if store.get_person(conn, p)]
            if not people or not (src and src["default_category_id"]):
                raise ImportError_("Set this source's default people and category first (Import sources), "
                                   "or use “Review and add” for each event.")
            _create(conn, it["slot"], it["g"], people, src["default_category_id"])
            _drop(conn, iid)
            added += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return added


def form_for_item(conn: sqlite3.Connection, it: dict) -> store.EventInput:
    """The event form pre-filled from a Google event and the source's defaults (D34)."""
    g = it["g"]
    src = get_source(conn, it["slot"]) or {"people_ids": [], "default_category_id": None}
    start = timeutil.parse_local(g["start_local"])
    end = timeutil.parse_local(g["end_local"])
    p = recurrence.pattern_from_rrule(g["rrule"], g["recurrence_end"], start.date())
    return store.EventInput(
        title=g["title"], location=g["location"], notes=g["notes"], all_day=g["all_day"],
        start_date=start.strftime(timeutil.DATE_FMT), start_time="" if g["all_day"] else start.strftime("%H:%M"),
        end_date=end.strftime(timeutil.DATE_FMT), end_time="" if g["all_day"] else end.strftime("%H:%M"),
        category_id=str(src["default_category_id"] or ""), person_ids=[str(x) for x in src["people_ids"]],
        repeat=p.repeat, interval=p.interval, weekdays=p.weekdays, ordinals=p.ordinals,
        nth_weekday=p.nth_weekday, until=p.until, custom_rrule=p.custom_rrule,
    )


def add_reviewed(conn: sqlite3.Connection, item_id: int, form: store.EventInput) -> int:
    """Save the reviewed form, then tie the new event to its Google event."""
    it = get_item(conn, item_id)
    if not it or it["kind"] != "new":
        raise ImportError_("That item has already been dealt with.")
    eid = store.save_event(conn, form)
    try:
        ev = store.get_event(conn, eid)
        if ev["rrule"]:
            # Google's skipped and changed dates follow the series (any that no longer fit are ignored).
            g = dict(it["g"], overrides={k: v for k, v in it["g"]["overrides"].items()})
            for d in g["exdates"]:
                conn.execute("INSERT OR REPLACE INTO event_exceptions (event_id, occ_date, skipped, updated)"
                             " VALUES (?, ?, 1, ?)", (eid, d, _now()))
            for d, o in g["overrides"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO event_exceptions (event_id, occ_date, skipped, start_local, end_local,"
                    " title, location, notes, updated) VALUES (?,?,0,?,?,?,?,?,?)",
                    (eid, d, o["start_local"], o["end_local"], o["title"], o["location"] or None,
                     o["notes"] or None, _now()))
        _attach(conn, eid, it["slot"], it["g"], imported=True)
        _drop(conn, item_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return eid


def link(conn: sqlite3.Connection, item_id: int) -> None:
    """Keep the hand-entered event as it is and remember that it is this Google event (D56)."""
    it = get_item(conn, item_id)
    if not it or it["kind"] != "duplicate" or not store.get_event(conn, it["event_id"] or 0):
        raise ImportError_("That item has already been dealt with, or its event is gone.")
    _attach(conn, it["event_id"], it["slot"], it["g"], imported=False)
    _drop(conn, item_id)
    conn.commit()


def keep_both(conn: sqlite3.Connection, item_id: int) -> None:
    """Not a duplicate after all: offer it as a new event."""
    conn.execute("UPDATE import_items SET kind = 'new', event_id = NULL WHERE id = ? AND kind = 'duplicate'", (item_id,))
    conn.commit()


def skip(conn: sqlite3.Connection, item_id: int) -> None:
    """Don't import this Google event, now or on later fetches."""
    it = get_item(conn, item_id)
    if not it:
        return
    conn.execute("INSERT OR REPLACE INTO import_skips (slot, uid, title, created) VALUES (?, ?, ?, ?)",
                 (it["slot"], it["uid"], it["g"].get("title") or "", _now()))
    _drop(conn, item_id)
    conn.commit()


def _apply(conn, it: dict) -> None:
    g = it["g"]
    conn.execute(
        "UPDATE events SET title = ?, location = ?, notes = ?, all_day = ?, start_local = ?, end_local = ?,"
        " rrule = ?, recurrence_end = ?, updated = ? WHERE id = ?",
        (g["title"], g["location"] or None, g["notes"] or None, 1 if g["all_day"] else 0, g["start_local"],
         g["end_local"], g["rrule"], g["recurrence_end"], _now(), it["event_id"]),
    )
    _write_exceptions(conn, it["event_id"], g)
    conn.execute("UPDATE events SET google_snapshot = ?, google_last_seen = ? WHERE id = ?",
                 (json.dumps(_norm(g)), _now(), it["event_id"]))
    _drop(conn, it["id"])


def apply_change(conn: sqlite3.Connection, item_id: int) -> None:
    """Use Google's version of the title, times, place, notes and repeats (never people, category or reminder)."""
    it = get_item(conn, item_id)
    if not it or it["kind"] not in ("changed", "conflict") or not store.get_event(conn, it["event_id"] or 0):
        raise ImportError_("That item has already been dealt with, or its event is gone.")
    _apply(conn, it)
    conn.commit()


def apply_all(conn: sqlite3.Connection, slot: int | None = None) -> int:
    """Apply every change to events not edited here (D57). Conflicts are left for one-by-one review."""
    n = 0
    try:
        for it in list_items(conn, slot):
            if it["kind"] == "changed" and store.get_event(conn, it["event_id"] or 0):
                _apply(conn, it)
                n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def keep_mine(conn: sqlite3.Connection, item_id: int) -> None:
    """Keep this calendar's version; Google's current version becomes the new reference."""
    it = get_item(conn, item_id)
    if not it or it["kind"] not in ("changed", "conflict"):
        raise ImportError_("That item has already been dealt with.")
    conn.execute("UPDATE events SET google_snapshot = ? WHERE id = ?", (json.dumps(_norm(it["g"])), it["event_id"]))
    _drop(conn, item_id)
    conn.commit()


def removed_delete(conn: sqlite3.Connection, item_id: int) -> None:
    """Gone from Google: delete it here too (it can be restored from Recently deleted, D51)."""
    it = get_item(conn, item_id)
    if not it or it["kind"] != "removed":
        raise ImportError_("That item has already been dealt with.")
    if store.get_event(conn, it["event_id"] or 0):
        store.delete_event(conn, it["event_id"])
    _drop(conn, item_id)
    conn.commit()


def removed_keep(conn: sqlite3.Connection, item_id: int) -> None:
    """Gone from Google but wanted here: keep it as an ordinary event, no longer tied to Google."""
    it = get_item(conn, item_id)
    if not it or it["kind"] != "removed":
        raise ImportError_("That item has already been dealt with.")
    conn.execute("UPDATE events SET google_source = NULL, google_uid = NULL, google_snapshot = NULL,"
                 " source = 'local' WHERE id = ?", (it["event_id"],))
    _drop(conn, item_id)
    conn.commit()


def use_this_copy(conn: sqlite3.Connection, item_id: int) -> None:
    """The same Google event came from two sources: follow it from this one from now on (D39)."""
    it = get_item(conn, item_id)
    if not it or it["kind"] != "same_uid":
        raise ImportError_("That item has already been dealt with.")
    old = conn.execute("SELECT google_source FROM events WHERE id = ?", (it["event_id"],)).fetchone()
    if old and old[0]:
        conn.execute("INSERT OR REPLACE INTO import_skips (slot, uid, title, created) VALUES (?, ?, ?, ?)",
                     (int(old[0]), it["uid"], it["g"]["title"], _now()))
    _attach(conn, it["event_id"], it["slot"], it["g"], imported=False)
    _drop(conn, item_id)
    conn.commit()


def list_skips(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM import_skips ORDER BY created DESC")]


def unskip(conn: sqlite3.Connection, slot: int, uid: str) -> None:
    conn.execute("DELETE FROM import_skips WHERE slot = ? AND uid = ?", (slot, uid))
    conn.commit()


# ---------------------------------------------------------------- labels


def when_label(g: dict, today: date | None = None) -> str:
    """The event's next date (for a series, its first date from today on), time and repeat."""
    start = timeutil.parse_local(g["start_local"])
    end = timeutil.parse_local(g["end_local"])
    nxt = first_date(g, today or timeutil.now_local().date()) if g.get("rrule") else None
    series_start = start
    if nxt:
        start, end = nxt
    day = f"{start.strftime('%a, %b')} {start.day}, {start.year}"
    if g["all_day"]:
        text = f"{day} · All day"
        if end.date() != start.date():
            text += f" until {end.strftime('%a, %b')} {end.day}"
    else:
        text = f"{day} · {timeutil.fmt_time(start)} – {timeutil.fmt_time(end)}"
    if g.get("rrule"):
        text = ("Next: " if nxt else "") + text
        text += " · " + recurrence.describe(g["rrule"], series_start.date(), g.get("recurrence_end"))
    return text
