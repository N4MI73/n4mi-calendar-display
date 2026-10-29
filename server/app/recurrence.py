"""Repeat patterns: building, reading back, describing and expanding them.

Patterns are stored as standard iCalendar RRULE text (the same form Google
uses), without DTSTART or UNTIL: the series' start comes from the event's own
start, and the optional end date is kept in its own column.

The editor offers four patterns (Phase 2 plan, decision D38):
  weekly        every 1 or 2 weeks on chosen weekdays   FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH
  monthly_nth   1st/2nd/3rd/4th/5th/last <weekday>      FREQ=MONTHLY;BYDAY=2FR,3FR,4FR,5FR
  monthly_date  the same date each month                FREQ=MONTHLY;BYMONTHDAY=15
  yearly        the same date each year                 FREQ=YEARLY

Expansion uses python-dateutil on naive local wall-clock datetimes, so a
10:00 AM series stays at 10:00 AM across daylight saving changes. Any other
valid RRULE (for example one imported from Google later) still expands; the
editor just shows it as "set elsewhere" instead of as one of the four choices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dateutil.rrule import rrulestr

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]  # index = date.weekday()
WEEKDAY_NAMES = {
    "MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday",
    "FR": "Friday", "SA": "Saturday", "SU": "Sunday",
}
ORDINALS = ["1", "2", "3", "4", "5", "-1"]
ORDINAL_NAMES = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "5": "5th", "-1": "last"}
REPEAT_CHOICES = ["none", "weekly", "monthly_nth", "monthly_date", "yearly"]
# A household reminder that doesn't repeat (D47) is stored as a rule with one date,
# so it uses the same expansion as the others. Events that don't repeat store no rule.
ONCE = "FREQ=DAILY;COUNT=1"


@dataclass
class Pattern:
    """A repeat pattern as the editor form sees it."""

    repeat: str = "none"                 # one of REPEAT_CHOICES, or "custom"
    interval: str = "1"                  # weekly only: "1" or "2"
    weekdays: list[str] = field(default_factory=list)   # weekly: ["MO", "FR"]
    ordinals: list[str] = field(default_factory=list)   # monthly_nth: ["2", "3", "-1"]
    nth_weekday: str = ""                # monthly_nth: "FR"
    until: str = ""                      # optional end date, YYYY-MM-DD
    custom_rrule: str = ""               # a rule the editor can't show as a choice


def _ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_rrule(p: Pattern, start: date, errors: dict[str, str]) -> str | None:
    """Turn a form pattern into RRULE text. Adds messages to `errors` if incomplete."""
    if p.repeat == "none":
        return None
    if p.repeat == "custom":
        return p.custom_rrule or None
    if p.repeat == "weekly":
        days = [d for d in WEEKDAYS if d in p.weekdays]
        if not days:
            errors["repeat"] = "Tick at least one day of the week."
            return None
        if p.interval not in ("1", "2"):
            errors["repeat"] = "Choose every week or every other week."
            return None
        rule = "FREQ=WEEKLY"
        if p.interval == "2":
            rule += ";INTERVAL=2"
        return rule + ";BYDAY=" + ",".join(days)
    if p.repeat == "monthly_nth":
        ords = [o for o in ORDINALS if o in p.ordinals]
        if not ords:
            errors["repeat"] = "Tick at least one of 1st, 2nd, 3rd, 4th, 5th or last."
            return None
        if p.nth_weekday not in WEEKDAYS:
            errors["repeat"] = "Choose the day of the week."
            return None
        return "FREQ=MONTHLY;BYDAY=" + ",".join(f"{o}{p.nth_weekday}" for o in ords)
    if p.repeat == "monthly_date":
        return f"FREQ=MONTHLY;BYMONTHDAY={start.day}"
    if p.repeat == "yearly":
        return "FREQ=YEARLY"
    errors["repeat"] = "Choose how this repeats."
    return None


def _parts(rule: str) -> dict[str, str]:
    out = {}
    for piece in rule.split(";"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            out[k.strip().upper()] = v.strip().upper()
    return out


def pattern_from_rrule(rule: str | None, until: str | None, start: date) -> Pattern:
    """Read stored RRULE text back into form values (or 'custom' if it isn't one of the four)."""
    if not rule or rule == ONCE:
        return Pattern(repeat="none", weekdays=[WEEKDAYS[start.weekday()]],
                       ordinals=[_nth_of_month(start)], nth_weekday=WEEKDAYS[start.weekday()])
    p = _parts(rule)
    custom = Pattern(repeat="custom", custom_rrule=rule, until=until or "")
    extra = set(p) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "WKST"}
    if extra:
        return custom
    freq, interval = p.get("FREQ"), p.get("INTERVAL", "1")
    byday = [d for d in p.get("BYDAY", "").split(",") if d]
    if freq == "WEEKLY" and interval in ("1", "2") and "BYMONTHDAY" not in p:
        days = byday or [WEEKDAYS[start.weekday()]]
        if all(d in WEEKDAYS for d in days):
            return Pattern(repeat="weekly", interval=interval, weekdays=days, until=until or "")
    if freq == "MONTHLY" and interval == "1" and byday and "BYMONTHDAY" not in p:
        ords, wds = [], set()
        for d in byday:
            o, wd = d[:-2], d[-2:]
            if o not in ORDINALS or wd not in WEEKDAYS:
                return custom
            ords.append(o)
            wds.add(wd)
        if len(wds) == 1:
            return Pattern(repeat="monthly_nth", ordinals=ords, nth_weekday=wds.pop(), until=until or "")
    if freq == "MONTHLY" and interval == "1" and not byday:
        if p.get("BYMONTHDAY", str(start.day)) == str(start.day):
            return Pattern(repeat="monthly_date", until=until or "")
    if freq == "YEARLY" and interval == "1" and not byday and "BYMONTHDAY" not in p:
        return Pattern(repeat="yearly", until=until or "")
    return custom


