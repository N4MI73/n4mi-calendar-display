"""The shelf display's health (Phase 6a, decision D61).

A small helper on the Pi reports its readings every 30 seconds (report()); the
server adds what only it knows (when the display last fetched the schedule).
evaluate() turns those into plain-word rows, each green ("ok"), amber
("check") or red ("alert"), and the worst row sets the health button. Until
the Pi has reported at least once, the button says "Not set up" ("none")
rather than raising a false alarm.

Restart and Shut down from the health panel are queued here (request_command)
and handed to the helper with its next report (take_command). At most one
request waits, and it expires after COMMAND_TTL_MINUTES, so a request made
while the Pi is off doesn't fire hours later.

Nothing here is secret, and nothing is written outside the calendar database.
"""

from __future__ import annotations

import json
import sqlite3
import time as _time
from datetime import datetime, timedelta

from . import store

TS_FMT = "%Y-%m-%dT%H:%M:%S"
COMMAND_TTL_MINUTES = 5
ACTIONS = ("restart", "shutdown")
LEVELS = ("ok", "check", "alert")          # worst last; "none" = not set up
LABELS = {"none": "Not set up", "ok": "OK", "check": "Check", "alert": "Alert"}

# get_throttled bits (Raspberry Pi firmware).
UNDERVOLT_NOW = 1 << 0
THROTTLED_NOW = 1 << 2
UNDERVOLT_SEEN = 1 << 16
THROTTLED_SEEN = 1 << 18

# Readings the helper may send, with their type and limits. Anything else is ignored.
_FIELDS = {
    "temp_c": (float, -40.0, 150.0),
    "throttled": (int, 0, 0xFFFFFFFF),
    "disk_free_pct": (float, 0.0, 100.0),
    "ssd_present": (bool, None, None),
    "uptime_s": (int, 0, 10 ** 9),
    "mem_avail_pct": (float, 0.0, 100.0),
    "epoch": (float, 0.0, 10 ** 11),
    "overlay": (bool, None, None),
    "helper_version": (str, None, 20),
    "os": (str, None, 60),
    "hdmi": (str, None, 20),
}


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], TS_FMT)
    except ValueError:
        return None


def _clean(readings: dict) -> dict:
    out = {}
    for key, (kind, lo, hi) in _FIELDS.items():
        if key not in readings or readings[key] is None:
            continue
        v = readings[key]
        try:
            if kind is bool:
                if not isinstance(v, bool):
                    continue
            elif kind is str:
                v = str(v).strip()[:hi]
            elif kind is int:
                if isinstance(v, bool):
                    continue
                v = int(v)
            else:
                if isinstance(v, bool):
                    continue
                v = float(v)
        except (TypeError, ValueError):
            continue
        if kind in (int, float) and not (lo <= v <= hi):
            continue
        out[key] = v
    return out


# ------------------------------------------------------------------ storage


def report(conn: sqlite3.Connection, readings: dict, now: datetime, server_epoch: float | None = None) -> dict:
    """Store a report from the Pi helper. Returns the readings as accepted.

    `server_epoch` (default: this machine's clock) is compared with the Pi's own
    clock to find how far off the Pi's clock is.
    """
    data = _clean(readings if isinstance(readings, dict) else {})
    if "epoch" in data:
        ref = server_epoch if server_epoch is not None else _time.time()
        data["clock_offset_s"] = round(data["epoch"] - ref, 1)
    conn.execute("UPDATE device_state SET report_received = ?, report_json = ? WHERE id = 1",
                 (_fmt(now), json.dumps(data)))
    conn.commit()
    return data


