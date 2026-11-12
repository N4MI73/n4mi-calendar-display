# Shelf display: Raspberry Pi setup

How the shelf display is built and kept running. Everything here can be
redone from scratch; nothing secret is needed except the Pi login's own
password.

## Hardware

- Raspberry Pi 5 (4 GB) with Active Cooler and the official rechargeable RTC
  battery, in an aluminium case.
- System on an NVMe SSD in a USB 3 enclosure (shows up as `/dev/sda`); no
  microSD card in normal use.
- 10.1" HDMI capacitive touch monitor, 1280×800, landscape: HDMI to the Pi's
  **HDMI 0** (`HDMI-A-1`), touch over USB, on its own 5 V supply. Its
  brightness is a button on the monitor (no software control).
- No UPS: after a power cut everything starts again by itself.

## Operating system

- Raspberry Pi OS (64-bit) with desktop, based on Debian 13 "trixie"; the
  desktop is **labwc** (Wayland). Login with automatic sign-in to the desktop.
- Moved from microSD to the SSD with SD Card Copier; optional:
  `sudo rpi-eeprom-config --edit` → `BOOT_ORDER=0xf14` (USB first).
- RTC battery charging: the rechargeable battery is only charged with
  `dtparam=rtc_bbat_vchg=3000000` in `/boot/firmware/config.txt` (confirmed
  on this Pi in Phase 6c). The clock then keeps time through outages.
- Screen blanking must stay off (the helper manages the screen). On this setup
  no `swayidle` runs; if it ever does, turn blanking off in
  `sudo raspi-config` → Display Options.

## The calendar helper and kiosk (Phase 6b)