def _nth_of_month(d: date) -> str:
    """Which weekday-of-the-month a date is: '1'..'5' (used to pre-fill the form)."""
    return str((d.day - 1) // 7 + 1)


def describe(rule: str | None, start: date, until: str | None = None) -> str:
    """Plain-English summary, for example 'Every month on the 2nd, 3rd, 4th and 5th Friday'."""
    if not rule:
        return "Doesn't repeat"
    if rule == ONCE:
        return f"Once, on {start.strftime('%a, %b')} {start.day}, {start.year}"
    p = pattern_from_rrule(rule, until, start)
    if p.repeat == "weekly":
        names = [WEEKDAY_NAMES[d] for d in WEEKDAYS if d in p.weekdays]
        text = ("Every other week" if p.interval == "2" else "Every week") + " on " + _join(names)
    elif p.repeat == "monthly_nth":
        ords = [ORDINAL_NAMES[o] for o in ORDINALS if o in p.ordinals]
        text = f"Every month on the {_join(ords)} {WEEKDAY_NAMES[p.nth_weekday]}"
    elif p.repeat == "monthly_date":
        text = f"Every month on the {start.day}{_ordinal_suffix(start.day)}"
        if start.day > 28:
            text += " (months without one are skipped)"
    elif p.repeat == "yearly":
        text = f"Every year on {start.strftime('%B')} {start.day}"
    else:
        text = "Repeats (set elsewhere)"
    if until:
        u = datetime.strptime(until, "%Y-%m-%d").date()
        text += f", until {u.strftime('%b')} {u.day}, {u.year}"
    return text


def _rule(rule: str, dtstart: datetime, until: str | None):
    r = rrulestr(rule, dtstart=dtstart, ignoretz=True)
    if until and "COUNT=" not in rule.upper():   # a rule can't have both
        end = datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
        r = r.replace(until=end)
    return r


def occurrences(rule: str, dtstart: datetime, until: str | None, lo: datetime, hi: datetime) -> list[datetime]:
    """Start times of occurrences with lo <= start <= hi (naive local wall-clock)."""
    return _rule(rule, dtstart, until).between(lo, hi, inc=True)


def is_occurrence(rule: str, dtstart: datetime, until: str | None, day: date) -> datetime | None:
    """The occurrence that starts on `day`, if the series has one."""
    lo = datetime.combine(day, datetime.min.time())
    hi = lo + timedelta(days=1) - timedelta(seconds=1)
    found = occurrences(rule, dtstart, until, lo, hi)
    return found[0] if found else None


def latest_on_or_before(rule: str, dtstart: datetime, until: str | None, moment: datetime) -> datetime | None:
    return _rule(rule, dtstart, until).before(moment, inc=True)


def next_after(rule: str, dtstart: datetime, until: str | None, moment: datetime) -> datetime | None:
    return _rule(rule, dtstart, until).after(moment, inc=False)
