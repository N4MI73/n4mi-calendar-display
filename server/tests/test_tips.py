"""Tips page tests (v0.6.4). Plain Python (unittest)."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import create_app  # noqa: E402


class TestTips(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(path)
        self.client = TestClient(create_app(path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_loads_and_is_linked(self):
        r = self.client.get("/tips")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<h1>Tips</h1>", r.text)
        self.assertIn('href="/tips"', self.client.get("/settings").text)

    def test_every_link_on_the_page_works(self):
        html = self.client.get("/tips").text
        body = html.split("<h1>Tips</h1>", 1)[1]
        links = sorted(set(re.findall(r'href="(/[^"#]*)"', body)))
        self.assertGreaterEqual(len(links), 8)
        for link in links:
            self.assertEqual(self.client.get(link).status_code, 200, link)

    def test_names_match_the_editor(self):
        """The tips quote button and field names; check they still exist where they say."""
        tpl = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
        def read(name):
            with open(os.path.join(tpl, name), encoding="utf-8") as f:
                return f.read()
        checks = {
            "_repeat.html": ["Monthly, by weekday (1st Tue…)", "Doesn't repeat"],
            "occurrence.html": ["Change this date", "Skip this date", "Put back as normal",
                                "Change from this date on", "Delete from this date on", "Edit the whole series"],
            "_lead.html": ["Reminder for all-day events, such as birthdays"],
            "reminder_form.html": ["Take it off the display at the end of the day",
                                   "Keep showing it until someone taps"],
            "quick_titles.html": ["Suggest from my events"],
            "recently_deleted.html": ["Restore"],
            "not_in_google.html": ["Add to Google", "Not needed in Google"],
            "import_source.html": ["Title rules"],
            "settings.html": ["Download a backup"],
        }
        tips = read("tips.html")
        for name, phrases in checks.items():
            text = read(name)
            for phrase in phrases:
                self.assertIn(phrase, text, f"{phrase!r} not in {name}")
                self.assertIn(phrase, tips.replace("&quot;", '"'), f"{phrase!r} not in tips")


if __name__ == "__main__":
    unittest.main()
