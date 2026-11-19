"""Coming up panel tests (v0.6.2): the display API's days range and the page's panel.

Plain Python (unittest). All data is fictional.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, store, timeutil  # noqa: E402
from app.main import create_app  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
NOW = "2026-11-19T09:40"   # a Thursday


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.alex = store.save_person(self.conn, "Alex", "#4C9AFF")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def cat(self, name):
        return self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]

    def add(self, **kw):
        base = dict(title="Car service", start_date="2026-11-24", start_time="09:15", end_date="2026-11-24",
                    end_time="10:45", category_id=str(self.cat("Home")), person_ids=[str(self.alex)])
        base.update(kw)
        if base.get("all_day"):
            base["start_time"] = base["end_time"] = ""
        return store.save_event(self.conn, store.EventInput(**base))

    def rng(self, first, count, now=NOW):
        return display.days_range(self.conn, timeutil.parse_local(now), date.fromisoformat(first), count)

    def titles(self, data):
        return [(d["date"], e["title"]) for d in data["days"] for e in d["events"]]


class TestDaysRange(Base):
    def test_thirty_days_from_tomorrow(self):
        self.add()
        data = self.rng("2026-11-20", 30)
        self.assertEqual((data["from"], data["to"]), ("2026-11-20", "2026-12-19"))
        self.assertEqual(len(data["days"]), 30)
        self.assertEqual(data["limit"], "2027-11-19")
        self.assertTrue(data["more"])
        self.assertIn(("2026-11-24", "Car service"), self.titles(data))
        ev = data["days"][4]["events"][0]
        # Same event shape as the main schedule, so the details panel works unchanged.
        for key in ("id", "occ", "time", "end_time", "people", "category", "notes", "repeat_text", "location"):
            self.assertIn(key, ev)

    def test_next_chunk_follows_on(self):
        self.add(title="Holiday party", start_date="2026-12-20", end_date="2026-12-20")
        first = self.rng("2026-11-20", 30)
        self.assertNotIn("Holiday party", [t for _, t in self.titles(first)])
        nxt = self.rng("2026-12-20", 30)
        self.assertEqual((nxt["from"], nxt["to"]), ("2026-12-20", "2027-01-18"))
        self.assertIn(("2026-12-20", "Holiday party"), self.titles(nxt))

    def test_stops_at_twelve_months(self):
        data = self.rng("2027-11-01", 30)
        self.assertEqual(data["to"], "2027-11-19")
        self.assertFalse(data["more"])
        self.assertEqual(len(data["days"]), 19)

    def test_bad_values_refused(self):
        for first, count in (("2026-11-18", 30), ("2027-11-20", 30), ("2026-11-20", 0), ("2026-11-20", 367)):
            with self.assertRaises(ValueError):
                self.rng(first, count)

    def test_repeats_and_multi_day(self):
        self.add(title="Coffee", start_date="2026-11-20", end_date="2026-11-20", start_time="10:00",
                 end_time="11:00", repeat="weekly", weekdays=["FR"])
        self.add(title="Visit", all_day=True, start_date="2026-11-25", end_date="2026-11-29")
        data = self.rng("2026-11-20", 30)
        coffee = [d for d, t in self.titles(data) if t == "Coffee"]
        self.assertEqual(coffee, ["2026-11-20", "2026-11-27", "2026-12-04", "2026-12-11", "2026-12-18"])
        visit = [e for d in data["days"] for e in d["events"] if e["title"] == "Visit"]
        self.assertEqual([e["day_index"] for e in visit], [1, 2, 3, 4, 5])
        self.assertEqual(visit[0]["until"], "Sun")
        # A later part starting mid-visit leaves it out: it was listed where it began.
        later = self.rng("2026-11-27", 5)
        self.assertNotIn("Visit", [t for _, t in self.titles(later)])

    def test_events_already_under_way_are_left_out(self):
        """An overnight event from today is in Today, not under Tomorrow (code review, v0.6.2)."""
        self.add(title="Night shift", start_date="2026-11-19", start_time="22:00", end_date="2026-11-20",
                 end_time="01:00")
        self.add(title="Camp", all_day=True, start_date="2026-11-18", end_date="2026-11-22")
        self.add(title="Late party", start_date="2026-12-19", start_time="21:00", end_date="2026-12-20",
                 end_time="00:30")
        first = self.titles(self.rng("2026-11-20", 30))
        self.assertNotIn("Night shift", [t for _, t in first])
        self.assertNotIn("Camp", [t for _, t in first])
        self.assertIn(("2026-12-19", "Late party"), first)
        self.assertNotIn("Late party", [t for _, t in self.titles(self.rng("2026-12-20", 30))])
        # The main schedule still shows them on today.
        today = display.build(self.conn, timeutil.parse_local(NOW))["days"][0]
        self.assertIn("Night shift", [e["title"] for e in today["events"]])
        self.assertIn("Camp", [e["title"] for e in today["events"]])

    def test_holidays_included_and_switch_respected(self):
        data = self.rng("2026-11-20", 30)
        self.assertEqual(next(d for d in data["days"] if d["date"] == "2026-11-26")["holidays"], ["Thanksgiving"])
        store.save_holidays(self.conn, False, ["thanksgiving"])
        data = self.rng("2026-11-20", 30)
        self.assertEqual(next(d for d in data["days"] if d["date"] == "2026-11-26")["holidays"], [])

    def test_no_household_reminders(self):
        data = self.rng("2026-11-20", 30)
        self.assertNotIn("reminders", data)

    def test_main_schedule_unchanged(self):
        self.add()
        main = display.build(self.conn, timeutil.parse_local(NOW))
        self.assertEqual(main["days"][0]["date"], "2026-11-19")
        self.assertEqual(len(main["days"]), 14 + display.CACHE_EXTRA_DAYS + 1)
        self.assertIn("reminders", main)


class TestDaysApi(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_over_http(self):
        self.add()
        r = self.client.get(f"/api/v1/display/days?from=2026-11-20&days=30&now={NOW}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["api"], 1)
        self.assertEqual(body["to"], "2026-12-19")

    def test_errors(self):
        self.assertEqual(self.client.get(f"/api/v2/display/days?from=2026-11-20&now={NOW}").status_code, 404)
        r = self.client.get(f"/api/v1/display/days?from=next-week&now={NOW}")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_request"))
        r = self.client.get(f"/api/v1/display/days?from=2026-11-20&days=lots&now={NOW}")
        self.assertEqual(r.status_code, 400)
        r = self.client.get(f"/api/v1/display/days?from=2026-11-01&now={NOW}")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_from"))
        r = self.client.get(f"/api/v1/display/days?from=2026-11-20&days=500&now={NOW}")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_days"))
        r = self.client.get("/api/v1/display/days?from=2026-11-20&now=soon")
        self.assertEqual(r.status_code, 400)

    def test_days_defaults_to_thirty(self):
        r = self.client.get(f"/api/v1/display/days?from=2026-11-20&now={NOW}")
        self.assertEqual(len(r.json()["days"]), 30)

    def test_does_not_count_as_display_contact(self):
        self.client.get("/api/v1/display/days?from=" + timeutil.now_local().date().isoformat())
        row = self.conn.execute("SELECT display_seen FROM device_state WHERE id = 1").fetchone()
        self.assertIsNone(row[0])


class TestPage(Base):
    def test_heading_button_and_panel_layer(self):
        html = TestClient(create_app(self.db_path)).get("/display").text
        self.assertIn('id="coming-h"', html)
        self.assertIn("See all", html)
        self.assertIn('id="allup"', html)
        # The panel sits under the details panel, so Close there comes back to the list.
        self.assertLess(html.index('id="allup"'), html.index('id="sheet"'))

    def test_script_uses_the_api_and_no_raw_html(self):
        js = (APP_DIR / "static" / "display.js").read_text(encoding="utf-8")
        self.assertIn("/api/v1/display/days?from=", js)
        self.assertIn("Load \" + PANEL_DAYS + \" more days", js)
        self.assertIn("Can't load more right now", js)
        self.assertIn("That's everything for the next 12 months.", js)
        self.assertEqual(js.count(".innerHTML"), 1)   # still only the fixed message text
        css = (APP_DIR / "static" / "display.css").read_text(encoding="utf-8")
        self.assertIn(".overlay.allup { z-index: 4;", css)


if __name__ == "__main__":
    unittest.main()
