#!/usr/bin/env python3
"""N4MI Calendar Display — the shelf Pi's helper (Phase 6b, decisions D61, D65).

One small program, Python standard library only, run as a systemd *user*
service inside the desktop session. It does three jobs:

1. Health: every 30 seconds it sends the Pi's readings (temperature, power,
   storage, uptime, clock, versions) to the calendar server, which works out
   the green / amber / red health button.
2. Night: during dark hours it switches the HDMI picture off with wlr-randr so
   the monitor goes fully dark, and a touch on the screen turns it back on for
   the wake time. The display page's own black night screen stays underneath,
   and the page itself makes sure the waking touch never presses a button.
3. Restart / Shut down: when the server's reply carries a request from the
   health panel, it runs a clean `systemctl reboot` or `poweroff`.

Dark hours and the wake time come from the server with every reply; if the
server can't be reached the last ones received are kept (default 11 PM–6 AM,
120 s). Nothing here is secret. Settings come from environment variables
(see calendar-agent.service).

Usage:  calendar-agent            run (normally started by systemd)
        calendar-agent --once     send one report, print it, and exit
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

VERSION = "1.1"
SERVER = os.environ.get("CAL_SERVER", "http://192.168.6.30:8087").rstrip("/")
REPORT_EVERY = int(os.environ.get("CAL_REPORT_SECONDS", "30"))
NIGHT_CONTROL = os.environ.get("CAL_NIGHT_CONTROL", "1") != "0"   # 0 = never touch the HDMI output
TIMEOUT = 10

DEFAULT_CONFIG = {"dark_start": "23:00", "dark_end": "06:00", "wake_seconds": 120}

# Linux input events: struct input_event on 64-bit = (sec, usec, type, code, value).
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY, EV_ABS = 0x01, 0x03


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ------------------------------------------------------------------ dark hours


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_dark(now: datetime, start: str, end: str) -> bool:
    """True during dark hours. Handles a window that crosses midnight (23:00–06:00)."""
    try:
        s, e = _minutes(start), _minutes(end)
    except (ValueError, AttributeError):
        s, e = _minutes(DEFAULT_CONFIG["dark_start"]), _minutes(DEFAULT_CONFIG["dark_end"])
    t = now.hour * 60 + now.minute
    if s == e:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


# ------------------------------------------------------------------ readings


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def temperature_c() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw.strip()) / 1000, 1) if raw else None
    except ValueError:
        return None


def throttled() -> int | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def disk_free_pct(path: str = "/") -> float | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    if not st.f_blocks:
        return None
    return round(100.0 * st.f_bavail / st.f_blocks, 1)


def root_fs() -> str:
    for line in (_read("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "/":
            return parts[2]
    return ""


def ssd_present() -> bool:
    """The system SSD (USB enclosure) shows up as /dev/sda."""
    return os.path.exists("/dev/sda")


def uptime_s() -> int | None:
    raw = _read("/proc/uptime")
    try:
        return int(float(raw.split()[0])) if raw else None
    except (ValueError, IndexError):
        return None


def mem_avail_pct() -> float | None:
    info = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        info[k] = v.strip().split(" ")[0]
    try:
        return round(100.0 * int(info["MemAvailable"]) / int(info["MemTotal"]), 1)
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def os_name() -> str:
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def readings(hdmi_state: str = "") -> dict:
    r = {
        "temp_c": temperature_c(),
        "throttled": throttled(),
        "disk_free_pct": disk_free_pct(),
        "ssd_present": ssd_present(),
        "uptime_s": uptime_s(),
        "mem_avail_pct": mem_avail_pct(),
        "epoch": time.time(),
        "overlay": root_fs() == "overlay",
        "helper_version": VERSION,
        "os": os_name(),
        "hdmi": hdmi_state,
    }
    return {k: v for k, v in r.items() if v is not None and v != ""}


# ------------------------------------------------------------------ the server


_reporting = {"ok": None}   # last outcome, so the log shows changes, not every report


def post(path: str, body: dict) -> dict | None:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SERVER + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"N4MI-Calendar-Agent/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if _reporting["ok"] is not False:
            log(f"report not sent: {type(exc).__name__} (will keep trying every {REPORT_EVERY} s)")
        _reporting["ok"] = False
        return None
    if _reporting["ok"] is not True:
        log("reports reaching the server")
    _reporting["ok"] = True
    return reply


def run_command(action: str) -> None:
    cmd = {"restart": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]}.get(action)
    if not cmd:
        return
    log(f"{action} requested from the health panel")
    try:
        subprocess.run(cmd, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{action} failed: {type(exc).__name__}")


# ------------------------------------------------------------------ the HDMI picture


def parse_outputs(text: str) -> list[dict]:
    """Outputs from `wlr-randr`: [{'name': 'HDMI-A-1', 'enabled': True}, ...]."""
    outs, cur = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = {"name": line.split()[0], "enabled": None}
            outs.append(cur)
        elif cur is not None:
            m = re.match(r"\s+Enabled:\s*(yes|no)", line)
            if m:
                cur["enabled"] = m.group(1) == "yes"
    return outs


def pick_output(outs: list[dict], remembered: str | None) -> str | None:
    """The real monitor: the remembered one, else the first HDMI output; never the NOOP placeholder."""
    names = [o["name"] for o in outs if not o["name"].startswith("NOOP")]
    if remembered and remembered in names:
        return remembered
    for prefer in ("HDMI-A-1", "HDMI-A-2"):
        if prefer in names:
            return prefer
    hdmi = [n for n in names if n.startswith("HDMI")]
    return hdmi[0] if hdmi else (names[0] if names else None)


class Screen:
    """Switches the monitor's picture off and on with wlr-randr."""

    def __init__(self):
        self.name: str | None = None
        self.on: bool | None = None

    def _randr(self, *args) -> str | None:
        if not shutil.which("wlr-randr"):
            return None
        try:
            p = subprocess.run(["wlr-randr", *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout if p.returncode == 0 else None

    def refresh(self) -> None:
        text = self._randr()
        if text is None:
            return
        outs = parse_outputs(text)
        self.name = pick_output(outs, self.name)
        for o in outs:
            if o["name"] == self.name:
                self.on = o["enabled"]

    def set(self, want_on: bool) -> None:
        if self.name is None:
            self.refresh()
        if self.name is None or self.on == want_on:
            return
        if self._randr("--output", self.name, "--on" if want_on else "--off") is not None:
            self.on = want_on
            log(f"picture {'on' if want_on else 'off'} ({self.name})")

    def state(self) -> str:
        if self.name is None:
            return ""
        return f"{self.name} {'on' if self.on else 'off' if self.on is False else '?'}"


# ------------------------------------------------------------------ touch


def touch_devices() -> list[str]:
    """Event devices that report absolute positions (touch screens), from /proc/bus/input/devices."""
    found = []
    for block in (_read("/proc/bus/input/devices") or "").split("\n\n"):
        ev = re.search(r"^B: EV=([0-9a-fA-F]+)", block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if not ev or not handlers:
            continue
        if not int(ev.group(1), 16) & (1 << EV_ABS):
            continue
        name = (re.search(r'^N: Name="(.*)"', block, re.M) or [None, ""])[1].lower()
        if any(word in name for word in ("mouse", "keyboard", "hdmi", "cec")):
            continue
        m = re.search(r"\b(event\d+)\b", handlers.group(1))
        if m:
            found.append("/dev/input/" + m.group(1))
    return found


class Touch:
    """Notices any touch on the screen without taking it from the desktop (read-only)."""

    def __init__(self):
        self.fds: dict[int, str] = {}
        self.next_scan = 0.0

    def scan(self) -> None:
        if time.monotonic() < self.next_scan:
            return
        self.next_scan = time.monotonic() + 60
        want = set(touch_devices())
        for fd, path in list(self.fds.items()):
            if path not in want:
                os.close(fd)
                del self.fds[fd]
        have = set(self.fds.values())
        for path in want - have:
            try:
                self.fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
                log(f"watching touch screen {path}")
            except OSError as exc:
                log(f"can't read {path}: {type(exc).__name__} (is the login in the 'input' group?)")

    def touched(self, wait: float) -> bool:
        """Wait up to `wait` seconds; True if the screen was touched."""
        self.scan()
        if not self.fds:
            time.sleep(wait)
            return False
        try:
            ready, _, _ = select.select(list(self.fds), [], [], wait)
        except (OSError, ValueError):
            self.next_scan = 0
            time.sleep(1)
            return False
        hit = False
        for fd in ready:
            try:
                data = os.read(fd, EVENT_SIZE * 64)
            except BlockingIOError:
                continue
            except OSError:
                os.close(fd)
                self.fds.pop(fd, None)
                self.next_scan = 0
                continue
            for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, _, _ = struct.unpack_from(EVENT_FMT, data, i)
                if etype in (EV_KEY, EV_ABS):
                    hit = True
        return hit


# ------------------------------------------------------------------ main loop


def main() -> int:
    if "--once" in sys.argv:
        screen = Screen()
        screen.refresh()
        body = readings(screen.state())
        print(json.dumps(body, indent=2))
        reply = post("/api/v1/device/report", body)
        print(json.dumps(reply, indent=2) if reply else "No reply from the server.")
        return 0 if reply else 1

    log(f"calendar-agent {VERSION} starting; server {SERVER}; night control {'on' if NIGHT_CONTROL else 'off'}")
    config = dict(DEFAULT_CONFIG)
    screen, touch = Screen(), Touch()
    screen.refresh()
    wake_until = 0.0
    next_report = 0.0
    while True:
        now_mono = time.monotonic()
        if now_mono >= next_report:
            next_report = now_mono + REPORT_EVERY
            reply = post("/api/v1/device/report", readings(screen.state()))
            if reply:
                cfg = reply.get("config") or {}
                for key in DEFAULT_CONFIG:
                    if key in cfg:
                        config[key] = cfg[key]
                cmd = reply.get("command")
                if cmd and cmd.get("action"):
                    if NIGHT_CONTROL:
                        screen.set(True)        # so the screen isn't left dark after a restart request
                    run_command(cmd["action"])
            screen.refresh()

        if NIGHT_CONTROL:
            dark = is_dark(datetime.now(), config["dark_start"], config["dark_end"])
            touched = touch.touched(1.0)
            if dark and touched:
                try:
                    wake_until = time.monotonic() + int(config.get("wake_seconds", 120))
                except (TypeError, ValueError):
                    wake_until = time.monotonic() + 120
            want_on = (not dark) or time.monotonic() < wake_until
            screen.set(want_on)
        else:
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
