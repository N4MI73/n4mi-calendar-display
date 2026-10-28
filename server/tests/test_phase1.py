"""Phase 1 tests: database, rules and editor pages.

Plain Python (unittest). Run from the server folder:
    python -m unittest discover -s tests -v

All data here is fictional.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, time, timedelta
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"  # don't create the module-level app at import

from fastapi.testclient import TestClient  # noqa: E402

from app import db, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "data", "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # helpers
    def person(self, name="Alex", color="#4C9AFF"):
        return store.save_person(self.conn, name, color)

    def category_id(self, name="Medical"):
        return self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]

    def event_input(self, **kw):
        base = dict(
            title="Dentist",
            start_date="2026-11-10",
            start_time="14:30",
            end_date="2026-11-10",
            end_time="15:30",
            category_id=str(self.category_id()),
            person_ids=[str(self.pid)] if hasattr(self, "pid") else [],
        )
        base.update(kw)
        return store.EventInput(**base)


class TestMigrations(TempDB):
    def test_fresh_database_has_schema_and_seeds(self):
        self.assertEqual(db.schema_version(self.conn), 1)
        names = [r["name"] for r in store.list_categories(self.conn)]
        self.assertEqual(
            names, ["Medical", "Family", "Home", "Social", "Travel", "Club / Ham radio", "Info", "Other"]
        )
        self.assertEqual(store.list_people(self.conn), [], "no people are seeded")
        cfg = store.get_config(self.conn)
        self.assertEqual(cfg["upcoming_days"], "14")
        self.assertEqual(cfg["dark_start"], "23:00")
        self.assertEqual(cfg["wake_seconds"], "120")

    def test_rerunning_migrations_changes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        self.assertEqual(db.migrate(self.db_path), [])
        after = self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(db.schema_version(self.conn), 1)

    def test_info_category_has_no_default_reminder(self):
        info = self.conn.execute("SELECT * FROM categories WHERE name = 'Info'").fetchone()
        self.assertIsNone(info["default_reminder_minutes"])
        medical = self.conn.execute("SELECT * FROM categories WHERE name = 'Medical'").fetchone()
        self.assertEqual(medical["default_reminder_minutes"], 120)

    def test_backup_written_before_a_new_migration(self):
        # Pretend a future migration exists and check the pre-migration copy is made.
        mig_dir = Path(db.MIGRATIONS_DIR)
        extra = mig_dir / "0999_test_only.sql"
        extra.write_text("CREATE TABLE test_only (x INTEGER);", encoding="utf-8")
        try:
            applied = db.migrate(self.db_path)
            self.assertEqual(applied, [999])
            backups = list(Path(self.db_path).parent.glob("calendar.db.pre-v1-*.bak"))
            self.assertEqual(len(backups), 1)
            check = sqlite3.connect(backups[0])
            self.assertEqual(check.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 8)
            check.close()
        finally:
            extra.unlink()


class TestEvents(TempDB):
    def setUp(self):
        super().setUp()
        self.pid = self.person()

    def test_create_edit_delete(self):
        eid = store.save_event(self.conn, self.event_input(location="Dr. Example, 1 Test St"))
        ev = store.get_event(self.conn, eid)
        self.assertEqual(ev["title"], "Dentist")
        self.assertEqual(ev["start_local"], "2026-11-10T14:30")
        self.assertEqual(ev["location"], "Dr. Example, 1 Test St")
        self.assertEqual([p["name"] for p in ev["people"]], ["Alex"])

        store.save_event(self.conn, self.event_input(title="Dentist (moved)", start_time="15:00", end_time="16:00"), eid)
        ev = store.get_event(self.conn, eid)
        self.assertEqual(ev["title"], "Dentist (moved)")
        self.assertEqual(ev["start_local"], "2026-11-10T15:00")

        store.delete_event(self.conn, eid)
        self.assertIsNone(store.get_event(self.conn, eid))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_people").fetchone()[0], 0)

    def test_required_fields(self):
        with self.assertRaises(store.ValidationError) as cm:
            store.save_event(self.conn, self.event_input(title="  ", person_ids=[], category_id="", start_time=""))
        errs = cm.exception.errors
        for key in ("title", "people", "category", "start_time"):
            self.assertIn(key, errs)

    def test_end_before_start_rejected(self):
        with self.assertRaises(store.ValidationError) as cm:
            store.save_event(self.conn, self.event_input(start_time="15:00", end_time="14:00"))
        self.assertIn("end_time", cm.exception.errors)
        with self.assertRaises(store.ValidationError) as cm:
            store.save_event(self.conn, self.event_input(all_day=True, start_date="2026-11-10", end_date="2026-11-09"))
        self.assertIn("end_date", cm.exception.errors)

    def test_blank_end_time_means_one_hour(self):
        eid = store.save_event(self.conn, self.event_input(end_time=""))
        self.assertEqual(store.get_event(self.conn, eid)["end_local"], "2026-11-10T15:30")

    def test_event_may_cross_midnight(self):
        eid = store.save_event(
            self.conn, self.event_input(start_time="22:00", end_date="2026-11-11", end_time="01:00")
        )
        ev = store.get_event(self.conn, eid)
        self.assertEqual(ev["end_local"], "2026-11-11T01:00")

    def test_all_day_and_multi_day(self):
        eid = store.save_event(
            self.conn, self.event_input(all_day=True, start_date="2026-11-12", end_date="2026-11-14", start_time="", end_time="")
        )
        ev = store.get_event(self.conn, eid)
        self.assertEqual((ev["start_local"], ev["end_local"]), ("2026-11-12", "2026-11-14"))
        # Shows up on every day it covers.
        on_13th = store.list_events(self.conn, date(2026, 11, 13), date(2026, 11, 13))
        self.assertEqual([e["id"] for e in on_13th], [eid])
        on_15th = store.list_events(self.conn, date(2026, 11, 15), date(2026, 11, 15))
        self.assertEqual(on_15th, [])

    def test_list_is_in_day_order_with_all_day_first(self):
        later_trip = store.save_event(self.conn, self.event_input(
            title="Trip", all_day=True, start_date="2026-11-12", end_date="2026-11-13", start_time="", end_time=""))
        evening = store.save_event(self.conn, self.event_input(title="Evening", start_date="2026-11-10",
                                                               start_time="19:00", end_time="20:00"))
        morning = store.save_event(self.conn, self.event_input(title="Morning", start_date="2026-11-10",
                                                               start_time="08:00", end_time="09:00"))
        holiday = store.save_event(self.conn, self.event_input(
            title="Holiday", all_day=True, start_date="2026-11-10", end_date="2026-11-10", start_time="", end_time=""))
        got = [e["id"] for e in store.list_events(self.conn, date(2026, 11, 1), date(2026, 11, 30))]
        self.assertEqual(got, [holiday, morning, evening, later_trip])

    def test_location_is_optional(self):
        eid = store.save_event(self.conn, self.event_input(location=""))
        self.assertIsNone(store.get_event(self.conn, eid)["location"])

    def test_wall_clock_time_survives_dst_changes(self):
        # 1 Nov 2026: clocks go back. 14 Mar 2027: clocks go forward.
        a = store.save_event(self.conn, self.event_input(start_date="2026-11-01", end_date="2026-11-01"))
        b = store.save_event(self.conn, self.event_input(start_date="2027-03-14", end_date="2027-03-14"))
        before = store.save_event(self.conn, self.event_input(start_date="2026-10-31", end_date="2026-10-31"))
        for eid in (a, b, before):
            ev = store.get_event(self.conn, eid)
            self.assertTrue(ev["start_local"].endswith("T14:30"))
            self.assertEqual(timeutil.fmt_time(timeutil.parse_local(ev["start_local"])), "2:30 PM")
        # The same wall-clock time maps to the correct UTC offset either side of the change.
        self.assertEqual(timeutil.to_aware(store.get_event(self.conn, before)["start_local"]).utcoffset(), timedelta(hours=-4))
        self.assertEqual(timeutil.to_aware(store.get_event(self.conn, a)["start_local"]).utcoffset(), timedelta(hours=-5))
        self.assertEqual(timeutil.to_aware(store.get_event(self.conn, b)["start_local"]).utcoffset(), timedelta(hours=-4))


class TestPeople(TempDB):
    def test_one_or_more_people(self):
        a = self.person("Alex", "#4C9AFF")
        b = self.person("Sam", "#F06BA8")
        c = self.person("Jordan", "#2BB3A3")
        self.pid = a
        eid = store.save_event(self.conn, self.event_input(person_ids=[str(a), str(c)]))
        self.assertEqual({p["name"] for p in store.get_event(self.conn, eid)["people"]}, {"Alex", "Jordan"})
        store.save_event(self.conn, self.event_input(person_ids=[str(b)]), eid)
        self.assertEqual([p["name"] for p in store.get_event(self.conn, eid)["people"]], ["Sam"])
        # Duplicates in the form are ignored.
        store.save_event(self.conn, self.event_input(person_ids=[str(b), str(b)]), eid)
        self.assertEqual(len(store.get_event(self.conn, eid)["people"]), 1)

    def test_unknown_person_rejected(self):
        self.pid = self.person()
        with self.assertRaises(store.ValidationError) as cm:
            store.save_event(self.conn, self.event_input(person_ids=["9999"]))
        self.assertIn("people", cm.exception.errors)

    def test_duplicate_name_and_bad_color_rejected(self):
        self.person("Alex")
        with self.assertRaises(store.ValidationError) as cm:
            store.save_person(self.conn, "alex", "#123456")
        self.assertIn("name", cm.exception.errors)
        with self.assertRaises(store.ValidationError) as cm:
            store.save_person(self.conn, "Sam", "blue")
        self.assertIn("color", cm.exception.errors)

    def test_person_in_use_cannot_be_deleted(self):
        self.pid = self.person()
        store.save_event(self.conn, self.event_input())
        with self.assertRaises(store.InUseError) as cm:
            store.delete_person(self.conn, self.pid)
        self.assertEqual(cm.exception.count, 1)
        unused = self.person("Sam", "#F06BA8")
        store.delete_person(self.conn, unused)
        self.assertIsNone(store.get_person(self.conn, unused))

    def test_category_in_use_cannot_be_deleted(self):
        self.pid = self.person()
        store.save_event(self.conn, self.event_input())
        with self.assertRaises(store.InUseError):
            store.delete_category(self.conn, self.category_id("Medical"))
        store.delete_category(self.conn, self.category_id("Travel"))
        self.assertNotIn("Travel", [c["name"] for c in store.list_categories(self.conn)])


class TestReminders(TempDB):
    def setUp(self):
        super().setUp()
        self.pid = self.person()

    def rem(self, **kw):
        eid = store.save_event(self.conn, self.event_input(**kw))
        return store.effective_reminder_minutes(store.get_event(self.conn, eid))

    def test_inherits_category_default(self):
        self.assertEqual(self.rem(category_id=str(self.category_id("Medical"))), 120)
        self.assertEqual(self.rem(category_id=str(self.category_id("Family"))), 60)

    def test_info_category_means_no_reminder(self):
        self.assertIsNone(self.rem(category_id=str(self.category_id("Info"))))

    def test_none_and_custom(self):
        self.assertIsNone(self.rem(reminder_mode="none"))
        self.assertEqual(self.rem(reminder_mode="custom", reminder_minutes="45"), 45)

    def test_custom_needs_valid_minutes(self):
        with self.assertRaises(store.ValidationError):
            self.rem(reminder_mode="custom", reminder_minutes="")
        with self.assertRaises(store.ValidationError):
            self.rem(reminder_mode="custom", reminder_minutes="-5")

    def test_all_day_never_reminds(self):
        self.assertIsNone(self.rem(all_day=True, start_time="", end_time=""))

    def test_category_default_change_follows_through(self):
        eid = store.save_event(self.conn, self.event_input(category_id=str(self.category_id("Home"))))
        store.save_category(self.conn, "Home", "#F76B15", "30", self.category_id("Home"))
        self.assertEqual(store.effective_reminder_minutes(store.get_event(self.conn, eid)), 30)


class TestDarkHours(unittest.TestCase):
    def test_crosses_midnight(self):
        s, e = time(23, 0), time(6, 0)
        self.assertTrue(timeutil.in_dark_hours(time(23, 0), s, e))
        self.assertTrue(timeutil.in_dark_hours(time(2, 30), s, e))
        self.assertTrue(timeutil.in_dark_hours(time(5, 59), s, e))
        self.assertFalse(timeutil.in_dark_hours(time(6, 0), s, e))
        self.assertFalse(timeutil.in_dark_hours(time(12, 0), s, e))
        self.assertFalse(timeutil.in_dark_hours(time(22, 59), s, e))

    def test_same_day_period(self):
        s, e = time(1, 0), time(5, 0)
        self.assertTrue(timeutil.in_dark_hours(time(3, 0), s, e))
        self.assertFalse(timeutil.in_dark_hours(time(6, 0), s, e))

    def test_equal_times_means_off(self):
        self.assertFalse(timeutil.in_dark_hours(time(3, 0), time(0, 0), time(0, 0)))


class TestSettings(TempDB):
    def test_saves_and_validates(self):
        store.save_config(self.conn, {"upcoming_days": "10", "stale_minutes": "20", "dark_start": "22:30",
                                      "dark_end": "05:45", "wake_seconds": "90"})
        cfg = store.get_config(self.conn)
        self.assertEqual((cfg["upcoming_days"], cfg["dark_start"], cfg["wake_seconds"]), ("10", "22:30", "90"))
        with self.assertRaises(store.ValidationError) as cm:
            store.save_config(self.conn, {"upcoming_days": "0", "stale_minutes": "x", "dark_start": "25:00",
                                          "dark_end": "06:00", "wake_seconds": "120"})
        self.assertEqual(set(cm.exception.errors), {"upcoming_days", "stale_minutes", "dark_start"})


class TestEditorPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_endpoints(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"], 1)

    def test_status_reports_unreadable_database(self):
        os.remove(self.db_path)
        os.mkdir(self.db_path)  # a folder where the database file should be
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.json()["ok"])

    def test_every_page_loads(self):
        for path in ("/", "/?show=past", "/events/new", "/people", "/categories", "/settings",
                     "/manifest.webmanifest", "/static/editor.css", "/static/icons/icon-192.png",
                     "/static/icons/apple-touch-icon.png"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_phone_friendly_markup(self):
        html = self.client.get("/").text
        self.assertIn('name="viewport"', html)
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("+ Add event", html)

    def test_full_editor_flow(self):
        c = self.client
        # Add people.
        r = c.post("/people", data={"name": "Alex", "color": "#4C9AFF"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        c.post("/people", data={"name": "Sam", "color": "#F06BA8"})
        r = c.post("/people", data={"name": "", "color": "#000000"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Enter a name.", r.text)

        conn = db.connect(self.db_path)
        alex, sam = [r["id"] for r in conn.execute("SELECT id FROM people ORDER BY id")]
        medical = conn.execute("SELECT id FROM categories WHERE name='Medical'").fetchone()[0]
        conn.close()

        # Create an event for two people.
        form = {"title": "Eye exam", "location": "Test Clinic", "start_date": "2027-01-05", "start_time": "09:00",
                "end_date": "2027-01-05", "end_time": "10:00", "category_id": str(medical),
                "people": [str(alex), str(sam)], "reminder_mode": "default"}
        r = c.post("/events/new", data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Eye exam", r.text)
        self.assertIn("Test Clinic", r.text)

        # Invalid form re-shows with errors and keeps what was typed.
        bad = dict(form, title="", people=[])
        r = c.post("/events/new", data=bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Enter a title.", r.text)
        self.assertIn("Tick at least one person.", r.text)
        self.assertIn("Test Clinic", r.text)

        conn = db.connect(self.db_path)
        eid = conn.execute("SELECT id FROM events").fetchone()[0]
        conn.close()

        # Edit, then the person in use can't be removed.
        r = c.get(f"/events/{eid}/edit")
        self.assertIn('value="Eye exam"', r.text)
        r = c.post(f"/events/{eid}/edit", data=dict(form, title="Eye exam (moved)", start_time="11:00", end_time="12:00"),
                   follow_redirects=True)
        self.assertIn("Eye exam (moved)", r.text)
        r = c.post(f"/people/{alex}/delete", follow_redirects=True)
        self.assertIn("can&#39;t be removed", r.text)
        r = c.post(f"/categories/{medical}/delete", follow_redirects=True)
        self.assertIn("can&#39;t be removed", r.text)

        # Delete with confirmation.
        r = c.get(f"/events/{eid}/delete")
        self.assertIn("Delete this event?", r.text)
        r = c.post(f"/events/{eid}/delete", follow_redirects=True)
        self.assertIn("Deleted", r.text)
        r = c.post(f"/people/{alex}/delete", follow_redirects=True)
        self.assertIn("Removed Alex", r.text)

    def test_settings_page_round_trip(self):
        r = self.client.post("/settings", data={"upcoming_days": "21", "stale_minutes": "30", "dark_start": "22:00",
                                                 "dark_end": "07:00", "wake_seconds": "120"}, follow_redirects=True)
        self.assertIn("Settings saved.", r.text)
        self.assertIn('value="21"', r.text)
        self.assertIn("cross midnight", r.text)
        r = self.client.post("/settings", data={"upcoming_days": "999", "stale_minutes": "30", "dark_start": "22:00",
                                                 "dark_end": "07:00", "wake_seconds": "120"})
        self.assertEqual(r.status_code, 400)

    def test_nav_marks_current_page(self):
        for path, label in (("/", "Events"), ("/events/new", "Events"), ("/people", "People"),
                            ("/categories", "Categories"), ("/settings", "Settings")):
            html = self.client.get(path).text
            self.assertIn(f'aria-current="page">{label}</a>', html, path)
            self.assertEqual(html.count('aria-current="page"'), 1, path)

    def test_home_page_lists_days_in_order(self):
        c = self.client
        c.post("/people", data={"name": "Alex", "color": "#4C9AFF"})
        today = timeutil.now_local().date()
        def add(title, d, all_day=False):
            f = {"title": title, "start_date": d.isoformat(), "end_date": d.isoformat(), "category_id": "1",
                 "people": ["1"], "reminder_mode": "default"}
            f.update({"all_day": "1"} if all_day else {"start_time": "10:00", "end_time": "11:00"})
            self.assertEqual(c.post("/events/new", data=f, follow_redirects=False).status_code, 303)
        add("Far away trip", today + timedelta(days=9), all_day=True)
        add("Soon", today + timedelta(days=2))
        add("Old thing", today - timedelta(days=3))
        add("Older thing", today - timedelta(days=5))
        html = c.get("/").text
        self.assertLess(html.index("Soon"), html.index("Far away trip"))
        self.assertNotIn("Old thing", html)
        html = c.get("/?show=past").text
        self.assertLess(html.index("Old thing"), html.index("Older thing"))
        self.assertNotIn("Soon", html)

    def test_missing_event_redirects_politely(self):
        r = self.client.get("/events/12345/edit", follow_redirects=True)
        self.assertIn("no longer exists", r.text)


if __name__ == "__main__":
    unittest.main()