Files are in [`display/`](../display/). Install from a clone of this
repository on the Pi (it's public, so no token is needed):

```bash
git clone https://github.com/N4MI73/n4mi-calendar-display.git ~/n4mi-calendar-display
bash ~/n4mi-calendar-display/display/install.sh
sudo reboot
```

`install.sh` asks for the login's password once (sudo) to add the permission
that lets the health panel restart or turn off the Pi
(`/etc/polkit-1/rules.d/50-n4mi-calendar.rules`). Everything else goes in the
login's home folder.

After the reboot:

- **Kiosk:** Chromium opens `http://192.168.6.30:8087/display?kiosk=1` full
  screen, using its own profile (`~/.config/calendar-kiosk`) with
  `--password-store=basic`, so no keyring prompt appears. It runs as the
  systemd user service `calendar-kiosk`, which reopens it within a few
  seconds if it closes. Both services are started by one autostart entry,
  `~/.config/autostart/n4mi-calendar.desktop`, which first hands the desktop
  session's settings (such as `WAYLAND_DISPLAY`) to systemd.
- **Helper:** `calendar-agent` (a systemd user service) reports to the server
  every 30 s, and the health button on the display turns green.
- **Night:** during dark hours (set in the editor's Settings) the helper turns
  the HDMI picture off with `wlr-randr`, so the monitor is fully dark. A touch
  turns it on for the wake time (default 120 s); the page itself makes sure
  that touch doesn't press anything. The helper reads the touch screen
  directly (`/dev/input/event*`, so the login must be in the `input` group,
  which it is by default).
- **Restart / Shut down** from the health panel reach the Pi with the
  helper's next report (within about 30 seconds). After Shut down, press the
  Pi's power button (or unplug it and plug it back in) to start it again.

### Everyday commands (over SSH)

| To | Run |
|---|---|
| Close the calendar for maintenance (it stays closed) | `calendar-kiosk stop` |
| Open it again | `calendar-kiosk start` (or reboot) |
| Is the calendar running? | `calendar-kiosk status` |
| The calendar's log | `journalctl --user -u calendar-kiosk -n 40 --no-pager` |
| See the helper's status | `systemctl --user status calendar-agent` |
| Watch the helper's log | `journalctl --user -u calendar-agent -f` |
| Send one report by hand and see the reply | `calendar-agent --once` |
| Restart the helper | `systemctl --user restart calendar-agent` |

Over SSH, commands that talk to the desktop need
`export WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u)` first
(for example `wlr-randr`).

### Updating

```bash
cd ~/n4mi-calendar-display && git pull && bash display/install.sh && sudo reboot
```

Raspberry Pi OS and Chromium updates are applied deliberately, never
automatically: `sudo apt update && sudo apt full-upgrade`, reboot, and check
that the calendar opens and the health button is green.

### Removing

```bash
systemctl --user stop calendar-agent calendar-kiosk
rm ~/.config/autostart/n4mi-calendar.desktop
rm ~/.config/systemd/user/calendar-agent.service ~/.config/systemd/user/calendar-kiosk.service
rm ~/.local/bin/calendar-agent ~/.local/bin/calendar-kiosk
sudo rm /etc/polkit-1/rules.d/50-n4mi-calendar.rules
```

## Read-only overlay (Phase 6c)

With Raspberry Pi OS's overlay file system on, the SSD is only read: every
change while the Pi runs goes to a working area in memory and is thrown away
at the next start. A power cut therefore can't damage the system, which is
why the display can simply be unplugged.

What that means day to day:

- Nothing on the Pi is saved between starts: no browser history or saved
  schedule (D65), no log files, and **no updates or setting changes** unless
  the overlay is turned off first.
- The health panel's **Storage** reading shows the in-memory working area
  while the overlay is on. It starts nearly empty after each start.
- The clock comes from the RTC battery and the network, not from a saved file.

Before turning it on, make sure the RTC battery is being charged:

```bash
grep -n rtc_bbat /boot/firmware/config.txt
cat /sys/class/rtc/rtc0/charging_voltage
```

If the first line shows nothing, add the setting, then reboot:

```bash
echo 'dtparam=rtc_bbat_vchg=3000000' | sudo tee -a /boot/firmware/config.txt
```

After the reboot `charging_voltage` reads `3000000`.

### Turning the overlay on

```bash
sudo raspi-config nonint do_overlayfs 0
sudo reboot
```

Check after the reboot (prints "overlay ON"):

```bash
grep -q ' / overlay ' /proc/mounts && echo "overlay ON" || echo "overlay OFF"
```

### Updating with the overlay (the routine)

```bash
sudo raspi-config nonint do_overlayfs 1     # 1 = off
sudo reboot
# ... reconnect, then update what's needed:
sudo apt update && sudo apt full-upgrade
cd ~/n4mi-calendar-display && git pull && bash display/install.sh
sudo reboot
# ... check the calendar opens and the health button is green, then:
sudo raspi-config nonint do_overlayfs 0     # 0 = on
sudo reboot
```

`install.sh` refuses to run while the overlay is on, as a reminder.

### Recovery tested

Phase 6c tests on this Pi (2026-11-12), with the overlay on:

- **Overlay:** on after `do_overlayfs 0`; a test file created before a
  restart was gone after it. RTC charging voltage 3000000.
- **Power pulls:** the Pi's plug pulled at random moments — idle, about 10 s
  into a boot, as the desktop appeared, with the health panel open, halfway
  through Add, during dark hours (screen dark, and just after a wake touch),
  and within 5 s of the calendar appearing. Ten pulls in all.
  Every time the calendar came back by itself in **18–22 seconds**, the health
  button turned green and the clock was right.
- **During dark hours** the calendar shows for a moment after the restart,
  then the screen goes dark again (the page needs its schedule and the helper
  its first reply before they know it's dark). Accepted: it only happens when
  power returns at night.
- **Simulated faults:** with the helper stopped, the button turned amber
  ("Pi isn't reporting") after about 5 minutes and green again once it was
  started; with the server stack stopped, amber "Can't reach the calendar"
  with the schedule still shown, and back to green when started; Restart from
  the health panel works with the overlay on.
