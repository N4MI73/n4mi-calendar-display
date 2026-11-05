"""Phase 5b tests: US holidays, the "Not in Google" list and Add to Google link,
import title rules (D60) and the clearer refusal message.

Plain Python (unittest). All data is fictional; nothing touches the network.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from unittest import mock
from urllib.parse import parse_qs, urlparse

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, google_links, holidays, ical_import, importer, store  # noqa: E402
from app.main import create_app  # noqa: E402

import test_phase5 as p5  # noqa: E402  (the tests folder is on the path under discover)

TODAY = date(2026, 11, 3)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.alex = store.save_person(self.conn, "Alex", "#4C9AFF")
        self.robin = store.save_person(self.conn, "Robin", "#F06BA8")
        os.environ["CAL_SOURCE_1_URL"] = p5.SECRET

    def tearDown(self):
        os.environ.pop("CAL_SOURCE_1_URL", None)
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

    def fetch(self, data):
        return importer.fetch_source(self.conn, 1, TODAY, fetcher=lambda url: data)


# ---------------------------------------------------------------- holidays


class TestHolidays(unittest.TestCase):
    def test_known_dates(self):
        known = {
            ("easter", 2026): date(2026, 4, 5), ("easter", 2027): date(2027, 3, 28), ("easter", 2028): date(2028, 4, 16),
            ("thanksgiving", 2026): date(2026, 11, 26), ("thanksgiving", 2027): date(2027, 11, 25),
            ("memorial", 2027): date(2027, 5, 31), ("mlk", 2027): date(2027, 1, 18),
            ("labor", 2026): date(2026, 9, 7), ("mothers", 2027): date(2027, 5, 9), ("fathers", 2027): date(2027, 6, 20),
            ("election", 2026): date(2026, 11, 3), ("election", 2027): date(2027, 11, 2),
            ("dst_start", 2027): date(2027, 3, 14), ("dst_end", 2027): date(2027, 11, 7),
            ("columbus", 2026): date(2026, 10, 12), ("presidents", 2027): date(2027, 2, 15),
        }
        for (key, year), want in known.items():
            self.assertEqual(holidays.date_of(key, year), want, key)

    def test_choices(self):
        self.assertEqual(holidays.chosen({}), holidays.FEDERAL)                 # first use: federal ones
        self.assertEqual(holidays.chosen({"holidays": "easter,bogus"}), ["easter"])
        self.assertEqual(holidays.chosen({"holidays_on": "0", "holidays": "easter"}), [])

    def test_between_spans_years(self):
        got = holidays.between(date(2026, 12, 20), date(2027, 1, 5), ["christmas", "new_year"])
        self.assertEqual(sorted(got), [date(2026, 12, 25), date(2027, 1, 1)])


class TestHolidaysOnDisplay(Base):
    def test_payload_and_settings(self):
        data = display.build(self.conn, datetime(2026, 11, 20, 8, 0))
        days = {d["date"]: d["holidays"] for d in data["days"]}
        self.assertEqual(days["2026-11-26"], ["Thanksgiving"])                  # federal: on by default
        store.save_holidays(self.conn, True, ["thanksgiving", "christmas_eve", "nonsense"])
        days = {d["date"]: d["holidays"] for d in display.build(self.conn, datetime(2026, 12, 15, 8, 0))["days"]}
        self.assertEqual(days["2026-12-24"], ["Christmas Eve"])
        self.assertEqual(days["2026-12-25"], [])                                # not ticked
        store.save_holidays(self.conn, False, ["thanksgiving"])
        data = display.build(self.conn, datetime(2026, 11, 20, 8, 0))
        self.assertFalse(any(d["holidays"] for d in data["days"]))

    def test_holidays_are_not_events(self):
        data = display.build(self.conn, datetime(2026, 11, 20, 8, 0))
        self.assertEqual([e for d in data["days"] for e in d["events"]], [])
        self.assertEqual(google_links.not_in_google(self.conn, TODAY), [])


# ------------------------------------------------------------ Not in Google


class TestNotInGoogle(Base):
    def test_list_excludes_linked_dismissed_and_outside_window(self):
        local = self.add(title="Plumber")
        dismissed = self.add(title="Pool service", start_date="2026-11-20", end_date="2026-11-20")
        self.add(title="Long ago", start_date="2026-10-01", end_date="2026-10-01")
        self.add(title="Far away", start_date="2028-01-10", end_date="2028-01-10")
        series = self.add(title="Book club", start_date="2026-11-04", end_date="2026-11-04", start_time="19:00",
                          end_time="20:30", repeat="monthly_nth", ordinals=["1"], nth_weekday="WE")
        self.add(title="Dentist", start_date="2026-11-17", end_date="2026-11-17", start_time="14:30", end_time="15:30")
        self.fetch(p5.feed(p5.DENTIST))
        importer.link(self.conn, importer.list_items(self.conn)[0]["id"])       # Dentist now tied to Google
        google_links.set_not_needed(self.conn, dismissed)
        got = [x["event"]["id"] for x in google_links.not_in_google(self.conn, TODAY)]
        self.assertEqual(got, [series, local])                                  # Book club first (Nov 4), once
        self.assertEqual([e["id"] for e in google_links.list_not_needed(self.conn)], [dismissed])
        google_links.set_not_needed(self.conn, dismissed, False)
        self.assertIn(dismissed, [x["event"]["id"] for x in google_links.not_in_google(self.conn, TODAY)])

    def test_link_formats(self):
        ev = store.get_event(self.conn, self.add(location="Kitchen", notes="Leaky tap & drain"))
        q = parse_qs(urlparse(google_links.add_to_google_url(ev)).query)
        self.assertEqual(q["action"], ["TEMPLATE"])
        self.assertEqual(q["text"], ["Plumber"])
        self.assertEqual(q["dates"], ["20261113T150000/20261113T160000"])
        self.assertEqual(q["ctz"], ["America/New_York"])
        self.assertEqual((q["location"], q["details"]), (["Kitchen"], ["Leaky tap & drain"]))
        self.assertNotIn("recur", q)
        trip = store.get_event(self.conn, self.add(title="Beach trip", all_day=True, start_date="2026-12-10",
                                                   end_date="2026-12-13"))
        self.assertEqual(parse_qs(urlparse(google_links.add_to_google_url(trip)).query)["dates"],
                         ["20261210/20261214"])                                  # Google's end date is exclusive
        club = store.get_event(self.conn, self.add(title="Club", repeat="monthly_nth", ordinals=["1"],
                                                   nth_weekday="TU", until="2027-06-30",
                                                   start_date="2026-11-03", end_date="2026-11-03"))
        self.assertEqual(parse_qs(urlparse(google_links.add_to_google_url(club)).query)["recur"],
                         ["RRULE:FREQ=MONTHLY;BYDAY=1TU;UNTIL=20270630"])

    def test_pages(self):
        client = TestClient(create_app(self.db_path))
        self.add()
        self.assertIn("Fetch from Google at least once first", client.get("/import/not-in-google").text)
        self.fetch(p5.feed(p5.DENTIST))
        r = client.get("/import/not-in-google")
        self.assertIn("Plumber", r.text)
        self.assertIn("calendar.google.com/calendar/render?action=TEMPLATE", r.text)
        self.assertNotIn("private-0123", r.text)
        eid = store.list_events(self.conn, TODAY, date(2026, 12, 31))[0]["id"]
        self.assertIn("Add to Google", client.get(f"/events/{eid}/edit").text)
        r = client.post(f"/events/{eid}/not-needed", follow_redirects=True)
        self.assertIn("Everything here is also in Google", r.text)
        self.assertIn("Put back on the list", r.text)
        r = client.get("/holidays")
        self.assertIn("Thanksgiving", r.text)
        r = client.post("/holidays", data={"on": "1", "holiday": ["easter"]}, follow_redirects=True)
        self.assertEqual(holidays.chosen(store.get_config(self.conn)), ["easter"])


# ------------------------------------------------------------ title rules


BIRTHDAY2 = p5.vevent("kate@google.com", "Kate's Birthday", "DTSTART;VALUE=DATE:19800320",
                      "DTEND;VALUE=DATE:19800321", "RRULE:FREQ=YEARLY")


class TestRules(Base):
    def setUp(self):
        super().setUp()
        self.everyone = store.save_person(self.conn, "Everyone", "#A78BFA")
        self.birthday = store.save_category(self.conn, "Birthday", "#46A758", "", default_unit="hours")
        importer.save_source(self.conn, 1, "Family calendar", [self.alex], self.cat("Other"))

    def test_rule_applies_to_bulk_add_any_capitals(self):
        importer.save_rule(self.conn, 1, "birthday", self.birthday, [self.everyone])
        self.fetch(p5.feed(p5.BIRTHDAY, BIRTHDAY2, p5.DENTIST))
        importer.add_with_defaults(self.conn, [i["id"] for i in importer.list_items(self.conn)])
        got = {e["title"]: (e["category_id"], [p["id"] for p in store.get_event(self.conn, e["id"])["people"]])
               for e in (dict(r) for r in self.conn.execute("SELECT id, title, category_id FROM events"))}
        self.assertEqual(got["Mom's birthday"], (self.birthday, [self.everyone]))
        self.assertEqual(got["Kate's Birthday"], (self.birthday, [self.everyone]))
        self.assertEqual(got["Dentist"], (self.cat("Other"), [self.alex]))       # source defaults

    def test_rule_prefills_review_form_and_partial_rules(self):
        importer.save_rule(self.conn, 1, "dentist", self.cat("Medical"), [])   # category only
        self.fetch(p5.feed(p5.DENTIST))
        form = importer.form_for_item(self.conn, importer.list_items(self.conn)[0])
        self.assertEqual((form.category_id, form.person_ids), (str(self.cat("Medical")), [str(self.alex)]))

    def test_rule_validation_and_delete(self):
        with self.assertRaises(store.ValidationError):
            importer.save_rule(self.conn, 1, "", self.birthday, [])
        with self.assertRaises(store.ValidationError):
            importer.save_rule(self.conn, 1, "birthday", "", [])               # neither category nor people
        rid = importer.save_rule(self.conn, 1, "birthday", self.birthday, [self.everyone])
        importer.delete_rule(self.conn, rid)
        self.assertEqual(importer.list_rules(self.conn, 1), [])

    def test_rules_page(self):
        client = TestClient(create_app(self.db_path))
        r = client.post("/import/sources/1/rules", data={"word": "birthday", "category_id": str(self.birthday),
                                                         "people": [str(self.everyone)]}, follow_redirects=True)
        self.assertIn("Rule added", r.text)
        self.assertIn("“birthday”", r.text)
        self.assertIn("Everyone", r.text)


# ---------------------------------------------------------- refusal message


class TestRefusalMessage(unittest.TestCase):
    def test_401_points_to_the_secret_address_and_hides_it(self):
        err = urllib.error.HTTPError(p5.SECRET, 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ical_import.FetchError) as cm:
                ical_import.fetch(p5.SECRET)
        self.assertIn("secret address", str(cm.exception))
        self.assertIn("not its public one", str(cm.exception))
        self.assertNotIn("private-0123", str(cm.exception))


class TestMigration0006(unittest.TestCase):
    def test_restore_of_an_older_snapshot(self):
        # A Recently deleted snapshot taken before migration 0006 has no not_needed_in_google field.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            db.migrate(path)
            c = db.connect(path)
            pid = store.save_person(c, "Alex", "#4C9AFF")
            eid = store.save_event(c, store.EventInput(title="Vet", start_date="2026-11-20", start_time="10:00",
                                                       end_date="2026-11-20", end_time="11:00", category_id="1",
                                                       person_ids=[str(pid)]))
            store.delete_event(c, eid)
            row = c.execute("SELECT id, snapshot FROM recently_deleted").fetchone()
            import json
            snap = json.loads(row["snapshot"])
            del snap["event"]["not_needed_in_google"]
            c.execute("UPDATE recently_deleted SET snapshot = ? WHERE id = ?", (json.dumps(snap), row["id"]))
            c.commit()
            store.restore_deleted(c, row["id"])
            self.assertEqual(c.execute("SELECT title, not_needed_in_google FROM events").fetchone()[:], ("Vet", 0))
            c.close()


if __name__ == "__main__":
    unittest.main()
