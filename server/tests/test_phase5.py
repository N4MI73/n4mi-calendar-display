"""Phase 5a tests: reading a Google calendar feed and the import review.

Plain Python (unittest). All data is fictional, and nothing touches the
network: the feed is a string given to the importer in place of a download.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ical_import, importer, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402

TODAY = date(2026, 11, 3)
SECRET = "https://calendar.google.com/calendar/ical/fictional%40example.com/private-0123456789abcdef/basic.ics"


def feed(*events: str) -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
                        "X-WR-TIMEZONE:America/New_York", *events, "END:VCALENDAR", ""])
    return body.encode()


def vevent(uid, summary, start, end, *extra):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", start, end, *extra, "END:VEVENT"]
    return "\r\n".join(lines)


DENTIST = vevent("dentist@google.com", "Dentist", "DTSTART:20261117T193000Z", "DTEND:20261117T203000Z",
                 "LOCATION:Lakeside Dental", "DESCRIPTION:Bring the<br>insurance card &amp; ID")
CLUB = vevent("club@google.com", "Radio club meeting", "DTSTART;TZID=America/New_York:20261103T190000",
              "DTEND;TZID=America/New_York:20261103T210000", "RRULE:FREQ=MONTHLY;UNTIL=20270706T035959Z;BYDAY=1TU",
              "EXDATE;TZID=America/New_York:20261201T190000")
CLUB_PARTY = vevent("club@google.com", "Club holiday party", "DTSTART;TZID=America/New_York:20270105T180000",
                    "DTEND;TZID=America/New_York:20270105T210000",
                    "RECURRENCE-ID;TZID=America/New_York:20270105T190000")
BIRTHDAY = vevent("bday@google.com", "Mom's birthday", "DTSTART;VALUE=DATE:19501105", "DTEND;VALUE=DATE:19501106",
                  "RRULE:FREQ=YEARLY")
TRIP = vevent("trip@google.com", "Beach trip", "DTSTART;VALUE=DATE:20261210", "DTEND;VALUE=DATE:20261214")
OLD = vevent("old@google.com", "Old thing", "DTSTART;VALUE=DATE:20250105", "DTEND;VALUE=DATE:20250106")
FAR = vevent("far@google.com", "Far away", "DTSTART;VALUE=DATE:20280105", "DTEND;VALUE=DATE:20280106")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.alex = store.save_person(self.conn, "Alex", "#4C9AFF")
        self.robin = store.save_person(self.conn, "Robin", "#F06BA8")
        os.environ["CAL_SOURCE_1_URL"] = SECRET
        self.feed = feed(DENTIST, CLUB, CLUB_PARTY, BIRTHDAY, TRIP, OLD, FAR)

    def tearDown(self):
        os.environ.pop("CAL_SOURCE_1_URL", None)
        os.environ.pop("CAL_SOURCE_2_URL", None)
        self.conn.close()
        self.tmp.cleanup()

    def cat(self, name):
        return self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]

    def defaults(self, slot=1):
        importer.save_source(self.conn, slot, "Family calendar", [self.alex], self.cat("Other"))

    def fetch(self, data=None, slot=1):
        data = self.feed if data is None else data
        return importer.fetch_source(self.conn, slot, TODAY, fetcher=lambda url: data)

    def items(self, kind=None):
        return [i for i in importer.list_items(self.conn) if kind is None or i["kind"] == kind]

    def by_title(self, title, kind=None):
        return next(i for i in self.items(kind) if i["g"].get("title") == title)

    def event(self, title):
        row = self.conn.execute("SELECT id FROM events WHERE title = ?", (title,)).fetchone()
        return store.get_event(self.conn, row[0]) if row else None

    def add_local(self, **kw):
        base = dict(title="Dentist", start_date="2026-11-17", start_time="14:30", end_date="2026-11-17",
                    end_time="15:30", category_id=str(self.cat("Medical")), person_ids=[str(self.robin)])
        base.update(kw)
        if base.get("all_day"):
            base["start_time"] = base["end_time"] = ""
        return store.save_event(self.conn, store.EventInput(**base))


# --------------------------------------------------------------- parsing


class TestParse(Base):
    def parsed(self):
        return {e["uid"]: e for e in ical_import.parse(self.feed, TODAY)["events"]}

    def test_window_and_all_uids(self):
        result = ical_import.parse(self.feed, TODAY)
        self.assertEqual({e["uid"] for e in result["events"]},
                         {"dentist@google.com", "club@google.com", "bday@google.com", "trip@google.com"})
        self.assertIn("old@google.com", result["uids"])       # in the feed, outside the window
        self.assertIn("far@google.com", result["uids"])

    def test_utc_converted_to_local_and_text_cleaned(self):
        d = self.parsed()["dentist@google.com"]
        self.assertEqual((d["start_local"], d["end_local"], d["all_day"]), ("2026-11-17T14:30", "2026-11-17T15:30", False))
        self.assertEqual(d["notes"], "Bring the\ninsurance card & ID")
        self.assertEqual(d["location"], "Lakeside Dental")

    def test_daylight_saving(self):
        summer = feed(vevent("s@g", "Summer", "DTSTART:20270615T180000Z", "DTEND:20270615T190000Z"))
        e = ical_import.parse(summer, TODAY)["events"][0]
        self.assertEqual(e["start_local"], "2027-06-15T14:00")   # EDT: UTC-4

    def test_series_with_until_exdate_and_changed_date(self):
        c = self.parsed()["club@google.com"]
        self.assertEqual(c["rrule"], "FREQ=MONTHLY;BYDAY=1TU")
        self.assertEqual(c["recurrence_end"], "2027-07-05")
        self.assertEqual(c["exdates"], ["2026-12-01"])
        self.assertEqual(c["overrides"]["2027-01-05"]["title"], "Club holiday party")
        self.assertEqual(c["overrides"]["2027-01-05"]["start_local"], "2027-01-05T18:00")

    def test_all_day_end_is_inclusive_and_old_yearly_series_included(self):
        p = self.parsed()
        self.assertEqual((p["trip@google.com"]["start_local"], p["trip@google.com"]["end_local"]),
                         ("2026-12-10", "2026-12-13"))
        self.assertEqual(p["bday@google.com"]["rrule"], "FREQ=YEARLY")

    def test_count_rule_and_cancelled(self):
        data = feed(vevent("c@g", "Course", "DTSTART;TZID=America/New_York:20261105T100000",
                           "DTEND;TZID=America/New_York:20261105T110000", "RRULE:FREQ=WEEKLY;COUNT=3"),
                    vevent("x@g", "Called off", "DTSTART:20261120T150000Z", "DTEND:20261120T160000Z",
                           "STATUS:CANCELLED"))
        events = ical_import.parse(data, TODAY)["events"]
        self.assertEqual([e["rrule"] for e in events], ["FREQ=WEEKLY;COUNT=3"])

    def test_bad_feed_and_fetch_errors_never_show_the_address(self):
        with self.assertRaises(ical_import.FetchError):
            ical_import.parse(b"not a calendar", TODAY)
        with self.assertRaises(ical_import.FetchError) as cm:
            ical_import.fetch("http://plain.example/x.ics")
        self.assertNotIn("plain.example", str(cm.exception))


# ------------------------------------------------------------- the review


class TestReview(Base):
    def test_first_fetch_offers_new_events(self):
        counts = self.fetch()
        self.assertEqual(counts["events"], 4)
        self.assertEqual(counts["new"], 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)   # nothing added yet

    def test_bulk_add_uses_defaults_and_keeps_series_details(self):
        self.fetch()
        with self.assertRaises(importer.ImportError_):
            importer.add_with_defaults(self.conn, [i["id"] for i in self.items()])
        self.defaults()
        n = importer.add_with_defaults(self.conn, [i["id"] for i in self.items()])
        self.assertEqual(n, 4)
        club = self.event("Radio club meeting")
        self.assertEqual([p["id"] for p in club["people"]], [self.alex])
        self.assertEqual(club["category_id"], self.cat("Other"))
        self.assertEqual((club["source"], club["google_uid"], club["google_source"]), ("google", "club@google.com", "1"))
        dates = [e["start_local"] for e in store.list_events(self.conn, date(2026, 11, 1), date(2027, 2, 28))
                 if e["id"] == club["id"]]
        self.assertEqual(dates, ["2026-11-03T19:00", "2027-01-05T18:00", "2027-02-02T19:00"])   # Dec skipped, Jan changed
        occ = store.get_occurrence(self.conn, club["id"], "2027-01-05")
        self.assertEqual(occ["title"], "Club holiday party")

    def test_second_fetch_offers_nothing_new(self):
        self.defaults()
        self.fetch()
        importer.add_with_defaults(self.conn, [i["id"] for i in self.items()])
        counts = self.fetch()
        self.assertEqual(sum(counts[k] for k in importer.KINDS), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)

    def test_skip_is_remembered_and_can_be_undone(self):
        self.fetch()
        importer.skip(self.conn, self.by_title("Beach trip")["id"])
        self.fetch()
        self.assertNotIn("Beach trip", [i["g"]["title"] for i in self.items()])
        importer.unskip(self.conn, 1, "trip@google.com")
        self.fetch()
        self.assertIn("Beach trip", [i["g"]["title"] for i in self.items()])

    def test_review_and_add_through_the_form(self):
        self.defaults()
        self.fetch()
        it = self.by_title("Radio club meeting")
        form = importer.form_for_item(self.conn, it)
        self.assertEqual((form.repeat, form.nth_weekday, form.ordinals, form.until), ("monthly_nth", "TU", ["1"], "2027-07-05"))
        form.category_id = str(self.cat("Club / Ham radio"))
        form.person_ids = [str(self.alex), str(self.robin)]
        eid = importer.add_reviewed(self.conn, it["id"], form)
        ev = store.get_event(self.conn, eid)
        self.assertEqual((ev["category_id"], len(ev["people"]), ev["google_uid"]), (self.cat("Club / Ham radio"), 2, "club@google.com"))
        self.assertTrue(store.get_occurrence(self.conn, eid, "2026-12-01")["skipped"])
        counts = self.fetch()
        self.assertEqual(sum(counts[k] for k in importer.KINDS), 3)      # the other three, still new


class TestDuplicatesAndLink(Base):
    def test_hand_entered_event_is_offered_as_duplicate_and_linked(self):
        local = self.add_local(title="dentist appt", location="", notes="My note")
        self.fetch()
        dup = self.by_title("Dentist", "duplicate")
        self.assertEqual(dup["event_id"], local)
        importer.link(self.conn, dup["id"])
        ev = store.get_event(self.conn, local)
        # Local version kept: title, notes, people, category.
        self.assertEqual((ev["title"], ev["notes"], ev["category_id"], ev["google_uid"]),
                         ("dentist appt", "My note", self.cat("Medical"), "dentist@google.com"))
        self.assertEqual(ev["source"], "local")
        counts = self.fetch()
        self.assertEqual(counts["duplicate"] + counts["changed"] + counts["conflict"], 0)

    def test_hand_entered_birthday_series_matches(self):
        self.add_local(title="Mom's Birthday", all_day=True, start_date="2020-11-05", end_date="2020-11-05",
                       repeat="yearly")
        self.fetch()
        self.assertEqual(self.by_title("Mom's birthday", "duplicate")["kind"], "duplicate")

    def test_different_time_or_title_is_not_a_duplicate(self):
        self.add_local(start_time="09:00", end_time="10:00")
        self.add_local(title="Plumber")
        self.fetch()
        self.assertEqual(self.by_title("Dentist")["kind"], "new")
        self.assertIn("Plumber (2:30 PM)", importer.overlaps(self.conn, self.by_title("Dentist")["g"], TODAY))

    def test_not_the_same_moves_to_new(self):
        self.add_local()
        self.fetch()
        importer.keep_both(self.conn, self.by_title("Dentist", "duplicate")["id"])
        self.assertEqual(self.by_title("Dentist")["kind"], "new")

    def test_similarity(self):
        self.assertTrue(importer.similar("Mom's birthday", "Moms Birthday"))
        self.assertTrue(importer.similar("Dentist", "Dentist - cleaning"))
        self.assertFalse(importer.similar("Dentist", "Doctor"))


class TestChanges(Base):
    CHANGED = vevent("dentist@google.com", "Dentist", "DTSTART:20261118T193000Z", "DTEND:20261118T203000Z",
                     "LOCATION:Lakeside Dental, Suite 2")

    def setUp(self):
        super().setUp()
        self.defaults()
        self.fetch()
        importer.add_with_defaults(self.conn, [i["id"] for i in self.items()])
        self.dentist = self.event("Dentist")
        store.save_event(self.conn, store.EventInput(**dict(vars(store.event_to_input(self.dentist)),
                                                              category_id=str(self.cat("Medical")),
                                                              person_ids=[str(self.robin)])),
                         self.dentist["id"])

    def refetch_changed(self):
        self.fetch(feed(self.CHANGED, CLUB, CLUB_PARTY, BIRTHDAY, TRIP))

    def test_changed_in_google_only_people_and_category_untouched(self):
        # Changing people and category here isn't an edit of Google's fields.
        self.refetch_changed()
        it = self.by_title("Dentist", "changed")
        importer.apply_all(self.conn)
        ev = store.get_event(self.conn, it["event_id"])
        self.assertEqual((ev["start_local"], ev["location"]), ("2026-11-18T14:30", "Lakeside Dental, Suite 2"))
        self.assertEqual((ev["category_id"], [p["id"] for p in ev["people"]]), (self.cat("Medical"), [self.robin]))
        self.assertEqual(self.items(), [])
        self.refetch_changed()
        self.assertEqual(self.items(), [])          # the new version is now the reference

    def test_conflict_when_edited_here_too(self):
        store.move_from_shelf(self.conn, self.dentist["id"], None, "2026-11-19", "09:00")
        self.refetch_changed()
        it = self.by_title("Dentist", "conflict")
        importer.apply_all(self.conn)               # conflicts are never applied in bulk
        self.assertEqual(store.get_event(self.conn, it["event_id"])["start_local"], "2026-11-19T09:00")
        importer.keep_mine(self.conn, it["id"])
        self.refetch_changed()
        self.assertEqual(self.items(), [])

    def test_use_googles_version_in_a_conflict(self):
        store.move_from_shelf(self.conn, self.dentist["id"], None, "2026-11-19", "09:00")
        self.refetch_changed()
        importer.apply_change(self.conn, self.by_title("Dentist", "conflict")["id"])
        self.assertEqual(store.get_event(self.conn, self.dentist["id"])["start_local"], "2026-11-18T14:30")

    def test_series_change_in_google(self):
        club2 = CLUB.replace("EXDATE;TZID=America/New_York:20261201T190000",
                             "EXDATE;TZID=America/New_York:20261201T190000,20270202T190000")
        self.fetch(feed(DENTIST, club2, CLUB_PARTY, BIRTHDAY, TRIP))
        it = self.by_title("Radio club meeting", "changed")
        importer.apply_change(self.conn, it["id"])
        self.assertTrue(store.get_occurrence(self.conn, it["event_id"], "2027-02-02")["skipped"])

    def test_removed_from_google(self):
        self.fetch(feed(CLUB, CLUB_PARTY, BIRTHDAY, TRIP))      # dentist gone
        it = self.items("removed")[0]
        self.assertEqual(it["event_id"], self.dentist["id"])
        importer.removed_keep(self.conn, it["id"])
        ev = store.get_event(self.conn, self.dentist["id"])
        self.assertEqual((ev["google_uid"], ev["source"]), (None, "local"))
        self.fetch(feed(CLUB, CLUB_PARTY, BIRTHDAY, TRIP))
        self.assertEqual(self.items(), [])

    def test_removed_then_deleted_goes_to_recently_deleted(self):
        self.fetch(feed(CLUB, CLUB_PARTY, BIRTHDAY, TRIP))
        importer.removed_delete(self.conn, self.items("removed")[0]["id"])
        self.assertIsNone(store.get_event(self.conn, self.dentist["id"]))
        self.assertEqual(store.list_recently_deleted(self.conn)[0]["title"], "Dentist")

    def test_moved_beyond_the_window_is_not_removed(self):
        far_dentist = DENTIST.replace("20261117T19", "20280117T19").replace("20261117T20", "20280117T20")
        self.fetch(feed(far_dentist, CLUB, CLUB_PARTY, BIRTHDAY, TRIP))
        self.assertEqual(self.items("removed"), [])

    def test_deleted_here_is_not_offered_again_until_restored(self):
        store.delete_from_shelf(self.conn, self.dentist["id"], None)
        self.fetch()
        self.assertEqual(self.items(), [])
        entry = store.list_recently_deleted(self.conn)[0]
        store.restore_deleted(self.conn, entry["id"])
        self.fetch()
        self.assertEqual(self.items(), [])          # restored, still linked: nothing to do
        self.assertEqual(self.event("Dentist")["google_uid"], "dentist@google.com")


class TestTwoSources(Base):
    def test_same_event_in_two_calendars(self):
        os.environ["CAL_SOURCE_2_URL"] = SECRET.replace("0123", "9999")
        self.defaults(1)
        self.fetch(slot=1)
        importer.add_with_defaults(self.conn, [i["id"] for i in self.items()])
        self.fetch(feed(DENTIST), slot=2)
        it = self.items("same_uid")[0]
        importer.use_this_copy(self.conn, it["id"])
        self.assertEqual(self.event("Dentist")["google_source"], "2")
        self.fetch(slot=1)
        self.assertNotIn("Dentist", [i["g"]["title"] for i in self.items()])   # remembered


# ------------------------------------------------------------- pages


class TestPages(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_pages_and_the_address_never_shown(self):
        r = self.client.get("/import")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Google calendar 1", r.text)
        self.assertNotIn("private-0123", r.text)
        importer.save_source(self.conn, 1, "Family calendar", [self.alex], self.cat("Other"))
        self.fetch()
        r = self.client.get("/import/review")
        self.assertIn("Radio club meeting", r.text)
        self.assertIn("Every month on the 1st Tuesday", r.text)
        self.assertNotIn("private-0123", r.text)
        ids = [str(i["id"]) for i in self.items()]
        r = self.client.post("/import/review/add-ticked", data={"item": ids}, follow_redirects=True)
        self.assertIn("Added 4 events", r.text)
        self.assertIn("Nothing to review", self.client.get("/import/review").text)

    def test_fetch_error_is_plain_and_hides_the_address(self):
        os.environ["CAL_SOURCE_1_URL"] = "http://not-secure.example/private-0123/basic.ics"
        r = self.client.post("/import/1/fetch", follow_redirects=True)
        self.assertIn("Couldn&#39;t read the Google calendar", r.text)
        self.assertNotIn("private-0123", r.text)
        self.assertNotIn("not-secure.example", r.text)

    def test_review_and_add_form(self):
        self.fetch()
        it = self.by_title("Dentist")
        r = self.client.get(f"/import/items/{it['id']}/add")
        self.assertIn("Add from Google", r.text)
        r = self.client.post(f"/import/items/{it['id']}/add", data={
            "title": "Dentist", "start_date": "2026-11-17", "start_time": "14:30", "end_date": "2026-11-17",
            "end_time": "15:30", "category_id": str(self.cat("Medical")), "people": [str(self.robin)],
            "reminder_mode": "default", "repeat": "none"}, follow_redirects=True)
        self.assertIn("Added “Dentist”", r.text)
        self.assertEqual(self.event("Dentist")["google_uid"], "dentist@google.com")

    def test_no_source_configured(self):
        os.environ.pop("CAL_SOURCE_1_URL")
        r = self.client.get("/import")
        self.assertIn("No Google calendar is set up yet", r.text)
        self.assertIn("CAL_SOURCE_1_URL", r.text)


# --------------------------------------------------------------- migration


class TestMigration0005(unittest.TestCase):
    def test_from_a_phase_4_database_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
            for v, p in db.available_migrations():
                if v > 4:
                    break
                conn.executescript(p.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (v,))
            conn.execute("INSERT INTO events (title, start_local, end_local, category_id, created, updated)"
                         " VALUES ('Birthday', '2026-11-05', '2026-11-05', 2, 'x', 'x')")
            conn.commit()
            conn.close()
            self.assertEqual(db.migrate(path)[0], 5)   # later migrations follow it
            self.assertEqual(len(list(Path(tmp).glob("calendar.db.pre-v4-*.bak"))), 1)
            c = db.connect(path)
            self.assertEqual(c.execute("SELECT title, google_snapshot FROM events").fetchone()[:], ("Birthday", None))
            c.close()


if __name__ == "__main__":
    unittest.main()
