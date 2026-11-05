"""US holidays, calculated on the server (Phase 5b, decision D59 as amended 2026-11-05).

The holidays are worked out from fixed rules, not read from Google: nothing to
fetch, nothing that breaks if a feed changes. Dan ticks which ones to show in
Settings; the federal holidays are ticked to begin with. They are read-only
reference items, never events: no reminders, no "Not in Google" flag, no move
or delete on the shelf.

Dates are the holiday itself (Independence Day is July 4 even when the
federal day off is Friday July 3).
"""

from __future__ import annotations

from datetime import date, timedelta

# key: (name, federal?, rule). A rule is ("fixed", month, day), ("nth", month, weekday, n)
# where weekday is 0=Monday and n=-1 means the last one, ("easter",) or ("election",).
HOLIDAYS = {
    "new_year": ("New Year's Day", True, ("fixed", 1, 1)),
    "mlk": ("Martin Luther King Jr. Day", True, ("nth", 1, 0, 3)),
    "groundhog": ("Groundhog Day", False, ("fixed", 2, 2)),
    "valentines": ("Valentine's Day", False, ("fixed", 2, 14)),
    "presidents": ("Presidents' Day", True, ("nth", 2, 0, 3)),
    "dst_start": ("Clocks go forward an hour", False, ("nth", 3, 6, 2)),
    "st_patricks": ("St. Patrick's Day", False, ("fixed", 3, 17)),
    "easter": ("Easter", False, ("easter",)),
    "mothers": ("Mother's Day", False, ("nth", 5, 6, 2)),
    "memorial": ("Memorial Day", True, ("nth", 5, 0, -1)),
    "fathers": ("Father's Day", False, ("nth", 6, 6, 3)),
    "juneteenth": ("Juneteenth", True, ("fixed", 6, 19)),
    "independence": ("Independence Day", True, ("fixed", 7, 4)),
    "labor": ("Labor Day", True, ("nth", 9, 0, 1)),
    "columbus": ("Columbus Day", True, ("nth", 10, 0, 2)),
    "halloween": ("Halloween", False, ("fixed", 10, 31)),
    "dst_end": ("Clocks go back an hour", False, ("nth", 11, 6, 1)),
    "election": ("Election Day", False, ("election",)),
    "veterans": ("Veterans Day", True, ("fixed", 11, 11)),
    "thanksgiving": ("Thanksgiving", True, ("nth", 11, 3, 4)),
    "christmas_eve": ("Christmas Eve", False, ("fixed", 12, 24)),
    "christmas": ("Christmas Day", True, ("fixed", 12, 25)),
    "new_years_eve": ("New Year's Eve", False, ("fixed", 12, 31)),
}

FEDERAL = [k for k, (_, fed, _) in HOLIDAYS.items() if fed]


def easter(year: int) -> date:
    """Western Easter Sunday (the Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def date_of(key: str, year: int) -> date:
    rule = HOLIDAYS[key][2]
    if rule[0] == "fixed":
        return date(year, rule[1], rule[2])
    if rule[0] == "nth":
        return _nth(year, rule[1], rule[2], rule[3])
    if rule[0] == "easter":
        return easter(year)
    # Election Day: the Tuesday after the first Monday in November.
    return _nth(year, 11, 0, 1) + timedelta(days=1)


def between(lo: date, hi: date, keys) -> dict[date, list[str]]:
    """The chosen holidays from lo to hi (inclusive), by date."""
    out: dict[date, list[str]] = {}
    for year in range(lo.year, hi.year + 1):
        for key in HOLIDAYS:
            if key not in keys:
                continue
            d = date_of(key, year)
            if lo <= d <= hi:
                out.setdefault(d, []).append(HOLIDAYS[key][0])
    return out


def chosen(cfg: dict) -> list[str]:
    """The keys Dan has ticked, or none if holidays are switched off."""
    if cfg.get("holidays_on", "1") != "1":
        return []
    raw = cfg.get("holidays")
    if raw is None:
        return list(FEDERAL)
    return [k for k in raw.split(",") if k in HOLIDAYS]
