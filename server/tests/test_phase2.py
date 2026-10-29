"""Phase 2 tests: repeating events and household "I did it" reminders.

Plain Python (unittest). All data is fictional.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, recurrence, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.pid = store.save_person(self.conn, "Alex", "#4C9AFF")
        self.cat = self.conn.execute("SELECT id FROM categories WHERE name = 'Social'").fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, **kw):
        base = dict(title="Coffee", start_date="2026-10-30", start_time="10:00", end_date="2026-10-30",
                    end_time="11:00", category_id=str(self.cat), person_ids=[str(self.pid)])
        base.update(kw)
        return store.save_event(self.conn, store.EventInput(**base))

    def dates(self, title, lo, hi):
        return [e["start_local"] for e in store.list_events(self.conn, lo, hi) if e["title"] == title]


class TestPatterns(unittest.TestCase):
    def rule(self, **kw):
        errors = {}
        r = recurrence.build_rrule(recurrence.Pattern(**kw), date(2026, 10, 30), errors)
        return r, errors

    def test_build_each_pattern(self):
        self.assertEqual(self.rule(repeat="weekly", weekdays=["FR"])[0], "FREQ=WEEKLY;BYDAY=FR")
        self.assertEqual(self.rule(repeat="weekly", interval="2", weekdays=["TH", "MO"])[0],
                         "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH")
        self.assertEqual(self.rule(repeat="monthly_nth", ordinals=["5", "2", "4", "3"], nth_weekday="FR")[0],
                         "FREQ=MONTHLY;BYDAY=2FR,3FR,4FR,5FR")
        self.assertEqual(self.rule(repeat="monthly_nth", ordinals=["-1"], nth_weekday="TU")[0],
                         "FREQ=MONTHLY;BYDAY=-1TU")
        self.assertEqual(self.rule(repeat="monthly_date")[0], "FREQ=MONTHLY;BYMONTHDAY=30")
        self.assertEqual(self.rule(repeat="yearly")[0], "FREQ=YEARLY")
        self.assertIsNone(self.rule(repeat="none")[0])

    def test_incomplete_patterns_rejected(self):
        self.assertIn("repeat", self.rule(repeat="weekly", weekdays=[])[1])
        self.assertIn("repeat", self.rule(repeat="monthly_nth", ordinals=[], nth_weekday="FR")[1])
        self.assertIn("repeat", self.rule(repeat="weekly", interval="3", weekdays=["MO"])[1])

    def test_round_trip_back_to_form(self):
        start = date(2026, 10, 30)
        for rule, kind in [("FREQ=WEEKLY;BYDAY=FR", "weekly"), ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH", "weekly"),
                           ("FREQ=MONTHLY;BYDAY=2FR,3FR,4FR,5FR", "monthly_nth"),
                           ("FREQ=MONTHLY;BYMONTHDAY=30", "monthly_date"), ("FREQ=YEARLY", "yearly")]:
            p = recurrence.pattern_from_rrule(rule, None, start)
            self.assertEqual(p.repeat, kind, rule)
            self.assertEqual(recurrence.build_rrule(p, start, {}), rule)

    def test_google_style_rules(self):
        start = date(2026, 7, 13)
        # Google adds WKST and INTERVAL=1; still one of the editor's patterns.
        p = recurrence.pattern_from_rrule("FREQ=WEEKLY;WKST=MO;INTERVAL=1", None, start)
        self.assertEqual((p.repeat, p.weekdays), ("weekly", ["MO"]))
        # A rule with UNTIL inside it is kept as-is ("set elsewhere").
        p = recurrence.pattern_from_rrule("FREQ=WEEKLY;UNTIL=20261231;BYDAY=FR", None, start)
        self.assertEqual(p.repeat, "custom")
        self.assertEqual(recurrence.describe("FREQ=WEEKLY;UNTIL=20261231;BYDAY=FR", start), "Repeats (set elsewhere)")

    def test_descriptions(self):
        d = date(2026, 10, 30)
        self.assertEqual(recurrence.describe("FREQ=MONTHLY;BYDAY=2FR,3FR,4FR,5FR", d),
                         "Every month on the 2nd, 3rd, 4th and 5th Friday")
        self.assertEqual(recurrence.describe("FREQ=MONTHLY;BYDAY=1TU", d), "Every month on the 1st Tuesday")
        self.assertEqual(recurrence.describe("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH", d),
                         "Every other week on Monday and Thursday")
        self.assertEqual(recurrence.describe("FREQ=YEARLY", date(2026, 3, 4), "2030-12-31"),
                         "Every year on March 4, until Dec 31, 2030")
        self.assertIn("skipped", recurrence.describe("FREQ=MONTHLY;BYMONTHDAY=31", date(2026, 1, 31)))
        self.assertEqual(recurrence.describe(None, d), "Doesn't repeat")


def _rep(**kw):
    """Repeat fields for EventInput."""
    return kw


class TestRepeatingEvents(Base):
    def test_coffee_and_breakfast_never_collide(self):
        self.add(title="Coffee", repeat="monthly_nth", ordinals=["2", "3", "4", "5"], nth_weekday="FR")
        self.add(title="Breakfast", start_time="09:30", end_time="10:30",
                 repeat="monthly_nth", ordinals=["1"], nth_weekday="FR")
        lo, hi = date(2026, 10, 30), date(2028, 10, 29)
        coffee = {s[:10] for s in self.dates("Coffee", lo, hi)}
        breakfast = {s[:10] for s in self.dates("Breakfast", lo, hi)}
        self.assertFalse(coffee & breakfast)
        fridays = {(lo + timedelta(days=i)).isoformat() for i in range((hi - lo).days + 1)
                   if (lo + timedelta(days=i)).weekday() == 4}
        self.assertEqual(coffee | breakfast, fridays, "together they cover every Friday")
        for d in breakfast:
            self.assertLessEqual(int(d[8:]), 7, "breakfast is always the 1st Friday")
        self.assertTrue(all(s.endswith("T09:30") for s in self.dates("Breakfast", lo, hi)))

    def test_fifth_and_last_weekdays(self):
        self.add(title="Fifth", start_date="2026-01-01", end_date="2026-01-01",
                 repeat="monthly_nth", ordinals=["5"], nth_weekday="FR")
        self.add(title="Last", start_date="2026-01-01", end_date="2026-01-01",
                 repeat="monthly_nth", ordinals=["-1"], nth_weekday="TU")
        fifth = [s[:10] for s in self.dates("Fifth", date(2026, 1, 1), date(2026, 12, 31))]
        self.assertEqual(fifth, ["2026-01-30", "2026-05-29", "2026-07-31", "2026-10-30"])
        last = [s[:10] for s in self.dates("Last", date(2026, 2, 1), date(2026, 4, 30))]
        self.assertEqual(last, ["2026-02-24", "2026-03-31", "2026-04-28"])

    def test_every_other_week_and_two_days(self):
        self.add(title="Alt", start_date="2026-11-02", end_date="2026-11-02",
                 repeat="weekly", interval="2", weekdays=["MO", "TH"])
        got = [s[:10] for s in self.dates("Alt", date(2026, 11, 1), date(2026, 11, 30))]
        self.assertEqual(got, ["2026-11-02", "2026-11-05", "2026-11-16", "2026-11-19", "2026-11-30"])

    def test_monthly_on_31st_skips_short_months(self):
        self.add(title="Bill", all_day=True, start_date="2026-10-31", end_date="2026-10-31",
                 start_time="", end_time="", repeat="monthly_date")
        got = [s for s in self.dates("Bill", date(2026, 10, 1), date(2027, 3, 31))]
        self.assertEqual(got, ["2026-10-31", "2026-12-31", "2027-01-31", "2027-03-31"])

    def test_yearly_all_day_birthday(self):
        self.add(title="Birthday", all_day=True, start_date="1990-03-04", end_date="1990-03-04",
                 start_time="", end_time="", repeat="yearly")
        got = self.dates("Birthday", date(2026, 1, 1), date(2028, 12, 31))
        self.assertEqual(got, ["2026-03-04", "2027-03-04", "2028-03-04"])

    def test_wall_clock_across_dst(self):
        self.add(title="Net", start_date="2026-10-26", end_date="2026-10-26", repeat="weekly", weekdays=["MO"])
        got = self.dates("Net", date(2026, 10, 26), date(2027, 3, 22))
        self.assertTrue(all(s.endswith("T10:00") for s in got))
        by_day = {s[:10]: s for s in got}
        self.assertEqual(timeutil.to_aware(by_day["2026-10-26"]).utcoffset(), timedelta(hours=-4))
        self.assertEqual(timeutil.to_aware(by_day["2026-11-02"]).utcoffset(), timedelta(hours=-5))
        self.assertEqual(timeutil.to_aware(by_day["2027-03-15"]).utcoffset(), timedelta(hours=-4))

    def test_end_date_is_inclusive(self):
        self.add(title="Class", start_date="2026-11-02", end_date="2026-11-02",
                 repeat="weekly", weekdays=["MO"], until="2026-11-16")
        got = [s[:10] for s in self.dates("Class", date(2026, 11, 1), date(2026, 12, 31))]
        self.assertEqual(got, ["2026-11-02", "2026-11-09", "2026-11-16"])
        with self.assertRaises(store.ValidationError) as cm:
            self.add(title="Bad", start_date="2026-11-02", repeat="weekly", weekdays=["MO"], until="2026-10-01")
        self.assertIn("until", cm.exception.errors)

    def test_multi_day_occurrence_overlapping_window(self):
        self.add(title="Trip", all_day=True, start_date="2026-11-06", end_date="2026-11-08",
                 start_time="", end_time="", repeat="monthly_nth", ordinals=["1"], nth_weekday="FR")
        got = store.list_events(self.conn, date(2026, 11, 7), date(2026, 11, 7))
        self.assertEqual([(e["start_local"], e["end_local"]) for e in got], [("2026-11-06", "2026-11-08")])

    def test_people_on_every_occurrence(self):
        sam = store.save_person(self.conn, "Sam", "#F06BA8")
        self.add(person_ids=[str(self.pid), str(sam)], repeat="weekly", weekdays=["FR"])
        for e in store.list_events(self.conn, date(2026, 10, 30), date(2026, 11, 30)):
            self.assertEqual([p["name"] for p in e["people"]], ["Alex", "Sam"])

    def test_skip_and_restore(self):
        eid = self.add(repeat="weekly", weekdays=["FR"])
        store.skip_occurrence(self.conn, eid, "2026-11-06")
        got = [s[:10] for s in self.dates("Coffee", date(2026, 11, 1), date(2026, 11, 14))]
        self.assertEqual(got, ["2026-11-13"])
        self.assertTrue(store.get_occurrence(self.conn, eid, "2026-11-06")["skipped"])
        store.restore_occurrence(self.conn, eid, "2026-11-06")
        got = [s[:10] for s in self.dates("Coffee", date(2026, 11, 1), date(2026, 11, 14))]
        self.assertEqual(got, ["2026-11-06", "2026-11-13"])

    def test_change_one_occurrence(self):
        eid = self.add(location="Cafe A", repeat="weekly", weekdays=["FR"])
        event = store.get_event(self.conn, eid)
        store.change_occurrence(self.conn, event, "2026-11-06", store.OccurrenceInput(
            title="Coffee (moved)", location="Cafe B", start_date="2026-11-06", start_time="09:30",
            end_date="2026-11-06", end_time="10:30"))
        items = store.list_events(self.conn, date(2026, 11, 1), date(2026, 11, 14))
        self.assertEqual([(e["title"], e["start_local"], e["location"], e["changed"]) for e in items], [
            ("Coffee (moved)", "2026-11-06T09:30", "Cafe B", True),
            ("Coffee", "2026-11-13T10:00", "Cafe A", False),
        ])

    def test_changed_occurrence_can_move_to_another_day(self):
        eid = self.add(repeat="weekly", weekdays=["FR"])
        event = store.get_event(self.conn, eid)
        store.change_occurrence(self.conn, event, "2026-11-06", store.OccurrenceInput(
            title="Coffee", start_date="2026-11-09", start_time="10:00", end_date="2026-11-09", end_time="11:00"))
        self.assertEqual(self.dates("Coffee", date(2026, 11, 9), date(2026, 11, 9)), ["2026-11-09T10:00"])
        self.assertEqual(self.dates("Coffee", date(2026, 11, 6), date(2026, 11, 6)), [])

    def test_series_edit_keeps_exceptions(self):
        eid = self.add(repeat="weekly", weekdays=["FR"])
        store.skip_occurrence(self.conn, eid, "2026-11-06")
        store.save_event(self.conn, store.EventInput(
            title="Retirees coffee", start_date="2026-10-30", start_time="10:00", end_date="2026-10-30",
            end_time="11:00", category_id=str(self.cat), person_ids=[str(self.pid)],
            repeat="weekly", weekdays=["FR"]), eid)
        got = [s[:10] for s in self.dates("Retirees coffee", date(2026, 11, 1), date(2026, 11, 14))]
        self.assertEqual(got, ["2026-11-13"])

    def test_turning_repeats_off_clears_exceptions(self):
        eid = self.add(repeat="weekly", weekdays=["FR"])
        store.skip_occurrence(self.conn, eid, "2026-11-06")
        self.add(repeat="none")  # unrelated event, unaffected
        store.save_event(self.conn, store.EventInput(
            title="Coffee", start_date="2026-10-30", start_time="10:00", end_date="2026-10-30", end_time="11:00",
            category_id=str(self.cat), person_ids=[str(self.pid)], repeat="none"), eid)
        self.assertEqual(store.get_exceptions(self.conn, eid), {})
        self.assertIsNone(store.get_event(self.conn, eid)["rrule"])

    def test_occurrence_must_belong_to_series(self):
        eid = self.add(repeat="weekly", weekdays=["FR"])
        self.assertIsNone(store.get_occurrence(self.conn, eid, "2026-11-07"))  # a Saturday
        self.assertIsNotNone(store.get_occurrence(self.conn, eid, "2026-11-06"))

    def test_rule_set_elsewhere_still_expands(self):
        eid = self.add()
        self.conn.execute("UPDATE events SET rrule = ? WHERE id = ?",
                          ("FREQ=WEEKLY;WKST=SU;UNTIL=20261121T045959Z;BYDAY=FR", eid))
        self.conn.commit()
        got = [s[:10] for s in self.dates("Coffee", date(2026, 10, 1), date(2026, 12, 31))]
        self.assertEqual(got, ["2026-10-30", "2026-11-06", "2026-11-13", "2026-11-20"])
        form = store.event_to_input(store.get_event(self.conn, eid))
        self.assertEqual(form.repeat, "custom")
        # Saving the form unchanged keeps the rule as it was.
        store.save_event(self.conn, form, eid)
        self.assertEqual(store.get_event(self.conn, eid)["rrule"], "FREQ=WEEKLY;WKST=SU;UNTIL=20261121T045959Z;BYDAY=FR")


class TestReminders(Base):
    def rem(self, **kw):
        base = dict(text="Garbage cans to the curb", start_date="2026-11-02", repeat="weekly", weekdays=["MO"])
        base.update(kw)
        return store.save_reminder(self.conn, store.ReminderInput(**base))

    def showing(self, when: str):
        return [r["text"] for r in store.reminders_showing(self.conn, datetime.fromisoformat(when))]

    def test_garbage_shows_monday_from_end_of_dark_hours_only(self):
        self.rem()
        self.assertEqual(self.showing("2026-11-02T05:59"), [])
        self.assertEqual(self.showing("2026-11-02T06:00"), ["Garbage cans to the curb"])
        self.assertEqual(self.showing("2026-11-02T23:59"), ["Garbage cans to the curb"])
        self.assertEqual(self.showing("2026-11-03T00:00"), [], "carry-over off: gone at the end of Monday")
        self.assertEqual(self.showing("2026-11-09T06:00"), ["Garbage cans to the curb"])

    def test_appear_time_follows_dark_hours_setting(self):
        self.rem()
        cfg = store.get_config(self.conn)
        cfg["dark_end"] = "07:00"
        store.save_config(self.conn, cfg)
        self.assertEqual(self.showing("2026-11-02T06:30"), [])
        self.assertEqual(self.showing("2026-11-02T07:00"), ["Garbage cans to the curb"])

    def test_own_appear_time(self):
        self.rem(appear_time="19:00")
        self.assertEqual(self.showing("2026-11-02T18:59"), [])
        self.assertEqual(self.showing("2026-11-02T19:00"), ["Garbage cans to the curb"])

    def test_i_did_it_and_undo(self):
        rid = self.rem()
        monday = datetime(2026, 11, 2, 7, 15)
        self.assertTrue(store.mark_reminder_done(self.conn, rid, monday))
        self.assertEqual(self.showing("2026-11-02T08:00"), [])
        self.assertEqual(self.showing("2026-11-09T06:00"), ["Garbage cans to the curb"], "back next week")
        self.assertTrue(store.undo_reminder_done(self.conn, rid))
        self.assertEqual(self.showing("2026-11-02T08:00"), ["Garbage cans to the curb"])
        self.assertIsNone(store.get_reminder(self.conn, rid)["last_done_at"])

    def test_undo_restores_the_previous_done(self):
        rid = self.rem()
        store.mark_reminder_done(self.conn, rid, datetime(2026, 11, 2, 7, 0))
        store.mark_reminder_done(self.conn, rid, datetime(2026, 11, 9, 7, 0))
        store.undo_reminder_done(self.conn, rid)
        r = store.get_reminder(self.conn, rid)
        self.assertEqual((r["last_done_occ"], r["last_done_at"]), ("2026-11-02", "2026-11-02T07:00"))

    def test_done_when_not_showing_does_nothing(self):
        rid = self.rem()
        self.assertFalse(store.mark_reminder_done(self.conn, rid, datetime(2026, 11, 3, 9, 0)))
        self.assertIsNone(store.get_reminder(self.conn, rid)["last_done_at"])

    def test_carry_over_stays_until_done_and_never_stacks(self):
        rid = self.rem(text="Change A/C filters", repeat="monthly_date", start_date="2026-11-01", carry_over=True)
        self.assertEqual(self.showing("2026-11-01T06:00"), ["Change A/C filters"])
        self.assertEqual(self.showing("2026-11-20T12:00"), ["Change A/C filters"], "still there weeks later")
        self.assertEqual(self.showing("2026-12-05T12:00"), ["Change A/C filters"], "one item, not two")
        store.mark_reminder_done(self.conn, rid, datetime(2026, 12, 5, 12, 0))
        self.assertEqual(self.showing("2026-12-20T12:00"), [])
        self.assertEqual(self.showing("2027-01-01T06:00"), ["Change A/C filters"])

    def test_paused_and_future_reminders_hidden(self):
        self.rem(active=False)
        self.rem(text="Later", start_date="2026-12-01", repeat="monthly_date")
        self.assertEqual(self.showing("2026-11-02T09:00"), [])
        self.assertEqual(self.showing("2026-12-01T09:00"), ["Later"])

    def test_status_details(self):
        self.rem()
        r = store.list_reminders(self.conn, datetime(2026, 11, 3, 9, 0))[0]
        self.assertFalse(r["showing"])
        self.assertEqual(r["next_at"], datetime(2026, 11, 9, 6, 0))
        self.assertEqual(r["description"], "Every week on Monday")
        self.assertEqual(r["appears"], "6:00 AM")

    def test_validation(self):
        with self.assertRaises(store.ValidationError) as cm:
            self.rem(text="", repeat="sometimes", appear_time="25:00")
        self.assertEqual(set(cm.exception.errors), {"text", "repeat", "appear_time"})
        with self.assertRaises(store.ValidationError):
            self.rem(text="x" * 81)


class TestMigrationFromPhase1(unittest.TestCase):
    def test_existing_data_survives_and_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            conn = sqlite3.connect(path)
            first = db.available_migrations()[0][1].read_text(encoding="utf-8")
            conn.executescript(first)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-28T11:00:00')")
            conn.execute("INSERT INTO people (name, color) VALUES ('Alex', '#4C9AFF')")
            conn.execute("INSERT INTO events (title, start_local, end_local, category_id, created, updated)"
                         " VALUES ('Dentist', '2026-11-10T14:30', '2026-11-10T15:30', 1, 'x', 'x')")
            conn.commit()
            conn.close()
            # Applies 0002 and every later migration; one backup is written first.
            self.assertEqual(db.migrate(path)[0], 2)
            self.assertEqual(len(list(Path(tmp).glob("calendar.db.pre-v1-*.bak"))), 1)
            c = db.connect(path)
            self.assertEqual(c.execute("SELECT title FROM events").fetchone()[0], "Dentist")
            self.assertEqual(store.list_reminders(c), [])
            c.close()


class TestEditorPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        self.client = TestClient(create_app(self.db_path))
        self.client.post("/people", data={"name": "Alex", "color": "#4C9AFF"})
        self.today = timeutil.now_local().date()

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_pages_load(self):
        for path in ("/reminders", "/reminders/new", "/events/new"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        self.assertIn('name="repeat"', self.client.get("/events/new").text)
        self.assertIn('aria-current="page">Reminders</a>', self.client.get("/reminders").text)

    def test_repeating_event_flow(self):
        c = self.client
        start = self.today + timedelta(days=1)
        wd = recurrence.WEEKDAYS[start.weekday()]
        form = {"title": "Club net", "start_date": start.isoformat(), "start_time": "19:30",
                "end_date": start.isoformat(), "end_time": "20:30", "category_id": "6", "people": ["1"],
                "reminder_mode": "default", "repeat": "weekly", "interval": "1", "weekdays": [wd]}
        r = c.post("/events/new", data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.text.count('<span class="title">Club net</span>'), (52, 53), "a year of weekly occurrences")
        self.assertIn("Repeats", r.text)

        occ = start.isoformat()
        r = c.get(f"/events/1/occurrence/{occ}")
        self.assertIn("Every week on", r.text)
        self.assertIn("Skip this date", r.text)

        r = c.post(f"/events/1/occurrence/{occ}/skip", follow_redirects=True)
        self.assertIn("Skipped", r.text)
        r = c.get("/events/1/edit")
        self.assertIn("Edit the whole series", r.text)
        self.assertIn("skipped", r.text)
        r = c.post(f"/events/1/occurrence/{occ}/restore", follow_redirects=True)
        self.assertIn("back to normal", r.text)

        r = c.get(f"/events/1/occurrence/{occ}/edit")
        self.assertIn('value="Club net"', r.text)
        r = c.post(f"/events/1/occurrence/{occ}/edit", data={
            "title": "Club net (special)", "start_date": occ, "start_time": "20:00",
            "end_date": occ, "end_time": "21:00", "location": "", "notes": ""}, follow_redirects=True)
        self.assertIn("Club net (special)", r.text)
        self.assertIn("Changed this time", r.text)

        r = c.get("/events/1/delete")
        self.assertIn("Delete the whole series?", r.text)

        bad = dict(form, weekdays=[])
        r = c.post("/events/new", data=bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Tick at least one day of the week.", r.text)

    def test_unknown_occurrence_redirects(self):
        r = self.client.get("/events/99/occurrence/2026-11-06", follow_redirects=True)
        self.assertIn("no longer part of the series", r.text)

    def test_reminder_flow(self):
        c = self.client
        every_day = recurrence.WEEKDAYS
        r = c.post("/reminders/new", data={"text": "Water the plants", "start_date": self.today.isoformat(),
                                            "appear_time": "00:00", "repeat": "weekly", "interval": "1",
                                            "weekdays": every_day, "carry_over": "0"}, follow_redirects=True)
        self.assertIn("Water the plants", r.text)
        self.assertIn("Showing now", r.text)
        r = c.post("/reminders/1/done", follow_redirects=True)
        self.assertIn("Done:", r.text)
        self.assertIn("/reminders/1/undo", r.text)
        self.assertNotIn("Showing now", r.text)
        r = c.post("/reminders/1/undo", follow_redirects=True)
        self.assertIn("is back", r.text)
        self.assertIn("Showing now", r.text)

        r = c.post("/reminders/1/edit", data={"text": "Water the plants", "start_date": self.today.isoformat(),
                                              "repeat": "weekly", "interval": "1", "weekdays": every_day,
                                              "carry_over": "1", "paused": "1"}, follow_redirects=True)
        self.assertIn("Paused", r.text)
        r = c.post("/reminders/new", data={"text": "", "start_date": "", "repeat": "weekly", "weekdays": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Enter the reminder.", r.text)
        r = c.post("/reminders/1/delete", follow_redirects=True)
        self.assertIn("Deleted", r.text)


if __name__ == "__main__":
    unittest.main()
