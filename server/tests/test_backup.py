"""Backup download tests (v0.6.3). Plain Python (unittest); fictional data only."""

from __future__ import annotations

import glob
import os
import tempfile
import unittest

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, store  # noqa: E402
from app.main import create_app  # noqa: E402


class TestBackupDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        c = db.connect(self.db_path)
        alex = store.save_person(c, "Alex", "#4C9AFF")
        cat = c.execute("SELECT id FROM categories WHERE name = 'Home'").fetchone()[0]
        store.save_event(c, store.EventInput(title="Car service", start_date="2026-11-24", start_time="09:15",
                                             end_date="2026-11-24", end_time="10:45", category_id=str(cat),
                                             person_ids=[str(alex)]))
        c.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def download(self):
        r = self.client.get("/settings/backup")
        self.assertEqual(r.status_code, 200)
        path = os.path.join(self.tmp.name, "downloaded.db")
        with open(path, "wb") as f:
            f.write(r.content)
        return r, path

    def test_a_valid_complete_copy(self):
        _, path = self.download()
        c = db.connect(path)
        try:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual([r[0] for r in c.execute("SELECT title FROM events")], ["Car service"])
            self.assertEqual([r[0] for r in c.execute("SELECT name FROM people")], ["Alex"])
            live = db.connect(self.db_path)
            try:
                self.assertEqual(db.schema_version(c), db.schema_version(live))
            finally:
                live.close()
        finally:
            c.close()

    def test_restored_copy_runs_the_app(self):
        """The downloaded file is what a restore puts back: the app must open it as is."""
        _, path = self.download()
        client = TestClient(create_app(path))
        self.assertEqual(client.get("/api/status").json()["events"], 1)
        self.assertIn("Car service", client.get("/").text)

    def test_headers(self):
        r, _ = self.download()
        self.assertEqual(r.headers["cache-control"], "no-store")
        disp = r.headers["content-disposition"]
        self.assertIn("attachment", disp)
        self.assertRegex(disp, r'calendar-backup-\d{4}-\d{2}-\d{2}-\d{4}\.db')

    def test_temporary_copy_removed(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "calendar-backup-*.db")))
        self.download()
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "calendar-backup-*.db")))
        self.assertEqual(after - before, set())

    def test_settings_page_offers_it(self):
        page = self.client.get("/settings").text
        self.assertIn('href="/settings/backup"', page)
        self.assertIn("Download a backup", page)


if __name__ == "__main__":
    unittest.main()
