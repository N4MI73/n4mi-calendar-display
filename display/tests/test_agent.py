"""Tests for the shelf Pi's helper (display/calendar-agent.py), Phase 6b.

Plain unittest, no Pi needed. Uses the real wlr-randr and /proc/bus/input/devices
text from the shelf Pi (2026-11-10). Run from the repository root:

    python3 -m unittest discover -s display/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("calendar_agent", HERE.parent / "calendar-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

RANDR_ON = '''HDMI-A-1 "Mediatrix Peripherals Inc MPI107 0x88888800 (HDMI-A-1)"
  Make: Mediatrix Peripherals Inc
  Enabled: yes
  Modes:
    1280x800 px, 60.000000 Hz (preferred, current)
'''
RANDR_OFF = '''HDMI-A-1 "Mediatrix Peripherals Inc MPI107 0x88888800 (HDMI-A-1)"
  Enabled: no
NOOP-1 "Headless output 1"
  Enabled: yes
'''
DEVICES = '''I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="vc4-hdmi-0"
H: Handlers=kbd event0
B: EV=100017

I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="vc4-hdmi-0 HDMI Jack"
H: Handlers=event1
B: EV=21

I: Bus=0003 Vendor=0000 Product=0000 Version=0000
N: Name="QDtech MPI1001"
H: Handlers=mouse0 event4
B: EV=1b

I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="pwr_button"
H: Handlers=kbd event5
B: EV=3
'''


class DarkHours(unittest.TestCase):
    def test_window_across_midnight(self):
        d = lambda h, m: datetime(2026, 11, 10, h, m)
        self.assertTrue(agent.is_dark(d(23, 0), "23:00", "06:00"))
        self.assertTrue(agent.is_dark(d(2, 30), "23:00", "06:00"))
        self.assertTrue(agent.is_dark(d(5, 59), "23:00", "06:00"))
        self.assertFalse(agent.is_dark(d(6, 0), "23:00", "06:00"))
        self.assertFalse(agent.is_dark(d(22, 59), "23:00", "06:00"))

    def test_window_within_a_day_and_equal_times(self):
        d = lambda h, m: datetime(2026, 11, 10, h, m)
        self.assertTrue(agent.is_dark(d(1, 30), "01:00", "05:00"))
        self.assertFalse(agent.is_dark(d(5, 0), "01:00", "05:00"))
        self.assertFalse(agent.is_dark(d(3, 0), "04:00", "04:00"))

    def test_bad_times_fall_back_to_defaults(self):
        self.assertTrue(agent.is_dark(datetime(2026, 11, 10, 23, 30), "x", None))


class Outputs(unittest.TestCase):
    def test_parse_and_pick_the_real_monitor(self):
        outs = agent.parse_outputs(RANDR_ON)
        self.assertEqual(outs, [{"name": "HDMI-A-1", "enabled": True}])
        self.assertEqual(agent.pick_output(outs, None), "HDMI-A-1")

    def test_placeholder_never_chosen_while_off(self):
        outs = agent.parse_outputs(RANDR_OFF)
        self.assertEqual([o["name"] for o in outs], ["HDMI-A-1", "NOOP-1"])
        self.assertEqual(agent.pick_output(outs, None), "HDMI-A-1")
        self.assertFalse(outs[0]["enabled"])

    def test_remembered_and_other_ports(self):
        outs = [{"name": "HDMI-A-2", "enabled": True}, {"name": "NOOP-1", "enabled": True}]
        self.assertEqual(agent.pick_output(outs, None), "HDMI-A-2")
        self.assertEqual(agent.pick_output(outs, "HDMI-A-2"), "HDMI-A-2")
        self.assertIsNone(agent.pick_output([{"name": "NOOP-1", "enabled": True}], None))


class TouchDevices(unittest.TestCase):
    def test_only_the_touch_screen(self):
        with mock.patch.object(agent, "_read", return_value=DEVICES):
            self.assertEqual(agent.touch_devices(), ["/dev/input/event4"])


class Readings(unittest.TestCase):
    def test_readings_are_plain_json_with_version(self):
        r = agent.readings("HDMI-A-1 on")
        json.dumps(r)
        self.assertEqual(r["helper_version"], agent.VERSION)
        self.assertEqual(r["hdmi"], "HDMI-A-1 on")
        self.assertIn("epoch", r)
        self.assertNotIn(None, r.values())

    def test_server_accepts_every_reading(self):
        """The server keeps every field the helper sends (needs the server's packages)."""
        server = HERE.parent.parent / "server"
        sys.path.insert(0, str(server))
        try:
            from app import health  # noqa: WPS433
        except ImportError:
            self.skipTest("server packages not installed")
        finally:
            sys.path.remove(str(server))
        sent = agent.readings("HDMI-A-1 on")
        unknown = set(sent) - set(health._FIELDS)
        self.assertEqual(unknown, set())

    def test_throttled_parses_vcgencmd(self):
        fake = mock.Mock(stdout="throttled=0x50005\n")
        with mock.patch.object(agent.shutil, "which", return_value="/usr/bin/vcgencmd"), \
                mock.patch.object(agent.subprocess, "run", return_value=fake):
            self.assertEqual(agent.throttled(), 0x50005)


class Commands(unittest.TestCase):
    def test_only_known_actions_run(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            agent.run_command("format")
            run.assert_not_called()
            agent.run_command("restart")
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0], ["systemctl", "reboot"])


if __name__ == "__main__":
    unittest.main()
