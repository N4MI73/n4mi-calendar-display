"""v1.1 tests: the Out of town marker.

Plain Python (unittest). All data is fictional (made-up people and places), and
nothing touches the network: Google feeds are strings given to the importer.
Dates are worked out from today, so the tests don't go stale.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, importer, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402

TODAY = date.today()
SECRET = "https://calendar.google.com/calendar/ical/fictional%40example.com/private-0123456789abcdef/basic.ics"


def d(n: int) -> date:
    return TODAY + timedelta(days=n)


def ical_dt(day: date, hh: int, mm: int = 0) -> str:
    return f"{day:%Y%m%d}T{hh:02d}{mm:02d}00"


def feed(*events: str) -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
                        "X-WR-TIMEZONE:America/New_York", *events, "END:VCALENDAR", ""])
    return body.encode()


def timed(uid: str, title: str, day: date, hh: int, location: str | None = None, *extra: str) -> str:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{title}",
             f"DTSTART;TZID=America/New_York:{ical_dt(day, hh)}",
             f"DTEND;TZID=America/New_York:{ical_dt(day, hh + 1)}"]
    if location is not None:
        lines.append(f"LOCATION:{location}")
    return "\r\n".join(lines + list(extra) + ["END:VEVENT"])


CARDIO = timed("cardio@google.com", "Cardiology", d(12), 10, "Peachtree Heart Clinic\\, Atlanta")
EYES = timed("eyes@google.com", "Eye exam", d(15), 14, "Bright Eyes Optometry\\, Evans")
NOWHERE = timed("plain@google.com", "Call the bank", d(13), 9)
HAMFEST = timed("ham@google.com", "Hamfest", d(20), 8, "Fairgrounds\\, Perry")
AUGUSTINE = timed("aug@google.com", "Lunch", d(14), 12, "Augustine's Diner\\, Perry")   # not the town Augusta


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.pat = store.save_person(self.conn, "Pat", "#F06BA8")
        self.sam = store.save_person(self.conn, "Sam", "#A78BFA")
        os.environ["CAL_SOURCE_1_URL"] = SECRET

    def tearDown(self):
        os.environ.pop("CAL_SOURCE_1_URL", None)
        self.conn.close()
        self.tmp.cleanup()

    def cat(self, name):
        return self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]

    def add(self, **kw):
        base = dict(title="Cardiology", start_date=d(12).isoformat(), start_time="10:30",
                    end_date=d(12).isoformat(), end_time="11:30", category_id=str(self.cat("Medical")),
                    person_ids=[str(self.pat)])
        base.update(kw)
        if base.get("all_day"):
            base["start_time"] = base["end_time"] = ""
        return store.save_event(self.conn, store.EventInput(**base))

    def defaults(self):
        importer.save_source(self.conn, 1, "Family calendar", [self.pat], self.cat("Other"))

    def fetch(self, *events):
        data = feed(*events)
        return importer.fetch_source(self.conn, 1, TODAY, fetcher=lambda url: data)

    def items(self):
        return importer.list_items(self.conn)

    def item(self, title):
        return next(i for i in self.items() if i["g"]["title"] == title)

    def event(self, title):
        row = self.conn.execute("SELECT id FROM events WHERE title = ?", (title,)).fetchone()
        return store.get_event(self.conn, row[0]) if row else None


# ---------------------------------------------------------------- the database


class TestMigration0008(unittest.TestCase):
    def test_from_a_v1_0_database_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
            for v, p in db.available_migrations():
                if v > 7:
                    break
                conn.executescript(p.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (v,))
            conn.execute("INSERT INTO events (title, start_local, end_local, category_id, created, updated)"
                         " VALUES ('Dentist', '2026-12-01T14:30', '2026-12-01T15:30', 1, 'x', 'x')")
            conn.commit()
            conn.close()
            self.assertEqual(db.migrate(path), [8])
            self.assertEqual(len(list(Path(tmp).glob("calendar.db.pre-v7-*.bak"))), 1)
            c = db.connect(path)
            self.assertEqual(c.execute("SELECT title, out_of_town FROM events").fetchone()[:], ("Dentist", 0))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM import_places").fetchone()[0], 0)
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE events SET out_of_town = 2")
            c.close()


# ---------------------------------------------------------------- the event form


class TestEventField(Base):
    def test_saved_edited_and_read_back(self):
        eid = self.add(out_of_town=True)
        ev = store.get_event(self.conn, eid)
        self.assertEqual(ev["out_of_town"], 1)
        self.assertTrue(store.event_to_input(ev).out_of_town)
        form = store.event_to_input(ev)
        form.out_of_town = False
        store.save_event(self.conn, form, eid)
        self.assertEqual(store.get_event(self.conn, eid)["out_of_town"], 0)
        self.assertEqual(store.get_event(self.conn, self.add(title="Haircut"))["out_of_town"], 0)   # default local

    def test_series_carries_it_on_every_date_and_after_a_split(self):
        eid = self.add(title="Physio", out_of_town=True, repeat="weekly",
                       weekdays=[["MO", "TU", "WE", "TH", "FR", "SA", "SU"][d(12).weekday()]], start_date=d(12).isoformat(), end_date=d(12).isoformat())
        occs = [o for o in store.list_events(self.conn, d(0), d(40)) if o["id"] == eid]
        self.assertGreaterEqual(len(occs), 4)
        self.assertTrue(all(o["out_of_town"] for o in occs))
        # One date moved: still out of town (it belongs to the series).
        occ = occs[1]
        store.change_occurrence(self.conn, store.get_event(self.conn, eid), occ["occ_date"],
                                store.OccurrenceInput(start_date=occ["occ_date"], start_time="15:00",
                                                      end_date=occ["occ_date"], end_time="16:00",
                                                      title="Physio", location="", notes=""))
        again = [o for o in store.list_events(self.conn, d(0), d(40)) if o["id"] == eid]
        self.assertTrue(all(o["out_of_town"] for o in again))
        # "This and all future": the new series keeps it.
        third = again[2]["occ_date"]
        new_id = store.split_series(self.conn, eid, third, store.future_input(store.get_event(self.conn, eid), third))
        self.assertNotEqual(new_id, eid)
        self.assertEqual(store.get_event(self.conn, new_id)["out_of_town"], 1)

    def test_shelf_add_is_local(self):
        c = TestClient(create_app(self.db_path))
        r = c.post("/api/v1/events", json={"title": "Coffee", "date": d(3).isoformat(), "time": "10:00",
                                           "people": [self.pat], "category_id": self.cat("Social"),
                                           "out_of_town": True})
        self.assertIn(r.status_code, (200, 201), r.text)
        self.assertEqual(self.event("Coffee")["out_of_town"], 0)

    def test_restore_brings_it_back_and_old_snapshots_are_local(self):
        eid = self.add(out_of_town=True)
        store.delete_event(self.conn, eid)
        entry = store.list_recently_deleted(self.conn)[0]
        store.restore_deleted(self.conn, entry["id"])
        self.assertEqual(self.event("Cardiology")["out_of_town"], 1)
        # A snapshot written before migration 0008 has no out_of_town: restored as local.
        eid = self.event("Cardiology")["id"]
        store.delete_event(self.conn, eid)
        entry = store.list_recently_deleted(self.conn)[0]
        snap = json.loads(self.conn.execute("SELECT snapshot FROM recently_deleted WHERE id = ?",
                                            (entry["id"],)).fetchone()[0])
        snap["event"].pop("out_of_town")
        self.conn.execute("UPDATE recently_deleted SET snapshot = ? WHERE id = ?", (json.dumps(snap), entry["id"]))
        self.conn.commit()
        store.restore_deleted(self.conn, entry["id"])
        self.assertEqual(self.event("Cardiology")["out_of_town"], 0)


# ---------------------------------------------------------------- the reminder


class TestReminder(Base):
    DARK = (time(23, 0), time(6, 0))

    def ev(self, **kw):
        return store.get_event(self.conn, self.add(**kw))

    def test_longer_of_category_default_and_out_of_town_days(self):
        ev = self.ev(out_of_town=True)                                   # Medical: 2 hours
        self.assertEqual(store.effective_reminder_minutes(ev, 5), 5 * 1440)
        self.assertEqual(store.effective_reminder_minutes(ev, 0), 120)   # setting off
        local = self.ev(title="Local", out_of_town=False)
        self.assertEqual(store.effective_reminder_minutes(local, 5), 120)
        # A category default already longer than the out-of-town days wins.
        store.save_category(self.conn, "Long", "#123456", "7", default_unit="days")
        longer = self.ev(title="Trip", out_of_town=True, category_id=str(self.cat("Long")))
        self.assertEqual(store.effective_reminder_minutes(longer, 5), 7 * 1440)

    def test_custom_and_none_always_win(self):
        custom = self.ev(out_of_town=True, reminder_mode="custom", reminder_minutes="3", reminder_unit="hours")
        self.assertEqual(store.effective_reminder_minutes(custom, 5), 180)
        none = self.ev(title="X", out_of_town=True, reminder_mode="none")
        self.assertIsNone(store.effective_reminder_minutes(none, 5))

    def test_all_day_and_no_category_default(self):
        fair = self.ev(title="Craft fair", all_day=True, out_of_town=True, category_id=str(self.cat("Social")),
                       start_date=d(20).isoformat(), end_date=d(20).isoformat())
        self.assertIsNone(fair["category_allday_days"])                  # categories start with none
        self.assertEqual(store.effective_reminder_minutes(fair, 5), 5 * 1440)
        at = display.remind_at(fair, *self.DARK, oot_days=5)
        self.assertEqual(at, datetime.combine(d(15), time(6, 0)))      # from the end of dark hours, 5 days before
        home = self.ev(title="Yard sale", all_day=True, category_id=str(self.cat("Social")),
                       start_date=d(20).isoformat(), end_date=d(20).isoformat())
        self.assertIsNone(store.effective_reminder_minutes(home, 5))

    def test_info_category_stays_as_it_is(self):
        flight = self.ev(title="Aunt's flight", out_of_town=True, category_id=str(self.cat("Info")))
        self.assertIsNone(store.effective_reminder_minutes(flight, 5))

    def test_settings_value_and_validation(self):
        self.assertEqual(store.oot_days(store.get_config(self.conn)), 5)
        store.save_config(self.conn, dict(store.get_config(self.conn), oot_reminder_days="3"))
        self.assertEqual(store.oot_days(store.get_config(self.conn)), 3)
        for bad in ("x", "-1", "31"):
            with self.assertRaises(store.ValidationError):
                store.save_config(self.conn, dict(store.get_config(self.conn), oot_reminder_days=bad))
        self.assertEqual(store.oot_days({"oot_reminder_days": "junk"}), 0)

    def test_display_payload_uses_it(self):
        self.add(out_of_town=True)
        now = datetime.combine(d(7), time(9, 40))                        # five days before
        data = display.build(self.conn, now)
        ev = next(e for day in data["days"] for e in day["events"] if e["title"] == "Cardiology")
        self.assertTrue(ev["out_of_town"])
        self.assertEqual(ev["remind_at"], f"{d(7).isoformat()}T10:30")
        self.add(title="Haircut", start_date=d(9).isoformat(), end_date=d(9).isoformat())
        data = display.build(self.conn, now)
        hc = next(e for day in data["days"] for e in day["events"] if e["title"] == "Haircut")
        self.assertFalse(hc["out_of_town"])
        panel = display.days_range(self.conn, datetime.combine(TODAY, time(9, 0)), d(1), 30)
        cardio = next(e for day in panel["days"] for e in day["events"] if e["title"] == "Cardiology")
        self.assertTrue(cardio["out_of_town"])


# ---------------------------------------------------------------- the import


class TestPlaces(Base):
    def test_whole_word_town_matching(self):
        towns = ["Grovetown", "Evans", "Augusta", "North Augusta"]
        self.assertEqual(store.town_in("Bright Eyes Optometry, evans", towns), "Evans")
        self.assertEqual(store.town_in("12 Main St, North  Augusta SC", towns), "Augusta")   # either is local
        self.assertIsNone(store.town_in("Augustine's Diner, Perry", towns))
        self.assertIsNone(store.town_in("Evansville Clinic", towns))
        self.assertIsNone(store.town_in("", towns))

    def test_default_towns_and_editing_the_list(self):
        towns = store.local_towns(store.get_config(self.conn))
        self.assertEqual(towns, ["Grovetown", "Evans", "Augusta", "Martinez", "North Augusta"])
        store.add_local_town(self.conn, "  Appling ")
        with self.assertRaises(store.ValidationError):
            store.add_local_town(self.conn, "appling")                   # already there, any capitals
        with self.assertRaises(store.ValidationError):
            store.add_local_town(self.conn, "")
        store.remove_local_town(self.conn, "EVANS")
        self.assertEqual(store.local_towns(store.get_config(self.conn)),
                         ["Grovetown", "Augusta", "Martinez", "North Augusta", "Appling"])

    def test_status_order(self):
        s = lambda loc: store.place_status(self.conn, loc)["state"]   # noqa: E731
        self.assertEqual(s(None), "local")
        self.assertEqual(s("  "), "local")
        self.assertEqual(s("Bright Eyes Optometry, Evans"), "local")
        self.assertEqual(s("Peachtree Heart Clinic, Atlanta"), "ask")
        store.remember_place(self.conn, "Peachtree  Heart Clinic, Atlanta", True)
        self.assertEqual(s("peachtree heart clinic, atlanta"), "out")    # spaces and capitals ignored
        # An answer given for a place wins over the towns list.
        store.remember_place(self.conn, "Hospital, Augusta", True)
        self.assertEqual(s("Hospital, Augusta"), "out")
        store.forget_place(self.conn, store.place_key("Hospital, Augusta"))
        self.assertEqual(s("Hospital, Augusta"), "local")


class TestImport(Base):
    def test_review_sorts_the_new_events(self):
        self.fetch(CARDIO, EYES, NOWHERE, HAMFEST, AUGUSTINE)
        got = {i["g"]["title"]: importer.place_status(self.conn, i)["state"] for i in self.items()}
        self.assertEqual(got, {"Cardiology": "ask", "Eye exam": "local", "Call the bank": "local",
                               "Hamfest": "ask", "Lunch": "ask"})

    def test_bulk_add_waits_for_an_answer_then_takes_it(self):
        self.defaults()
        self.fetch(CARDIO, EYES, NOWHERE)
        ids = [i["id"] for i in self.items()]
        self.assertEqual(importer.waiting_for_answer(self.conn, ids), 1)
        self.assertEqual(importer.add_with_defaults(self.conn, ids), 2)
        self.assertEqual(self.event("Eye exam")["out_of_town"], 0)
        self.assertEqual(self.event("Call the bank")["out_of_town"], 0)
        self.assertIsNone(self.event("Cardiology"))                      # still waiting
        importer.answer_place(self.conn, self.item("Cardiology")["id"], True)
        self.assertEqual(importer.waiting_for_answer(self.conn, [self.item("Cardiology")["id"]]), 0)
        self.assertEqual(importer.add_with_defaults(self.conn, [self.item("Cardiology")["id"]]), 1)
        self.assertEqual(self.event("Cardiology")["out_of_town"], 1)

    def test_answer_is_remembered_for_the_next_import_of_that_place(self):
        self.defaults()
        self.fetch(HAMFEST)
        importer.answer_place(self.conn, self.item("Hamfest")["id"], True)
        importer.add_with_defaults(self.conn, [self.item("Hamfest")["id"]])
        later = timed("ham2@google.com", "Swap meet", d(40), 9, "fairgrounds\\,  perry")
        self.fetch(HAMFEST, later)
        it = self.item("Swap meet")
        self.assertEqual(importer.place_status(self.conn, it)["state"], "out")
        importer.add_with_defaults(self.conn, [it["id"]])
        self.assertEqual(self.event("Swap meet")["out_of_town"], 1)
        self.assertEqual([p["label"] for p in store.list_places(self.conn)], ["Fairgrounds, Perry"])

    def test_review_and_add_fills_the_tick_box_and_its_choice_becomes_the_answer(self):
        self.defaults()
        store.remember_place(self.conn, "Fairgrounds, Perry", True)
        self.fetch(HAMFEST, CARDIO)
        self.assertTrue(importer.form_for_item(self.conn, self.item("Hamfest")).out_of_town)
        form = importer.form_for_item(self.conn, self.item("Cardiology"))
        self.assertFalse(form.out_of_town)                               # new place: unticked until chosen
        form.out_of_town = True
        importer.add_reviewed(self.conn, self.item("Cardiology")["id"], form)
        self.assertEqual(self.event("Cardiology")["out_of_town"], 1)
        self.assertEqual(store.place_status(self.conn, "Peachtree Heart Clinic, Atlanta")["state"], "out")

    def test_import_never_changes_it_on_an_event_already_here(self):
        self.defaults()
        self.fetch(EYES)
        importer.add_with_defaults(self.conn, [self.item("Eye exam")["id"]])
        ev = self.event("Eye exam")
        form = store.event_to_input(ev)
        form.out_of_town = True
        store.save_event(self.conn, form, ev["id"])
        self.conn.execute("UPDATE events SET locally_modified = 0 WHERE id = ?", (ev["id"],))
        self.conn.commit()
        moved = timed("eyes@google.com", "Eye exam", d(15), 14, "New Clinic\\, Evans")   # Google changed the place
        self.fetch(moved)
        importer.apply_all(self.conn)
        ev = self.event("Eye exam")
        self.assertEqual((ev["location"], ev["out_of_town"]), ("New Clinic, Evans", 1))

    def test_link_keeps_the_local_setting(self):
        self.defaults()
        eid = self.add(out_of_town=True, start_time="10:00", end_time="11:00")
        self.fetch(CARDIO)
        dup = next(i for i in self.items() if i["kind"] == "duplicate")
        importer.link(self.conn, dup["id"])
        self.assertEqual(store.get_event(self.conn, eid)["out_of_town"], 1)


# ---------------------------------------------------------------- the pages


class TestPages(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_event_form_has_the_tick_box_and_saves_it(self):
        r = self.client.get("/events/new")
        self.assertIn('name="out_of_town"', r.text)
        self.assertIn("Away from home", r.text)
        self.assertIn("at least 5 days ahead", r.text)
        r = self.client.post("/events/new", data={
            "title": "Cardiology", "start_date": d(12).isoformat(), "start_time": "10:30",
            "end_date": d(12).isoformat(), "end_time": "11:30", "category_id": str(self.cat("Medical")),
            "people": str(self.pat), "reminder_mode": "default", "repeat": "none", "out_of_town": "1"},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.event("Cardiology")["out_of_town"], 1)
        r = self.client.get("/")
        self.assertIn("oot-tag", r.text)                                  # the tag on the events list

    def test_review_page_asks_and_the_answer_is_saved(self):
        self.defaults()
        self.fetch(CARDIO, EYES)
        r = self.client.get("/import/review")
        self.assertIn("Local or out of town?", r.text)
        self.assertIn("Peachtree Heart Clinic, Atlanta", r.text)
        self.assertIn("Evans is on your local towns list", r.text)
        cid = self.item("Cardiology")["id"]
        r = self.client.post(f"/import/items/{cid}/place", data={"answer": "out"}, follow_redirects=True)
        self.assertIn("out of town, from now on", r.text)
        self.assertIn("your earlier answer for this place", r.text)
        r = self.client.post(f"/import/items/{cid}/place", data={"answer": "maybe"}, follow_redirects=True)
        self.assertIn("Choose Local or Out of town", r.text)
        r = self.client.post("/import/review/add-ticked", data={"item": [str(cid)]}, follow_redirects=True)
        self.assertIn("Added 1 event.", r.text)

    def test_bulk_add_message_names_the_waiting_ones(self):
        self.defaults()
        self.fetch(CARDIO, EYES)
        ids = [str(i["id"]) for i in self.items()]
        r = self.client.post("/import/review/add-ticked", data={"item": ids}, follow_redirects=True)
        self.assertIn("Added 1 event. 1 is waiting for “Local or out of town?”", r.text)

    def test_review_and_add_form_shows_the_new_place(self):
        self.defaults()
        self.fetch(CARDIO)
        r = self.client.get(f"/import/items/{self.item('Cardiology')['id']}/add")
        self.assertIn("New place.", r.text)
        self.assertIn('name="out_of_town"', r.text)

    def test_settings_section(self):
        r = self.client.get("/settings")
        for text in ("Out of town reminder, in days before", "Local towns", "North Augusta",
                     "Places answered at import (0)"):
            self.assertIn(text, r.text)
        r = self.client.post("/settings/towns/add", data={"town": "Appling"}, follow_redirects=True)
        self.assertIn("Added Appling.", r.text)
        r = self.client.post("/settings/towns/add", data={"town": "appling"}, follow_redirects=True)
        self.assertIn("already on the list", r.text)
        r = self.client.post("/settings/towns/remove", data={"town": "Appling"}, follow_redirects=True)
        self.assertNotIn("<span>Appling</span>", r.text)
        store.remember_place(self.conn, "Fairgrounds, Perry", True)
        key = store.place_key("Fairgrounds, Perry")
        r = self.client.post("/settings/places/change", data={"place": key}, follow_redirects=True)
        self.assertIn("Fairgrounds, Perry: now local", r.text)
        self.assertEqual(store.place_status(self.conn, "Fairgrounds, Perry")["state"], "local")
        r = self.client.post("/settings/places/forget", data={"place": key}, follow_redirects=True)
        self.assertIn("Places answered at import (0)", r.text)
        # Saving the main form keeps the towns, and checks the days.
        cfg = store.get_config(self.conn)
        base = {k: cfg[k] for k in ("upcoming_days", "stale_minutes", "dark_start", "dark_end", "wake_seconds",
                                     "display_title")}
        r = self.client.post("/settings", data=dict(base, oot_reminder_days="3"), follow_redirects=True)
        self.assertIn("Settings saved.", r.text)
        self.assertEqual(store.get_config(self.conn)["oot_reminder_days"], "3")
        self.assertIn("Grovetown", r.text)
        r = self.client.post("/settings", data=dict(base, oot_reminder_days="40"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Use a value from 0 to 30.", r.text)

    def test_settings_post_without_the_new_fields_leaves_them(self):
        store.save_config(self.conn, dict(store.get_config(self.conn), oot_reminder_days="4"))
        r = self.client.post("/settings", data={"upcoming_days": "14", "stale_minutes": "30", "dark_start": "23:00",
                                                 "dark_end": "06:00", "wake_seconds": "120"}, follow_redirects=True)
        self.assertIn("Settings saved.", r.text)
        cfg = store.get_config(self.conn)
        self.assertEqual((cfg["oot_reminder_days"], cfg["display_title"]), ("4", "Family Calendar"))

    def test_display_page_has_the_tag_code(self):
        js = self.client.get("/static/display.js").text
        self.assertIn("function ootTag()", js)
        self.assertIn("ev.out_of_town", js)
        css = self.client.get("/static/display.css").text
        self.assertIn(".oot {", css)


if __name__ == "__main__":
    unittest.main()
