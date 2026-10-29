"""Phase 3 tests: the display API, reminder units and all-day reminders,
"this and all future" edits, and the display page itself.

Plain Python (unittest). All data is fictional.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, recurrence, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.alex = store.save_person(self.conn, "Alex", "#4C9AFF")
        self.robin = store.save_person(self.conn, "Robin", "#F06BA8")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def cat(self, name):
        return self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]

    def add(self, **kw):
        base = dict(title="Dentist", start_date="2026-11-02", start_time="14:30", end_date="2026-11-02",
                    end_time="15:30", category_id=str(self.cat("Medical")), person_ids=[str(self.alex)])
        base.update(kw)
        if base.get("all_day"):
            base.setdefault("start_time", "")
            base["start_time"] = base["end_time"] = ""
        return store.save_event(self.conn, store.EventInput(**base))

    def payload(self, now):
        return display.build(self.conn, timeutil.parse_local(now))

    def day(self, data, d):
        return next(x for x in data["days"] if x["date"] == d)


# ----------------------------------------------------------- reminder units


class TestReminderUnits(Base):
    def test_amount_and_unit_become_minutes(self):
        for amount, unit, minutes in [("45", "minutes", 45), ("2", "hours", 120), ("3", "days", 4320)]:
            eid = self.add(reminder_mode="custom", reminder_minutes=amount, reminder_unit=unit)
            self.assertEqual(store.get_event(self.conn, eid)["reminder_minutes"], minutes)

    def test_shown_back_in_the_largest_whole_unit(self):
        self.assertEqual(store.split_minutes(45), ("45", "minutes"))
        self.assertEqual(store.split_minutes(120), ("2", "hours"))
        self.assertEqual(store.split_minutes(90), ("90", "minutes"))
        self.assertEqual(store.split_minutes(2880), ("2", "days"))
        self.assertEqual(store.split_minutes(0), ("0", "minutes"))
        self.assertEqual(store.split_minutes(None), ("", "minutes"))
        eid = self.add(reminder_mode="custom", reminder_minutes="2", reminder_unit="days")
        form = store.event_to_input(store.get_event(self.conn, eid))
        self.assertEqual((form.reminder_minutes, form.reminder_unit), ("2", "days"))

    def test_limits_and_bad_units(self):
        for amount, unit in [("31", "days"), ("-1", "hours"), ("2", "weeks"), ("x", "minutes"), ("", "hours")]:
            with self.assertRaises(store.ValidationError, msg=(amount, unit)):
                self.add(reminder_mode="custom", reminder_minutes=amount, reminder_unit=unit)
        self.add(reminder_mode="custom", reminder_minutes="30", reminder_unit="days")  # the most allowed

    def test_all_day_custom_reminder_must_be_in_days(self):
        with self.assertRaises(store.ValidationError) as cm:
            self.add(all_day=True, reminder_mode="custom", reminder_minutes="2", reminder_unit="hours")
        self.assertIn("reminder", cm.exception.errors)

    def test_category_defaults_in_units_and_all_day_days(self):
        cid = self.cat("Family")
        store.save_category(self.conn, "Family", "#46A758", "2", cid, default_unit="hours", allday_days="2")
        cat = store.get_category(self.conn, cid)
        self.assertEqual(cat["default_reminder_minutes"], 120)
        self.assertEqual(cat["default_allday_days"], 2)
        store.save_category(self.conn, "Family", "#46A758", "", cid, allday_days="")
        cat = store.get_category(self.conn, cid)
        self.assertIsNone(cat["default_reminder_minutes"])
        self.assertIsNone(cat["default_allday_days"])
        with self.assertRaises(store.ValidationError):
            store.save_category(self.conn, "Family", "#46A758", "1", cid, allday_days="40")


class TestAllDayReminders(Base):
    def rem(self, **kw):
        eid = self.add(all_day=True, category_id=str(self.cat("Family")), **kw)
        return store.effective_reminder_minutes(store.get_event(self.conn, eid))

    def test_none_until_the_category_sets_one(self):
        self.assertIsNone(self.rem())

    def test_category_all_day_default(self):
        store.save_category(self.conn, "Family", "#46A758", "1", self.cat("Family"), default_unit="hours",
                            allday_days="2")
        self.assertEqual(self.rem(), 2 * 1440)
        self.assertIsNone(self.rem(reminder_mode="none"))
        self.assertEqual(self.rem(reminder_mode="custom", reminder_minutes="5", reminder_unit="days"), 5 * 1440)

    def test_timed_events_still_use_the_timed_default(self):
        store.save_category(self.conn, "Family", "#46A758", "1", self.cat("Family"), default_unit="hours",
                            allday_days="2")
        eid = self.add(category_id=str(self.cat("Family")))
        self.assertEqual(store.effective_reminder_minutes(store.get_event(self.conn, eid)), 60)


# ------------------------------------------------------------- migration


class TestMigration0003(unittest.TestCase):
    def test_from_a_phase_2_database_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            # Build a Phase 2 database (migrations 0001 and 0002 only) with an event in it.
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
            for v, p in db.available_migrations():
                if v > 2:
                    break
                conn.executescript(p.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (v,))
            conn.execute("INSERT INTO events (title, start_local, end_local, category_id, created, updated)"
                         " VALUES ('Birthday', '2026-11-05', '2026-11-05', 2, 'x', 'x')")
            conn.commit()
            conn.close()
            self.assertEqual(db.migrate(path), [3])
            self.assertEqual(len(list(Path(tmp).glob("calendar.db.pre-v2-*.bak"))), 1)
            c = db.connect(path)
            self.assertEqual(c.execute("SELECT title FROM events").fetchone()[0], "Birthday")
            self.assertTrue(all(r["default_allday_days"] is None for r in store.list_categories(c)))
            c.close()
            self.assertEqual(db.migrate(path), [])   # nothing more to do


# ------------------------------------------------------------ remind_at


class TestRemindAt(Base):
    DARK = (time(23, 0), time(6, 0))

    def at(self, eid):
        ev = store.get_event(self.conn, eid)
        r = display.remind_at(ev, *self.DARK)
        return r.strftime(timeutil.DT_FMT) if r else None

    def test_timed_event_lead_time(self):
        self.assertEqual(self.at(self.add()), "2026-11-02T12:30")   # Medical: 2 hours

    def test_moved_out_of_dark_hours(self):
        # 6:30 AM appointment, 2-hour lead would be 4:30 AM: shown at the end of dark hours instead.
        self.assertEqual(self.at(self.add(start_time="06:30", end_time="07:30")), "2026-11-02T06:00")
        # 11:30 PM lead time the evening before a 1:00 AM event: not until 6:00 AM (after the start).
        eid = self.add(start_date="2026-11-03", end_date="2026-11-03", start_time="01:00", end_time="02:00",
                       reminder_mode="custom", reminder_minutes="90", reminder_unit="minutes")
        self.assertEqual(self.at(eid), "2026-11-03T06:00")

    def test_all_day_days_before_in_the_morning(self):
        eid = self.add(all_day=True, start_date="2026-11-05", end_date="2026-11-05",
                       reminder_mode="custom", reminder_minutes="2", reminder_unit="days")
        self.assertEqual(self.at(eid), "2026-11-03T06:00")
        eid = self.add(all_day=True, start_date="2026-11-05", end_date="2026-11-05",
                       reminder_mode="custom", reminder_minutes="0", reminder_unit="days")
        self.assertEqual(self.at(eid), "2026-11-05T06:00")
        ev = store.get_event(self.conn, eid)
        self.assertEqual(display.remind_at(ev, time(6, 0), time(6, 0)).strftime(timeutil.DT_FMT),
                         "2026-11-05T00:00")   # no dark hours: midnight

    def test_no_reminder(self):
        self.assertIsNone(self.at(self.add(reminder_mode="none")))
        self.assertIsNone(self.at(self.add(category_id=str(self.cat("Info")))))


# -------------------------------------------------------------- display API


class TestDisplayPayload(Base):
    def test_shape_and_window(self):
        self.add()
        data = self.payload("2026-11-02T08:05")
        self.assertEqual(data["api"], 1)
        self.assertEqual(data["generated"], "2026-11-02T08:05")
        self.assertEqual(data["config"], {"upcoming_days": 14, "stale_minutes": 30, "dark_start": "23:00",
                                          "dark_end": "06:00", "wake_seconds": 120})
        # Today + 14 days + 7 days of spare for the offline copy.
        self.assertEqual(len(data["days"]), 22)
        self.assertEqual(data["days"][0]["date"], "2026-11-02")
        self.assertEqual(data["days"][-1]["date"], "2026-11-23")
        d = data["days"][0]
        self.assertEqual((d["weekday"], d["month_short"], d["day"]), ("Monday", "Nov", 2))
        ev = d["events"][0]
        for key in ("id", "occ", "title", "location", "all_day", "start", "end", "time", "end_time", "people",
                    "category", "info", "remind_at", "repeats", "day_index", "day_count", "until"):
            self.assertIn(key, ev)
        self.assertEqual((ev["time"], ev["end_time"]), ("2:30 PM", "3:30 PM"))
        self.assertEqual(ev["people"], [{"name": "Alex", "color": "#4C9AFF"}])
        self.assertEqual(ev["category"]["name"], "Medical")
        self.assertEqual(ev["remind_at"], "2026-11-02T12:30")

    def test_window_follows_the_upcoming_setting(self):
        store.save_config(self.conn, dict(store.get_config(self.conn), upcoming_days="7"))
        self.assertEqual(len(self.payload("2026-11-02T08:05")["days"]), 15)

    def test_multi_day_event_on_each_day(self):
        self.add(title="Visit", all_day=True, start_date="2026-11-01", end_date="2026-11-04",
                 category_id=str(self.cat("Family")), person_ids=[str(self.alex), str(self.robin)])
        data = self.payload("2026-11-02T08:05")
        seen = [(d["date"], e["day_index"], e["day_count"], e["until"]) for d in data["days"] for e in d["events"]]
        self.assertEqual(seen, [("2026-11-02", 2, 4, "Wed"), ("2026-11-03", 3, 4, "Wed"), ("2026-11-04", 4, 4, "Wed")])
        self.assertEqual([p["name"] for p in data["days"][0]["events"][0]["people"]], ["Alex", "Robin"])

    def test_timed_event_across_midnight_listed_on_its_start_day(self):
        self.add(title="Night net", start_date="2026-11-03", end_date="2026-11-04", start_time="22:00",
                 end_time="01:00")
        data = self.payload("2026-11-02T08:05")
        self.assertEqual([e["title"] for e in self.day(data, "2026-11-03")["events"]], ["Night net"])
        self.assertEqual(self.day(data, "2026-11-04")["events"], [])

    def test_all_day_listed_before_timed(self):
        self.add(title="Later")
        self.add(title="Holiday", all_day=True, category_id=str(self.cat("Other")))
        self.assertEqual([e["title"] for e in self.payload("2026-11-02T08:05")["days"][0]["events"]],
                         ["Holiday", "Later"])

    def test_info_events_flagged(self):
        self.add(title="Flight lands", category_id=str(self.cat("Info")))
        ev = self.payload("2026-11-02T08:05")["days"][0]["events"][0]
        self.assertTrue(ev["info"])
        self.assertIsNone(ev["remind_at"])

    def test_repeating_events_skips_and_changes(self):
        eid = self.add(title="Coffee", start_date="2026-10-09", end_date="2026-10-09", start_time="10:00",
                       end_time="11:00", category_id=str(self.cat("Social")),
                       repeat="monthly_nth", ordinals=["2", "3", "4", "5"], nth_weekday="FR")
        store.skip_occurrence(self.conn, eid, "2026-11-13")
        event = store.get_event(self.conn, eid)
        store.change_occurrence(self.conn, event, "2026-11-20", store.OccurrenceInput(
            title="Coffee (new place)", start_date="2026-11-20", start_time="10:30",
            end_date="2026-11-20", end_time="11:30"))
        data = self.payload("2026-11-02T08:05")
        titles = {d["date"]: [(e["title"], e["time"]) for e in d["events"]] for d in data["days"]}
        self.assertEqual(titles["2026-11-06"], [])                       # 1st Friday: not coffee
        self.assertEqual(titles["2026-11-13"], [])                       # skipped
        self.assertEqual(titles["2026-11-20"], [("Coffee (new place)", "10:30 AM")])
        ev = self.day(data, "2026-11-20")["events"][0]
        self.assertTrue(ev["repeats"])
        self.assertEqual(ev["occ"], "2026-11-20")

    def test_wall_clock_time_across_daylight_saving(self):
        self.add(title="Net", start_date="2026-10-25", end_date="2026-10-25", start_time="10:00", end_time="11:00",
                 category_id=str(self.cat("Club / Ham radio")), repeat="weekly", weekdays=["SU"])
        for now, d in [("2026-10-31T09:00", "2026-11-01"), ("2027-03-13T09:00", "2027-03-14")]:
            ev = self.day(self.payload(now), d)["events"][0]
            self.assertEqual((ev["start"], ev["time"], ev["remind_at"]), (d + "T10:00", "10:00 AM", d + "T09:00"))

    def test_reminders_show_from_their_appear_time(self):
        store.save_reminder(self.conn, store.ReminderInput(text="Garbage cans to the curb", start_date="2026-11-02",
                                                           repeat="weekly", weekdays=["MO"]))
        self.assertEqual(self.payload("2026-11-02T05:30")["reminders"], [])   # still dark hours
        rems = self.payload("2026-11-02T06:00")["reminders"]
        self.assertEqual(rems, [{"id": 1, "text": "Garbage cans to the curb", "carry_over": False,
                                 "due": "2026-11-02"}])
        self.assertEqual(self.payload("2026-11-03T08:00")["reminders"], [])   # carry-over off: gone next day

    def test_carry_over_reminder_waits(self):
        store.save_reminder(self.conn, store.ReminderInput(text="Change the A/C filters", start_date="2026-11-05",
                                                           repeat="monthly_nth", ordinals=["1"], nth_weekday="TH",
                                                           carry_over=True))
        rems = self.payload("2026-11-09T08:00")["reminders"]
        self.assertEqual([(r["text"], r["carry_over"], r["due"]) for r in rems],
                         [("Change the A/C filters", True, "2026-11-05")])

    def test_quiet_day(self):
        self.add(start_date="2026-11-04", end_date="2026-11-04")
        data = self.payload("2026-11-02T08:05")
        self.assertEqual(data["days"][0]["events"], [])
        self.assertEqual(len(self.day(data, "2026-11-04")["events"]), 1)


class TestDisplayEndpoints(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))
        store.save_reminder(self.conn, store.ReminderInput(text="Garbage cans to the curb", start_date="2026-11-02",
                                                           repeat="weekly", weekdays=["MO"]))

    def test_payload_over_http(self):
        r = self.client.get("/api/v1/display?now=2026-11-02T08:05")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        body = r.json()
        self.assertEqual((body["ok"], body["api"]), (True, 1))
        self.assertRegex(body["app_version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(len(body["reminders"]), 1)

    def test_without_test_clock_uses_now(self):
        body = self.client.get("/api/v1/display").json()
        self.assertEqual(body["days"][0]["date"], timeutil.now_local().date().strftime(timeutil.DATE_FMT))

    def test_unsupported_version_and_bad_clock(self):
        r = self.client.get("/api/v2/display")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"ok": False, "error": "unsupported_api_version", "supported": [1]})
        r = self.client.get("/api/v1/display?now=tomorrow")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_now"))

    def test_i_did_it_and_undo(self):
        url = "/api/v1/reminders/1/{}?now=2026-11-02T08:10"
        r = self.client.post(url.format("done"))
        self.assertEqual(r.json(), {"ok": True, "id": 1, "action": "done"})
        self.assertEqual(self.client.get("/api/v1/display?now=2026-11-02T08:11").json()["reminders"], [])
        # Ticking again: it isn't showing any more.
        r = self.client.post(url.format("done"))
        self.assertEqual((r.status_code, r.json()["error"]), (409, "not_showing"))
        r = self.client.post(url.format("undo"))
        self.assertEqual(r.json()["action"], "undo")
        self.assertEqual(len(self.client.get("/api/v1/display?now=2026-11-02T08:12").json()["reminders"]), 1)
        r = self.client.post(url.format("undo"))
        self.assertEqual((r.status_code, r.json()["error"]), (409, "nothing_to_undo"))

    def test_reminder_endpoint_errors(self):
        self.assertEqual(self.client.post("/api/v1/reminders/99/done").json()["error"], "no_such_reminder")
        self.assertEqual(self.client.post("/api/v1/reminders/1/delete").json()["error"], "unknown_action")
        self.assertEqual(self.client.post("/api/v9/reminders/1/done").status_code, 404)

    def test_display_page_is_self_contained(self):
        r = self.client.get("/display")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/static/display.js", r.text)
        self.assertIn('name="app-version"', r.text)
        for path in ("/static/display.js", "/static/display.css", "/static/fonts/AtkinsonHyperlegible-Regular.woff2",
                     "/static/fonts/AtkinsonHyperlegible-Bold.woff2", "/static/fonts/AtkinsonHyperlegible-Italic.woff2",
                     "/static/fonts/OFL.txt"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        # Nothing is loaded from the internet at runtime.
        for name in ("templates/display.html", "static/display.css", "static/display.js"):
            text = (APP_DIR / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(src|href|url)\s*[=(]\s*[\"']?https?://", text), name)

    def test_display_page_never_uses_innerhtml_for_calendar_data(self):
        js = (APP_DIR / "static/display.js").read_text(encoding="utf-8")
        # The only innerHTML is the fixed message text.
        self.assertEqual(js.count(".innerHTML"), 1)
        self.assertIn('$("message-text").innerHTML = html', js)


# ---------------------------------------------------- this and all future (D46)


class TestSplitSeries(Base):
    def coffee(self, **kw):
        base = dict(title="Coffee", start_date="2026-10-02", end_date="2026-10-02", start_time="10:00",
                    end_time="11:00", category_id=str(self.cat("Social")), location="Corner Cafe",
                    person_ids=[str(self.alex), str(self.robin)], repeat="weekly", weekdays=["FR"])
        base.update(kw)
        return self.add(**base)

    def listing(self, lo="2026-10-01", hi="2026-12-31"):
        return [(e["start_local"], e["title"], e["location"])
                for e in store.list_events(self.conn, timeutil.parse_date(lo), timeutil.parse_date(hi))]

    def test_change_from_a_date_on(self):
        eid = self.coffee()
        form = store.future_input(store.get_event(self.conn, eid), "2026-11-06")
        self.assertEqual((form.start_date, form.start_time), ("2026-11-06", "10:00"))
        form.location = "New Cafe"
        form.start_time, form.end_time = "10:30", "11:30"
        new_id = store.split_series(self.conn, eid, "2026-11-06", form)
        self.assertNotEqual(new_id, eid)
        old, new = store.get_event(self.conn, eid), store.get_event(self.conn, new_id)
        self.assertEqual(old["recurrence_end"], "2026-11-05")
        self.assertEqual(new["start_local"], "2026-11-06T10:30")
        self.assertEqual([p["name"] for p in new["people"]], ["Alex", "Robin"])
        rows = self.listing("2026-10-23", "2026-11-13")
        self.assertEqual(rows, [("2026-10-23T10:00", "Coffee", "Corner Cafe"),
                                ("2026-10-30T10:00", "Coffee", "Corner Cafe"),
                                ("2026-11-06T10:30", "Coffee", "New Cafe"),
                                ("2026-11-13T10:30", "Coffee", "New Cafe")])

    def test_adjusted_dates_carried_or_dropped(self):
        eid = self.coffee()
        store.skip_occurrence(self.conn, eid, "2026-10-16")          # before the split: stays
        store.skip_occurrence(self.conn, eid, "2026-11-20")          # after, still a Friday: carried
        event = store.get_event(self.conn, eid)
        store.change_occurrence(self.conn, event, "2026-11-27", store.OccurrenceInput(
            title="Coffee", start_date="2026-11-27", start_time="09:00", end_date="2026-11-27", end_time="10:00"))

        # Same pattern: nothing is dropped, both adjustments move to the new series.
        form = store.future_input(store.get_event(self.conn, eid), "2026-11-06")
        form.title = "Coffee club"
        self.assertEqual(store.split_dropped(self.conn, eid, "2026-11-06", form), [])
        new_id = store.split_series(self.conn, eid, "2026-11-06", form)
        self.assertEqual(sorted(store.get_exceptions(self.conn, eid)), ["2026-10-16"])
        self.assertEqual(sorted(store.get_exceptions(self.conn, new_id)), ["2026-11-20", "2026-11-27"])
        days = [r[0] for r in self.listing("2026-11-13", "2026-11-30")]
        self.assertEqual(days, ["2026-11-13T10:00", "2026-11-27T09:00"])

        # Pattern changes to Thursdays from Dec 3: the Friday adjustments no longer line up.
        form2 = store.future_input(store.get_event(self.conn, new_id), "2026-12-04")
        self.assertEqual(store.split_dropped(self.conn, new_id, "2026-12-04", form2), [])
        store.skip_occurrence(self.conn, new_id, "2026-12-11")
        form2 = store.future_input(store.get_event(self.conn, new_id), "2026-12-04")
        form2.start_date = form2.end_date = "2026-12-10"
        form2.weekdays = ["TH"]
        dropped = store.split_dropped(self.conn, new_id, "2026-12-04", form2)
        self.assertEqual([x["occ_date"] for x in dropped], ["2026-12-11"])
        third = store.split_series(self.conn, new_id, "2026-12-04", form2)
        self.assertEqual(store.get_exceptions(self.conn, third), {})
        self.assertEqual([r[0] for r in self.listing("2026-12-01", "2026-12-18")],
                         ["2026-12-10T10:00", "2026-12-17T10:00"])

    def test_first_date_means_the_whole_series(self):
        eid = self.coffee()
        form = store.future_input(store.get_event(self.conn, eid), "2026-10-02")
        form.title = "Coffee club"
        self.assertEqual(store.split_series(self.conn, eid, "2026-10-02", form), eid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertEqual(store.get_event(self.conn, eid)["title"], "Coffee club")

    def test_new_part_cannot_start_before_the_chosen_date(self):
        eid = self.coffee()
        form = store.future_input(store.get_event(self.conn, eid), "2026-11-06")
        form.start_date = form.end_date = "2026-10-30"
        with self.assertRaises(store.ValidationError) as cm:
            store.split_series(self.conn, eid, "2026-11-06", form)
        self.assertIn("start_date", cm.exception.errors)

    def test_delete_from_a_date_on(self):
        eid = self.coffee()
        store.skip_occurrence(self.conn, eid, "2026-11-20")
        self.assertFalse(store.end_series_from(self.conn, eid, "2026-11-06"))
        self.assertEqual(store.get_event(self.conn, eid)["recurrence_end"], "2026-11-05")
        self.assertEqual(store.get_exceptions(self.conn, eid), {})
        self.assertEqual([r[0] for r in self.listing("2026-10-23", "2026-12-31")],
                         ["2026-10-23T10:00", "2026-10-30T10:00"])
        self.assertTrue(store.end_series_from(self.conn, eid, "2026-10-02"))   # from the first date: all gone
        self.assertIsNone(store.get_event(self.conn, eid))


class TestOneTimeReminders(Base):
    """'Doesn't repeat' household reminders (D47)."""

    def once(self, **kw):
        base = dict(text="Call the plumber about the estimate", start_date="2026-11-05", repeat="none")
        base.update(kw)
        return store.save_reminder(self.conn, store.ReminderInput(**base))

    def showing(self, when):
        return [r["text"] for r in store.reminders_showing(self.conn, datetime.fromisoformat(when))]

    def test_stored_as_a_single_date(self):
        rid = self.once(until="2026-12-31")          # a leftover end date is ignored
        rem = store.get_reminder(self.conn, rid)
        self.assertEqual((rem["rrule"], rem["recurrence_end"]), (recurrence.ONCE, None))
        r = store.list_reminders(self.conn, datetime(2026, 11, 1, 9, 0))[0]
        self.assertEqual(r["description"], "Once, on Thu, Nov 5, 2026")
        self.assertTrue(r["once"])
        self.assertEqual(r["next_at"], datetime(2026, 11, 5, 6, 0))
        self.assertEqual(store.reminder_to_input(rem).repeat, "none")

    def test_shows_on_its_day_only_without_carry_over(self):
        self.once(appear_time="08:00")
        self.assertEqual(self.showing("2026-11-05T07:59"), [])
        self.assertEqual(self.showing("2026-11-05T08:00"), ["Call the plumber about the estimate"])
        self.assertEqual(self.showing("2026-11-06T09:00"), [])
        self.assertEqual(self.showing("2026-11-12T09:00"), [])   # never comes back

    def test_carry_over_waits_until_done_then_gone_for_good(self):
        rid = self.once(carry_over=True)
        self.assertEqual(self.showing("2026-11-09T09:00"), ["Call the plumber about the estimate"])
        self.assertTrue(store.mark_reminder_done(self.conn, rid, datetime(2026, 11, 9, 9, 5)))
        self.assertEqual(self.showing("2026-11-09T09:06"), [])
        self.assertEqual(self.showing("2027-11-05T09:00"), [])
        r = store.list_reminders(self.conn, datetime(2026, 11, 9, 9, 6))[0]
        self.assertIsNone(r["next_at"])
        # Undo puts it back.
        self.assertTrue(store.undo_reminder_done(self.conn, rid))
        self.assertEqual(self.showing("2026-11-09T09:07"), ["Call the plumber about the estimate"])

    def test_on_the_display_and_ticked_from_it(self):
        self.once(carry_over=True)
        client = TestClient(create_app(self.db_path))
        rems = client.get("/api/v1/display?now=2026-11-06T09:00").json()["reminders"]
        self.assertEqual([(r["text"], r["due"]) for r in rems], [("Call the plumber about the estimate", "2026-11-05")])
        self.assertTrue(client.post("/api/v1/reminders/1/done?now=2026-11-06T09:01").json()["ok"])
        self.assertEqual(client.get("/api/v1/display?now=2026-11-06T09:02").json()["reminders"], [])

    def test_editor_offers_doesnt_repeat_and_shows_done(self):
        client = TestClient(create_app(self.db_path))
        self.assertIn('<option value="none"', client.get("/reminders/new").text)
        r = client.post("/reminders/new", data={"text": "Return the library books", "start_date": "2026-11-05",
                                                "repeat": "none", "carry_over": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        store.mark_reminder_done(self.conn, 1, datetime(2026, 11, 5, 9, 0))
        page = client.get("/reminders").text
        self.assertIn("Once, on Thu, Nov 5, 2026", page)
        self.assertIn('<span class="pill">Done</span>', page)
        self.assertIn('<option value="none" selected', client.get("/reminders/1/edit").text)


class TestEditorFlows(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_category_form_saves_units_and_all_day_days(self):
        cid = self.cat("Family")
        r = self.client.post(f"/categories/{cid}/edit", data={
            "name": "Family", "color": "#46A758", "default_reminder": "1", "default_unit": "days", "allday_days": "2"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        cat = store.get_category(self.conn, cid)
        self.assertEqual((cat["default_reminder_minutes"], cat["default_allday_days"]), (1440, 2))
        page = self.client.get(f"/categories/{cid}/edit").text
        self.assertIn('name="allday_days"', page)
        self.assertIn('<option value="days" selected>', page)
        listing = self.client.get("/categories").text
        self.assertIn("all-day: 2 days before", listing)
        r = self.client.post("/categories", data={"name": "Pets", "color": "#123456", "default_reminder": "",
                                                  "default_unit": "minutes", "allday_days": "99"})
        self.assertEqual(r.status_code, 400)

    def test_event_form_reminder_unit(self):
        r = self.client.post("/events/new", data={
            "title": "Checkup", "start_date": "2026-11-10", "start_time": "09:00", "end_date": "2026-11-10",
            "end_time": "10:00", "category_id": str(self.cat("Medical")), "people": str(self.alex),
            "reminder_mode": "custom", "reminder_minutes": "1", "reminder_unit": "days"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        eid = self.conn.execute("SELECT id FROM events WHERE title = 'Checkup'").fetchone()[0]
        self.assertEqual(store.get_event(self.conn, eid)["reminder_minutes"], 1440)
        page = self.client.get(f"/events/{eid}/edit").text
        self.assertIn('name="reminder_unit"', page)
        self.assertIn('<option value="days" selected>', page)

    def test_occurrence_page_offers_three_choices(self):
        eid = self.add(title="Coffee", start_date="2026-10-02", end_date="2026-10-02", start_time="10:00",
                       end_time="11:00", repeat="weekly", weekdays=["FR"])
        page = self.client.get(f"/events/{eid}/occurrence/2026-11-06").text
        for text in ("Change this date", "Skip this date", "Change from this date on", "Delete from this date on",
                     "Edit the whole series", "Delete the whole series"):
            self.assertIn(text, page)

    def test_change_future_with_confirmation_of_dropped_dates(self):
        eid = self.add(title="Coffee", start_date="2026-10-02", end_date="2026-10-02", start_time="10:00",
                       end_time="11:00", repeat="weekly", weekdays=["FR"])
        store.skip_occurrence(self.conn, eid, "2026-11-20")
        url = f"/events/{eid}/occurrence/2026-11-06/edit-future"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Change this and later dates", page.text)
        self.assertIn('value="2026-11-06"', page.text)
        form = {"title": "Coffee", "start_date": "2026-11-05", "start_time": "10:00", "end_date": "2026-11-05",
                "end_time": "11:00", "category_id": str(self.cat("Medical")), "people": str(self.alex),
                "reminder_mode": "default", "repeat": "weekly", "interval": "1", "weekdays": "TH"}
        r = self.client.post(url, data=form)
        self.assertEqual(r.status_code, 400)       # starts before Nov 6
        form.update(start_date="2026-11-12", end_date="2026-11-12")
        r = self.client.post(url, data=form)
        self.assertEqual(r.status_code, 200)       # asks first: the skipped Nov 20 goes back to normal
        self.assertIn("Save anyway", r.text)
        self.assertIn("Fri, Nov 20, 2026 (skipped)", r.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        r = self.client.post(url, data=dict(form, confirm="1"), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_delete_future_flow(self):
        eid = self.add(title="Coffee", start_date="2026-10-02", end_date="2026-10-02", start_time="10:00",
                       end_time="11:00", repeat="weekly", weekdays=["FR"])
        url = f"/events/{eid}/occurrence/2026-11-06/delete-future"
        page = self.client.get(url).text
        self.assertIn("Delete from Fri, Nov 6, 2026 on", page)
        r = self.client.post(url, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(store.get_event(self.conn, eid)["recurrence_end"], "2026-11-05")


if __name__ == "__main__":
    unittest.main()
