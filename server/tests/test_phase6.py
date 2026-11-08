"""Phase 6a tests: the Pi's health reports and levels (D61), Restart / Shut down
requests, the display title (D65), and the editor's Health page.

Plain Python (unittest). All data is fictional; nothing touches the network.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ["CAL_NO_AUTOAPP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, display, health, store  # noqa: E402
from app.main import create_app  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
NOW = datetime(2026, 11, 9, 8, 5, 0)
GOOD = {"temp_c": 48.2, "throttled": 0, "disk_free_pct": 82.0, "ssd_present": True, "uptime_s": 6 * 86400 + 300,
        "mem_avail_pct": 70.0, "epoch": 1_800_000_000.0, "overlay": False, "helper_version": "1.0", "os": "Debian 13"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "calendar.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def send(self, at=NOW, server_epoch=1_800_000_000.0, **changes):
        readings = dict(GOOD, **changes)
        return health.report(self.conn, readings, at, server_epoch=server_epoch)

    def rows(self, at=NOW):
        return {r["key"]: r for r in health.evaluate(self.conn, at, "0.6.0")["rows"]}


# ----------------------------------------------------------------- levels


class LevelTests(Base):
    def test_not_set_up_before_any_report(self):
        h = health.evaluate(self.conn, NOW, "0.6.0")
        self.assertEqual(h["level"], "none")
        self.assertEqual(h["label"], "Not set up")
        self.assertFalse(h["set_up"])

    def test_display_contact_alone_does_not_make_it_set_up(self):
        health.mark_display_seen(self.conn, NOW)
        h = health.evaluate(self.conn, NOW, "0.6.0")
        self.assertEqual(h["level"], "none")
        self.assertIn("display", {r["key"] for r in h["rows"]})

    def test_all_good(self):
        self.send()
        health.mark_display_seen(self.conn, NOW)
        h = health.evaluate(self.conn, NOW + timedelta(seconds=15), "0.6.0")
        self.assertEqual(h["level"], "ok")
        self.assertEqual(h["head"], "Everything looks good")
        self.assertEqual(h["sub"], "Checked less than a minute ago")
        self.assertEqual(health.evaluate(self.conn, NOW + timedelta(minutes=3), "0.6.0")["sub"], "Checked 3 minutes ago")
        keys = [r["key"] for r in h["rows"]]
        self.assertEqual(keys, ["temperature", "power", "storage", "display", "pi", "clock"])
        self.assertTrue(all(r["level"] == "ok" for r in h["rows"]))
        self.assertEqual(h["versions"], "Calendar 0.6.0 · Pi helper 1.0 · up 6 days")

    def test_temperature_thresholds(self):
        for temp, level in ((69.9, "ok"), (70, "check"), (79.9, "check"), (80, "alert")):
            self.send(temp_c=temp)
            self.assertEqual(self.rows()["temperature"]["level"], level, temp)

    def test_power_bits(self):
        self.send(throttled=0x1)
        self.assertEqual(self.rows()["power"]["level"], "alert")
        self.send(throttled=0x10000)
        self.assertEqual(self.rows()["power"]["level"], "check")
        self.send(throttled=0x40000)
        self.assertEqual(self.rows()["power"]["level"], "check")
        self.send(throttled=0)
        self.assertEqual(self.rows()["power"]["level"], "ok")

    def test_storage(self):
        for free, level in ((15, "ok"), (14.9, "check"), (5, "check"), (4.9, "alert")):
            self.send(disk_free_pct=free)
            self.assertEqual(self.rows()["storage"]["level"], level, free)
        self.send(ssd_present=False)
        self.assertEqual(self.rows()["storage"]["level"], "alert")
        self.assertEqual(self.rows()["storage"]["value"], "Drive not found")

    def test_stale_pi_report(self):
        self.send(at=NOW)
        self.assertEqual(self.rows(NOW + timedelta(minutes=5))["pi"]["level"], "ok")
        self.assertEqual(self.rows(NOW + timedelta(minutes=6))["pi"]["level"], "check")
        self.assertEqual(self.rows(NOW + timedelta(minutes=31))["pi"]["level"], "alert")

    def test_stale_display_contact(self):
        self.send(at=NOW + timedelta(minutes=40))
        health.mark_display_seen(self.conn, NOW)
        self.assertEqual(self.rows(NOW + timedelta(minutes=40))["display"]["level"], "alert")
        health.mark_display_seen(self.conn, NOW + timedelta(minutes=34))
        self.assertEqual(self.rows(NOW + timedelta(minutes=40))["display"]["level"], "check")

    def test_clock_offset(self):
        self.send(epoch=1_800_000_030.0)
        self.assertEqual(self.rows()["clock"]["level"], "ok")
        self.send(epoch=1_800_000_000.0 + 300)
        r = self.rows()["clock"]
        self.assertEqual(r["level"], "check")
        self.assertEqual(r["value"], "Off by 5 minutes")

    def test_worst_row_sets_the_button_and_tell_name(self):
        self.send(temp_c=73, disk_free_pct=3)
        h = health.evaluate(self.conn, NOW, "0.6.0")
        self.assertEqual(h["level"], "alert")
        self.assertEqual(h["sub"], "Almost full")
        health.save_settings(self.conn, dict(store.DEFAULT_CONFIG, health_tell="Alex"))
        h = health.evaluate(self.conn, NOW, "0.6.0")
        self.assertEqual(h["sub"], "Tell Alex: almost full")

    def test_bad_readings_are_ignored(self):
        data = health.report(self.conn, {"temp_c": "hot", "throttled": True, "disk_free_pct": 250, "x": 1,
                                         "helper_version": "1.0" * 30}, NOW)
        self.assertNotIn("temp_c", data)
        self.assertNotIn("throttled", data)
        self.assertNotIn("disk_free_pct", data)
        self.assertNotIn("x", data)
        self.assertEqual(len(data["helper_version"]), 20)

    def test_thresholds_editable_and_validated(self):
        form = dict(store.DEFAULT_CONFIG, health_temp_check="60", health_temp_alert="65")
        health.save_settings(self.conn, form)
        self.send(temp_c=62)
        self.assertEqual(self.rows()["temperature"]["level"], "check")
        with self.assertRaises(store.ValidationError) as cm:
            health.save_settings(self.conn, dict(form, health_temp_alert="55"))
        self.assertIn("health_temp_alert", cm.exception.errors)
        with self.assertRaises(store.ValidationError) as cm:
            health.save_settings(self.conn, dict(form, health_disk_alert="20", health_disk_check="15"))
        self.assertIn("health_disk_alert", cm.exception.errors)


# --------------------------------------------------------------- commands


class CommandTests(Base):
    def test_one_waiting_and_delivered_once(self):
        health.request_command(self.conn, "restart", NOW)
        health.request_command(self.conn, "shutdown", NOW + timedelta(seconds=10))
        cmd = health.take_command(self.conn, NOW + timedelta(seconds=20))
        self.assertEqual(cmd["action"], "shutdown")
        self.assertIsNone(health.take_command(self.conn, NOW + timedelta(seconds=30)))

    def test_expires_after_five_minutes(self):
        health.request_command(self.conn, "restart", NOW)
        self.assertIsNone(health.take_command(self.conn, NOW + timedelta(minutes=5, seconds=1)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM device_commands").fetchone()[0], 0)

    def test_unknown_action(self):
        with self.assertRaises(store.ValidationError):
            health.request_command(self.conn, "format", NOW)


# --------------------------------------------------------------------- API


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_report_reply_carries_dark_hours_and_command(self):
        r = self.client.post("/api/v1/device/report", json=GOOD)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["config"], {"dark_start": "23:00", "dark_end": "06:00", "wake_seconds": 120})
        self.assertIsNone(body["command"])
        self.assertEqual(self.client.post("/api/v1/device/command", json={"action": "restart"}).status_code, 200)
        body = self.client.post("/api/v1/device/report", json=GOOD).json()
        self.assertEqual(body["command"]["action"], "restart")
        self.assertIsNone(self.client.post("/api/v1/device/report", json=GOOD).json()["command"])

    def test_command_refused_before_set_up(self):
        r = self.client.post("/api/v1/device/command", json={"action": "restart"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "not_set_up")

    def test_command_and_report_errors(self):
        self.client.post("/api/v1/device/report", json=GOOD)
        self.assertEqual(self.client.post("/api/v1/device/command", json={"action": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/device/command", content=b"nope").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/device/report", content=b"nope").status_code, 400)
        self.assertEqual(self.client.post("/api/v2/device/report", json=GOOD).status_code, 404)
        self.assertEqual(self.client.post("/api/v2/device/command", json={"action": "restart"}).status_code, 404)

    def test_display_payload_has_health_and_title(self):
        data = self.client.get("/api/v1/display").json()
        self.assertEqual(data["title"], "Family Calendar")
        self.assertEqual(data["health"]["level"], "none")
        self.assertEqual(set(data["health"]), {"level", "label", "head", "sub", "rows", "versions", "set_up"})
        self.client.post("/api/v1/device/report", json=dict(GOOD, epoch=None))
        data = self.client.get("/api/v1/display").json()
        self.assertEqual(data["health"]["level"], "ok")

    def test_display_seen_only_for_the_real_display(self):
        self.client.get("/api/v1/display?now=2026-11-09T08:00")
        self.assertIsNone(health.state(self.conn)["display_seen"])
        self.client.get("/api/v1/display")
        self.assertIsNotNone(health.state(self.conn)["display_seen"])

    def test_health_page_and_save(self):
        page = self.client.get("/health")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Not set up yet", page.text)
        form = {k: store.DEFAULT_CONFIG[k] for k in health.THRESHOLD_KEYS}
        form["health_tell"] = "Alex"
        r = self.client.post("/health", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(store.get_config(self.conn)["health_tell"], "Alex")
        bad = dict(form, health_temp_check="abc")
        r = self.client.post("/health", data=bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Enter a whole number.", r.text)
        self.assertIn('href="/health"', self.client.get("/settings").text)

    def test_title_setting(self):
        form = {k: store.DEFAULT_CONFIG[k] for k in store.DEFAULT_CONFIG}
        form["display_title"] = "The Smiths"
        self.client.post("/settings", data=form)
        self.assertEqual(self.client.get("/api/v1/display").json()["title"], "The Smiths")
        form["display_title"] = ""
        self.client.post("/settings", data=form)
        self.assertEqual(self.client.get("/api/v1/display").json()["title"], "")
        form["display_title"] = "x" * 41
        r = self.client.post("/settings", data=form)
        self.assertEqual(r.status_code, 400)

    def test_display_page_has_title_and_health_button(self):
        page = self.client.get("/display").text
        self.assertIn('id="title"', page)
        self.assertIn('id="health-btn"', page)
        js = (APP_DIR / "static" / "display.js").read_text(encoding="utf-8")
        for needle in ('"/api/v1/device/command"', "drawHealth", "drawDeviceConfirm", "not_set_up"):
            self.assertIn(needle, js)


class MigrationTests(unittest.TestCase):
    def test_migration_7_creates_device_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calendar.db")
            db.migrate(path)
            conn = sqlite3.connect(path)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"device_state", "device_commands"} <= tables)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM device_state").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 7)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
