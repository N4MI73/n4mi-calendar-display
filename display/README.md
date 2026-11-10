# display/ — the shelf Pi

Files that turn a Raspberry Pi 5 with an HDMI touch monitor into the shelf
display. No secrets are kept here. The full setup, from a blank SSD, is in
[`docs/pi-setup.md`](../docs/pi-setup.md).

| File | What it is |
|---|---|
| `calendar-agent.py` | The helper: sends the Pi's health to the server every 30 s, switches the HDMI picture off during dark hours (a touch wakes it), and runs Restart / Shut down from the health panel. Python standard library only. |
| `calendar-agent.service` | systemd *user* unit for the helper. |
| `calendar-kiosk.sh` | Runs Chromium full screen on the display page; `calendar-kiosk stop` / `start` / `status` for maintenance. |
| `calendar-kiosk.service` | systemd *user* unit for the kiosk; reopens Chromium if it closes. |
| `install.sh` | Installs or updates all of the above for the desktop login, adds the autostart entries, and installs the Restart / Shut down permission. |
| `tests/test_agent.py` | Tests for the helper (`python3 -m unittest discover -s display/tests`). |

Install or update, on the Pi:

```bash
cd ~/n4mi-calendar-display && git pull && bash display/install.sh && sudo reboot
```
