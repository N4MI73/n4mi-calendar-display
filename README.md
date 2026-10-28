# N4MI Calendar Display

A self-hosted household calendar: a small server on the home network, a
browser editor that also works on a phone, and a shelf-mounted touch display
(Raspberry Pi 5 running Chromium in kiosk mode).

It is independent of Google, Apple and other cloud calendars. Google Calendar
is used only as a read-only import source, and only when the owner starts an
import (a later phase).

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
- **Next:** on-screen quick add, delete and reschedule, then Google import and
  review, then hardening the Pi as an unattended appliance.

## Privacy

This repository holds code only. Real calendar data, database files, exports,
backups and import URLs are never committed. Tests and screenshots use
fictional data.

## Licence

MIT. See [LICENSE](LICENSE).
