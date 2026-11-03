"""Phase 4 tests: actions on the shelf (add, move, delete), Recently deleted,
Quick titles, "Added on the shelf", and the new display API fields.

Plain Python (unittest). All data is fictional.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402


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
        base = dict(title="Plumber", start_date="2026-11-13", start_time="15:00", end_date="2026-11-13",
                    end_time="16:00", category_id=str(self.cat("Home")), person_ids=[str(self.alex)])
        base.update(kw)
        if base.get("all_day"):
            base["start_time"] = base["end_time"] = ""
        return store.save_event(self.conn, store.EventInput(**base))

    def club(self, **kw):
        # 1st Tuesday of the month, 7:00-9:00 PM.
        return self.add(title="Radio club meeting", start_date="2026-11-03", end_date="2026-11-03",
                        start_time="19:00", end_time="21:00", category_id=str(self.cat("Club / Ham radio")),
                        repeat="monthly_nth", ordinals=["1"], nth_weekday="TU", **kw)

    def dates(self, title, lo="2026-11-01", hi="2027-06-30"):
        return [(e["start_local"], e["end_local"]) for e in
                store.list_events(self.conn, timeutil.parse_date(lo), timeutil.parse_date(hi)) if e["title"] == title]


# ----------------------------------------------------------------- add


class TestAdd(Base):
    def test_timed_add_is_one_hour_with_category_reminder_and_marked(self):
        eid = store.add_from_shelf(self.conn, "Dentist", "2026-11-17", "14:30", False, [self.robin],
                                   self.cat("Medical"))
        ev = store.get_event(self.conn, eid)
        self.assertEqual((ev["start_local"], ev["end_local"]), ("2026-11-17T14:30", "2026-11-17T15:30"))
        self.assertEqual(ev["reminder_mode"], "default")
        self.assertEqual(store.effective_reminder_minutes(ev), 120)   # Medical default
        self.assertEqual(ev["added_on_shelf"], 1)
        self.assertEqual([p["id"] for p in ev["people"]], [self.robin])
        self.assertIsNone(ev["rrule"])

    def test_all_day_add(self):
        eid = store.add_from_shelf(self.conn, "Visit", "2026-11-20", "09:00", True, [self.alex, self.robin],
                                   self.cat("Family"))
        ev = store.get_event(self.conn, eid)
        self.assertEqual((ev["all_day"], ev["start_local"], ev["end_local"]), (1, "2026-11-20", "2026-11-20"))
        self.assertEqual(len(ev["people"]), 2)

    def test_validation(self):
        with self.assertRaises(store.ValidationError) as cm:
            store.add_from_shelf(self.conn, "  ", "2026-11-20", "", False, [], None)
        self.assertEqual(set(cm.exception.errors), {"title", "people", "category", "start_time"})

    def test_editor_events_are_not_marked(self):
        self.assertEqual(store.get_event(self.conn, self.add())["added_on_shelf"], 0)


# ---------------------------------------------------------------- move


class TestMove(Base):
    def test_one_off_keeps_length(self):
        eid = self.add(end_time="16:30")
        moved = store.move_from_shelf(self.conn, eid, None, "2026-11-16", "10:30")
        self.assertEqual(moved, {"start": "2026-11-16T10:30", "end": "2026-11-16T12:00"})
        self.assertEqual(self.dates("Plumber"), [("2026-11-16T10:30", "2026-11-16T12:00")])

    def test_one_off_crossing_midnight_keeps_length(self):
        eid = self.add(start_time="22:00", end_date="2026-11-14", end_time="01:00")
        moved = store.move_from_shelf(self.conn, eid, None, "2026-11-20", "23:00")
        self.assertEqual(moved, {"start": "2026-11-20T23:00", "end": "2026-11-21T02:00"})

    def test_all_day_multi_day_moves_as_a_whole(self):
        eid = self.add(title="Sam visiting", all_day=True, start_date="2026-11-12", end_date="2026-11-15")
        moved = store.move_from_shelf(self.conn, eid, None, "2026-11-19", "")
        self.assertEqual(moved, {"start": "2026-11-19", "end": "2026-11-22"})

    def test_one_date_of_a_series_only(self):
        eid = self.club()
        store.move_from_shelf(self.conn, eid, "2026-12-01", "2026-12-02", "18:30")
        got = self.dates("Radio club meeting", hi="2027-01-31")
        self.assertEqual(got, [("2026-11-03T19:00", "2026-11-03T21:00"), ("2026-12-02T18:30", "2026-12-02T20:30"),
                               ("2027-01-05T19:00", "2027-01-05T21:00")])

    def test_series_across_daylight_saving_keeps_wall_clock(self):
        # March 2 2027 is before the 14 March change; moving it after keeps 7:00 PM local.
        eid = self.club()
        store.move_from_shelf(self.conn, eid, "2027-03-02", "2027-03-16", "19:00")
        got = dict(self.dates("Radio club meeting", lo="2027-03-01", hi="2027-04-30"))
        self.assertIn("2027-03-16T19:00", got)
        self.assertEqual(got["2027-04-06T19:00"], "2027-04-06T21:00")
        # The aware times show the real hour change: EST before, EDT after.
        self.assertEqual(timeutil.to_aware("2027-03-16T19:00").utcoffset(), timedelta(hours=-4))

    def test_moving_a_changed_date_keeps_its_changes(self):
        eid = self.club()
        store.change_occurrence(self.conn, store.get_event(self.conn, eid), "2026-12-01", store.OccurrenceInput(
            title="Club holiday party", location="Fire hall", notes="Bring a dish", start_date="2026-12-01",
            start_time="18:00", end_date="2026-12-01", end_time="21:00"))
        store.move_from_shelf(self.conn, eid, "2026-12-01", "2026-12-08", "18:00")
        occ = store.get_occurrence(self.conn, eid, "2026-12-01")
        self.assertEqual((occ["title"], occ["location"], occ["notes"]), ("Club holiday party", "Fire hall", "Bring a dish"))
        self.assertEqual((occ["start_local"], occ["end_local"]), ("2026-12-08T18:00", "2026-12-08T21:00"))

    def test_validation(self):
        eid = self.add()
        with self.assertRaises(store.ValidationError) as cm:
            store.move_from_shelf(self.conn, eid, None, "soon", "25:00")
        self.assertEqual(set(cm.exception.errors), {"date", "time"})

    def test_gone_elsewhere(self):
        eid = self.club()
        with self.assertRaises(store.NotFoundError):
            store.move_from_shelf(self.conn, eid, "2026-12-02", "2026-12-09", "19:00")   # not a date of it
        store.skip_occurrence(self.conn, eid, "2026-12-01")
        with self.assertRaises(store.NotFoundError):
            store.move_from_shelf(self.conn, eid, "2026-12-01", "2026-12-09", "19:00")   # skipped meanwhile
        with self.assertRaises(store.NotFoundError):
            store.move_from_shelf(self.conn, 999, None, "2026-12-09", "19:00")


# ------------------------------------------------- delete and restore


class TestDeleteAndRestore(Base):
    NOW = datetime(2026, 11, 13, 13, 20)

    def test_delete_one_off_then_restore(self):
        eid = self.add(location="Kitchen", notes="Leaky tap")
        self.assertEqual(store.delete_from_shelf(self.conn, eid, None, self.NOW), "event")
        self.assertIsNone(store.get_event(self.conn, eid))
        items = store.list_recently_deleted(self.conn, self.NOW)
        self.assertEqual([(i["kind"], i["title"], i["when_label"], i["from_shelf"]) for i in items],
                         [("event", "Plumber", "Fri, Nov 13 at 3:00 PM", 1)])
        msg = store.restore_deleted(self.conn, items[0]["id"], self.NOW)
        self.assertIn("Restored", msg)
        back = [e for e in store.list_events(self.conn, self.NOW.date(), self.NOW.date())]
        self.assertEqual([(e["title"], e["location"], e["notes"], len(e["people"])) for e in back],
                         [("Plumber", "Kitchen", "Leaky tap", 1)])
        self.assertEqual(store.list_recently_deleted(self.conn, self.NOW), [])

    def test_delete_one_date_of_series_then_restore(self):
        eid = self.club()
        self.assertEqual(store.delete_from_shelf(self.conn, eid, "2026-12-01", self.NOW), "date")
        self.assertEqual(len(self.dates("Radio club meeting", hi="2027-01-31")), 2)
        item = store.list_recently_deleted(self.conn, self.NOW)[0]
        self.assertEqual((item["kind"], item["occ_date"], item["when_label"]), ("date", "2026-12-01", "Tue, Dec 1 at 7:00 PM"))
        store.restore_deleted(self.conn, item["id"], self.NOW)
        self.assertEqual(len(self.dates("Radio club meeting", hi="2027-01-31")), 3)

    def test_restoring_a_changed_date_brings_its_changes_back(self):
        eid = self.club()
        store.change_occurrence(self.conn, store.get_event(self.conn, eid), "2026-12-01", store.OccurrenceInput(
            title="Club holiday party", start_date="2026-12-01", start_time="18:00", end_date="2026-12-01",
            end_time="21:00"))
        store.delete_from_shelf(self.conn, eid, "2026-12-01", self.NOW)
        item = store.list_recently_deleted(self.conn, self.NOW)[0]
        self.assertEqual(item["title"], "Club holiday party")
        store.restore_deleted(self.conn, item["id"], self.NOW)
        occ = store.get_occurrence(self.conn, eid, "2026-12-01")
        self.assertEqual((occ["skipped"], occ["title"], occ["start_local"]), (False, "Club holiday party", "2026-12-01T18:00"))

    def test_whole_series_from_editor_restores_with_its_exceptions(self):
        eid = self.club()
        store.skip_occurrence(self.conn, eid, "2026-12-01")
        store.delete_event(self.conn, eid, now=self.NOW)
        item = store.list_recently_deleted(self.conn, self.NOW)[0]
        self.assertEqual((item["kind"], item["from_shelf"]), ("event", 0))
        self.assertTrue(item["when_label"].startswith("Every month"))
        store.restore_deleted(self.conn, item["id"], self.NOW)
        self.assertEqual(len(self.dates("Radio club meeting", hi="2027-01-31")), 2)   # Dec still skipped

    def test_date_of_a_series_deleted_since_cannot_be_restored(self):
        eid = self.club()
        store.delete_from_shelf(self.conn, eid, "2026-12-01", self.NOW)
        date_item = store.list_recently_deleted(self.conn, self.NOW)[0]
        store.delete_event(self.conn, eid, now=self.NOW)
        with self.assertRaises(store.NotFoundError):
            store.restore_deleted(self.conn, date_item["id"], self.NOW)

    def test_restore_gets_a_fresh_id_when_the_old_one_was_reused(self):
        eid = self.add()
        store.delete_from_shelf(self.conn, eid, None, self.NOW)
        newer = self.add(title="Car service")          # SQLite may reuse the deleted id
        store.restore_deleted(self.conn, store.list_recently_deleted(self.conn, self.NOW)[0]["id"], self.NOW)
        titles = sorted(e["title"] for e in store.list_events(self.conn, self.NOW.date(), self.NOW.date()))
        self.assertEqual(titles, ["Car service", "Plumber"])
        self.assertEqual(store.get_event(self.conn, newer)["title"], "Car service")

    def test_kept_three_days(self):
        eid = self.add()
        store.delete_from_shelf(self.conn, eid, None, self.NOW)
        self.assertEqual(len(store.list_recently_deleted(self.conn, self.NOW + timedelta(days=2, hours=23))), 1)
        self.assertEqual(store.list_recently_deleted(self.conn, self.NOW + timedelta(days=3, minutes=1)), [])

    def test_at_most_twenty(self):
        for i in range(23):
            store.delete_from_shelf(self.conn, self.add(title=f"Item {i:02d}"), None, self.NOW + timedelta(minutes=i))
        items = store.list_recently_deleted(self.conn, self.NOW + timedelta(minutes=30))
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0]["title"], "Item 22")
        self.assertEqual(items[-1]["title"], "Item 03")

    def test_restore_refused_when_people_are_gone(self):
        eid = self.add(person_ids=[str(self.robin)])
        store.delete_from_shelf(self.conn, eid, None, self.NOW)
        store.delete_person(self.conn, self.robin)
        with self.assertRaises(store.NotFoundError):
            store.restore_deleted(self.conn, store.list_recently_deleted(self.conn, self.NOW)[0]["id"], self.NOW)


# ------------------------------------------------------------ quick titles


class TestQuickTitles(Base):
    def test_save_list_and_duplicates(self):
        store.save_quick_title(self.conn, "Vet", None)
        store.save_quick_title(self.conn, "Dentist", self.cat("Medical"))
        self.assertEqual([(q["title"], q["category_name"]) for q in store.list_quick_titles(self.conn)],
                         [("Dentist", "Medical"), ("Vet", None)])
        with self.assertRaises(store.ValidationError):
            store.save_quick_title(self.conn, "dentist", None)
        with self.assertRaises(store.ValidationError):
            store.save_quick_title(self.conn, "", None)

    def test_suggestions(self):
        for d in ("2026-11-02", "2026-12-07", "2027-01-11"):
            self.add(title="Dentist", start_date=d, end_date=d, category_id=str(self.cat("Medical")))
        self.add(title="dentist", start_date="2027-02-01", end_date="2027-02-01", category_id=str(self.cat("Other")))
        self.add(title="Once only")
        got = store.suggest_quick_titles(self.conn)
        self.assertEqual([(s["uses"], s["category_id"]) for s in got], [(4, self.cat("Medical"))])
        store.save_quick_title(self.conn, "Dentist", None)
        self.assertEqual(store.suggest_quick_titles(self.conn), [])

    def test_category_removed_clears_it(self):
        cat_id = store.save_category(self.conn, "Pets", "#AA7744", "1", default_unit="hours")
        store.save_quick_title(self.conn, "Vet", cat_id)
        store.delete_category(self.conn, cat_id)
        self.assertIsNone(store.list_quick_titles(self.conn)[0]["category_id"])

    def test_payload_offers_ten_alphabetically(self):
        for i in range(12):
            store.save_quick_title(self.conn, f"Title {chr(76 - i)}", None)   # L .. A
        data = display.build(self.conn, datetime(2026, 11, 13, 13, 20))
        titles = [q["title"] for q in data["quick_titles"]]
        self.assertEqual(len(titles), 10)
        self.assertEqual(titles, sorted(titles))
        self.assertEqual(titles[0], "Title A")


# ------------------------------------------------------------ display API


class TestApi(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_payload_fields(self):
        self.add(notes="Bring the key")
        self.club()
        data = self.client.get("/api/v1/display?now=2026-11-03T08:00").json()
        self.assertEqual({p["name"] for p in data["people"]}, {"Alex", "Robin"})
        self.assertIn("Medical", [c["name"] for c in data["categories"]])
        self.assertEqual(data["quick_titles"], [])
        events = {e["title"]: e for d in data["days"] for e in d["events"]}
        self.assertEqual(events["Plumber"]["notes"], "Bring the key")
        self.assertEqual(events["Plumber"]["repeat_text"], "")
        self.assertEqual(events["Radio club meeting"]["repeat_text"], "Every month on the 1st Tuesday")

    def test_add_move_delete_round_trip(self):
        r = self.client.post("/api/v1/events", json={"title": "Dentist", "date": "2026-11-17", "time": "14:30",
                                                      "people": [self.robin], "category_id": self.cat("Medical")})
        self.assertEqual(r.status_code, 200, r.text)
        eid = r.json()["id"]
        r = self.client.post(f"/api/v1/events/{eid}/move", json={"date": "2026-11-18", "time": "09:00"})
        self.assertEqual(r.json(), {"ok": True, "id": eid, "start": "2026-11-18T09:00", "end": "2026-11-18T10:00"})
        r = self.client.post(f"/api/v1/events/{eid}/delete", json={})
        self.assertEqual(r.json()["deleted"], "event")
        self.assertIn("Dentist", self.client.get("/recently-deleted").text)

    def test_series_date_delete_and_errors(self):
        eid = self.club()
        r = self.client.post(f"/api/v1/events/{eid}/delete", json={"occ": "2026-12-01"})
        self.assertEqual(r.json()["deleted"], "date")
        r = self.client.post(f"/api/v1/events/{eid}/delete", json={"occ": "2026-12-01"})
        self.assertEqual((r.status_code, r.json()["error"]), (404, "not_found"))
        r = self.client.post("/api/v1/events", json={"title": "", "date": "x"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid"))
        self.assertIn("title", r.json()["fields"])
        r = self.client.post("/api/v1/events", content="not json", headers={"content-type": "application/json"})
        self.assertEqual(r.json()["error"], "bad_json")
        r = self.client.post("/api/v2/events", json={})
        self.assertEqual((r.status_code, r.json()["error"]), (404, "unsupported_api_version"))

    def test_editor_pages(self):
        self.assertEqual(self.client.get("/quick-titles").status_code, 200)
        r = self.client.post("/quick-titles", data={"title": "Vet", "category_id": ""}, follow_redirects=True)
        self.assertIn("Added “Vet”", r.text)
        self.add(title="Haircut", start_date="2026-11-04", end_date="2026-11-04")
        self.add(title="Haircut", start_date="2026-12-04", end_date="2026-12-04")
        self.assertIn("Haircut", self.client.get("/quick-titles?suggest=1").text)
        r = self.client.post("/quick-titles/add-suggested", data={"pick": [f"Haircut|{self.cat('Home')}"]},
                             follow_redirects=True)
        self.assertIn("Added 1 title", r.text)
        self.assertIn("Quick titles", self.client.get("/settings").text)
        # Shelf tag on the events list and the event form.
        eid = store.add_from_shelf(self.conn, "Vet", "2026-11-20", "10:00", False, [self.alex], self.cat("Other"))
        self.assertIn("Added on the shelf", self.client.get(f"/events/{eid}/edit").text)
        # Restore through the editor.
        self.client.post(f"/api/v1/events/{eid}/delete", json={})
        entry = store.list_recently_deleted(self.conn)[0]
        r = self.client.post(f"/recently-deleted/{entry['id']}/restore", follow_redirects=True)
        self.assertIn("Restored “Vet”", r.text)


# --------------------------------------------------------------- migration


class TestMigration0004(unittest.TestCase):
    def test_from_a_phase_3_database_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
            for v, p in db.available_migrations():
                if v > 3:
                    break
                conn.executescript(p.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (v,))
            conn.execute("INSERT INTO events (title, start_local, end_local, category_id, created, updated)"
                         " VALUES ('Birthday', '2026-11-05', '2026-11-05', 2, 'x', 'x')")
            conn.commit()
            conn.close()
            self.assertEqual(db.migrate(path)[0], 4)   # later migrations follow it
            self.assertEqual(len(list(Path(tmp).glob("calendar.db.pre-v3-*.bak"))), 1)
            c = db.connect(path)
            self.assertEqual(c.execute("SELECT title, added_on_shelf FROM events").fetchone()[:], ("Birthday", 0))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quick_titles").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM recently_deleted").fetchone()[0], 0)
            c.close()


if __name__ == "__main__":
    unittest.main()