def mark_display_seen(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("UPDATE device_state SET display_seen = ? WHERE id = 1", (_fmt(now),))
    conn.commit()


def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT report_received, report_json, display_seen FROM device_state WHERE id = 1").fetchone()
    if row is None:
        return {"received": None, "readings": {}, "display_seen": None}
    try:
        readings = json.loads(row["report_json"]) if row["report_json"] else {}
    except ValueError:
        readings = {}
    return {"received": _parse(row["report_received"]), "readings": readings,
            "display_seen": _parse(row["display_seen"])}


def request_command(conn: sqlite3.Connection, action: str, now: datetime) -> int:
    """Queue Restart or Shut down, replacing any request still waiting."""
    if action not in ACTIONS:
        raise store.ValidationError({"action": "Choose restart or shutdown."})
    conn.execute("DELETE FROM device_commands WHERE delivered IS NULL")
    cur = conn.execute("INSERT INTO device_commands (action, requested) VALUES (?, ?)", (action, _fmt(now)))
    conn.commit()
    return cur.lastrowid


def pending_command(conn: sqlite3.Connection, now: datetime) -> dict | None:
    cutoff = _fmt(now - timedelta(minutes=COMMAND_TTL_MINUTES))
    row = conn.execute("SELECT id, action, requested FROM device_commands WHERE delivered IS NULL AND requested >= ?"
                       " ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
    return dict(row) if row else None


def take_command(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """The waiting request, if any, marked as delivered (the helper gets it once)."""
    cmd = pending_command(conn, now)
    if cmd:
        conn.execute("UPDATE device_commands SET delivered = ? WHERE id = ?", (_fmt(now), cmd["id"]))
    # Expired requests are simply dropped.
    conn.execute("DELETE FROM device_commands WHERE delivered IS NULL AND requested < ?",
                 (_fmt(now - timedelta(minutes=COMMAND_TTL_MINUTES)),))
    conn.commit()
    return {"id": cmd["id"], "action": cmd["action"]} if cmd else None


def last_commands(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, action, requested, delivered FROM device_commands ORDER BY id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------------------ settings

THRESHOLD_KEYS = {
    # key: (label, lo, hi)
    "health_temp_check": ("Temperature: amber at (°C)", 40, 95),
    "health_temp_alert": ("Temperature: red at (°C)", 45, 100),
    "health_disk_check": ("Free storage: amber below (%)", 1, 50),
    "health_disk_alert": ("Free storage: red below (%)", 1, 50),
    "health_contact_check": ("No contact: amber after (minutes)", 1, 120),
    "health_contact_alert": ("No contact: red after (minutes)", 2, 1440),
    "health_clock_check": ("Clock: amber when off by more than (seconds)", 5, 3600),
}


def thresholds(cfg: dict) -> dict:
    out = {}
    for key, (_, lo, hi) in THRESHOLD_KEYS.items():
        try:
            out[key] = int(cfg.get(key, store.DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            out[key] = int(store.DEFAULT_CONFIG[key])
    return out


def save_settings(conn: sqlite3.Connection, form: dict) -> dict:
    """Thresholds and the name shown in "Tell …", from the editor's Health page."""
    errors, clean = {}, {}
    for key, (label, lo, hi) in THRESHOLD_KEYS.items():
        raw = str(form.get(key, "")).strip()
        try:
            v = int(raw)
        except ValueError:
            errors[key] = "Enter a whole number."
            continue
        if not lo <= v <= hi:
            errors[key] = f"Use a value from {lo} to {hi}."
            continue
        clean[key] = str(v)
    pairs = [("health_temp_check", "health_temp_alert", "The red temperature must be higher than the amber one."),
             ("health_contact_check", "health_contact_alert", "Red must come later than amber.")]
    for a, b, msg in pairs:
        if a in clean and b in clean and int(clean[b]) <= int(clean[a]):
            errors[b] = msg
    if "health_disk_check" in clean and "health_disk_alert" in clean and \
            int(clean["health_disk_alert"]) >= int(clean["health_disk_check"]):
        errors["health_disk_alert"] = "Red must be a smaller amount of free space than amber."
    tell = str(form.get("health_tell", "")).strip()
    if len(tell) > 30:
        errors["health_tell"] = "Keep it to 30 characters or fewer."
    clean["health_tell"] = tell
    if errors:
        raise store.ValidationError(errors)
    conn.executemany(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(clean.items()))
    conn.commit()
    return clean


# ------------------------------------------------------------------ evaluation


def _ago(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    if s < 60:
        # The display refreshes once a minute, so seconds would freeze on screen.
        return "less than a minute ago"
    m = s // 60
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''} ago"
    h = m // 60
    if h < 48:
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = h // 24
    return f"{d} days ago"


def _uptime(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h = rem // 3600
    if d:
        return f"up {d} day{'s' if d != 1 else ''}"
    if h:
        return f"up {h} hour{'s' if h != 1 else ''}"
    return "just started"


def _row(key, label, value, level, note=""):
    return {"key": key, "label": label, "value": value, "level": level, "note": note}


def _worst(levels):
    rank = {"ok": 0, "check": 1, "alert": 2}
    best = "ok"
    for lv in levels:
        if lv in rank and rank[lv] > rank[best]:
            best = lv
    return best


def evaluate(conn: sqlite3.Connection, now: datetime, app_version: str = "") -> dict:
    """The health summary for the display and the editor."""
    cfg = store.get_config(conn)
    t = thresholds(cfg)
    st = state(conn)
    r = st["readings"]
    rows = []

    def age_level(seen):
        if seen is None:
            return None, ""
        mins = (now - seen).total_seconds() / 60
        if mins > t["health_contact_alert"]:
            return "alert", ""
        if mins > t["health_contact_check"]:
            return "check", ""
        return "ok", ""

    set_up = st["received"] is not None
    if set_up:
        temp = r.get("temp_c")
        if temp is not None:
            lv = "alert" if temp >= t["health_temp_alert"] else "check" if temp >= t["health_temp_check"] else "ok"
            note = {"alert": "Too hot", "check": "Running warm"}.get(lv, "")
            rows.append(_row("temperature", "Temperature", f"{round(temp)} °C", lv, note))
        thr = r.get("throttled")
        if thr is not None:
            if thr & UNDERVOLT_NOW:
                rows.append(_row("power", "Power", "Not enough power right now", "alert", "Check the power supply"))
            elif thr & UNDERVOLT_SEEN:
                rows.append(_row("power", "Power", "Dropped since the last start", "check", "Check the power supply"))
            elif thr & (THROTTLED_NOW | THROTTLED_SEEN):
                rows.append(_row("power", "Power", "Steady, but the Pi slowed down", "check", "Running warm"))
            else:
                rows.append(_row("power", "Power", "Steady, no drops", "ok"))
        if r.get("ssd_present") is False:
            rows.append(_row("storage", "Storage", "Drive not found", "alert", "Check the drive's cable"))
        elif r.get("disk_free_pct") is not None:
            free = r["disk_free_pct"]
            lv = "alert" if free < t["health_disk_alert"] else "check" if free < t["health_disk_check"] else "ok"
            note = {"alert": "Almost full", "check": "Getting full"}.get(lv, "")
            rows.append(_row("storage", "Storage", f"{round(free)} % free", lv, note))

    lv, _ = age_level(st["display_seen"])
    if lv:
        rows.append(_row("display", "Calendar server", "Schedule updated " + _ago(now - st["display_seen"]), lv,
                         "" if lv == "ok" else "Display isn't updating"))
    if set_up:
        lv, _ = age_level(st["received"])
        when = _ago(now - st["received"])
        rows.append(_row("pi", "Pi check-in", when[:1].upper() + when[1:], lv,
                         "" if lv == "ok" else "The Pi isn't reporting"))
        off = r.get("clock_offset_s")
        if off is not None:
            if abs(off) > t["health_clock_check"]:
                mins = abs(off) / 60
                amount = f"{round(mins)} minute{'s' if round(mins) != 1 else ''}" if mins >= 1 else f"{round(abs(off))} seconds"
                rows.append(_row("clock", "Clock", f"Off by {amount}", "check", "Clock is wrong"))
            else:
                rows.append(_row("clock", "Clock", "Correct", "ok"))

    versions = [f"Calendar {app_version}" if app_version else "Calendar"]
    if r.get("helper_version"):
        versions.append(f"Pi helper {r['helper_version']}")
    if r.get("uptime_s") is not None:
        versions.append(_uptime(r["uptime_s"]))

    if not set_up:
        level = "none"
        head = "Not set up yet"
        sub = "The Pi hasn't reported its health yet."
    else:
        level = _worst(row["level"] for row in rows)
        tell = cfg.get("health_tell", "").strip()
        worst_note = next((row["note"] for lvl in ("alert", "check") for row in rows
                           if row["level"] == lvl and row["note"]), "")
        if level == "ok":
            head = "Everything looks good"
            sub = "Checked " + _ago(now - st["received"])
        else:
            head = "Something is wrong" if level == "alert" else "Something should be checked"
            detail = worst_note[:1].lower() + worst_note[1:] if worst_note else ""
            if tell:
                sub = f"Tell {tell}" + (f": {detail}" if detail else "")
            else:
                sub = worst_note or "See the readings below"
    return {
        "level": level,
        "label": LABELS[level],
        "head": head,
        "sub": sub,
        "rows": rows,
        "versions": " · ".join(versions),
        "set_up": set_up,
        "received": _fmt(st["received"]) if st["received"] else None,
        "readings": r,
    }
