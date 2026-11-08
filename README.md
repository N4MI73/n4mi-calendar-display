# N4MI Calendar Display

A self-hosted household calendar: a small server on the home network, a
browser editor that also works on a phone, and a shelf-mounted touch display
(Raspberry Pi 5 running Chromium in kiosk mode).

It is independent of Google, Apple and other cloud calendars. Google Calendar
is used only as a read-only import source, and only when the owner starts an
import.

## What's here

| Folder | Contents |
| --- | --- |
| `server/` | The calendar server (Python, FastAPI, SQLite), the editor pages, tests, Dockerfile and compose file. See [`server/README.md`](server/README.md). |
| `.github/workflows/` | Tests the server, then builds its Docker image and publishes it to GitHub Container Registry. |
| `docs/` | Setup and reference notes (added as the project grows). |
| `display/` | Raspberry Pi kiosk and autostart configuration (a later phase). |

## Status

- **Phase 1 (server core and editor):** events with one or more people, a
  category and an optional location; people and categories; display settings.
- **Phase 2:** repeating events (with skip or change of a single date) and
  household "I did it" reminders.
- **Phase 3:** the shelf display page (`/display`) and its versioned JSON API,
  with "I did it" from the display, an offline copy and night mode; reminder
  times in minutes, hours or days, reminders for all-day events, and changing
  a repeating event from a date on.
- **Phase 4:** add, move and delete from the shelf display (with quick
  titles and its own on-screen keyboard), event details with notes, a
  scrolling Coming up, and a Recently deleted list in the editor.
- **Phase 5a:** read-only import from a Google calendar's private iCal
  address, with a review page (new, already here, changed, removed) where
  nothing changes until the owner chooses.
- **Phase 5b:** a "Not in Google" list with a pre-filled Add to Google link
  (the calendar itself never writes to Google), title rules for imports, and
  US holidays calculated on the server.
- **Phase 6a:** a health button on the shelf display (green / amber / red,
  always with a symbol and a word) with a panel of the Pi's vital signs and a
  confirmed Restart and Shut down; a Health page in the editor; and a title in
  the display's top bar.
- **Next (6b, 6c):** the Pi helper (health reports, kiosk autostart, screen
  off at night), then the read-only overlay and power-pull tests.

## Privacy

This repository holds code only. Real calendar data, database files, exports,
backups and import URLs are never committed. Tests and screenshots use
fictional data.

## Licence

MIT. See [LICENSE](LICENSE).
