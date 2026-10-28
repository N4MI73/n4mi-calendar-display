# N4MI Calendar Display — server

FastAPI + SQLite. Serves the editor (works on a computer or a phone), the
shelf display page (`/display`) and the display's JSON API (`/api/v1/...`).

The editor manages events (one-off and repeating), household "I did it"
reminders, people, categories and display settings.

## Running it

| | |
| --- | --- |
| Host | `n4misrv` (192.168.6.30), Docker, deployed as a Portainer stack |
| Port | **8087** — open `http://192.168.6.30:8087/` |
| Image | `ghcr.io/n4mi73/n4mi-calendar-display` (tags `latest` and the short commit SHA) |
| Data | `/srv/docker/n4mi-calendar-display/data/calendar.db` on the host (bind mount to `/data`) |
| Runs as | uid 1000 (not root) |
| Access | Home network only. No login in version 1. |

### Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `TZ` | `America/New_York` | Household time zone. All times are stored as local wall-clock times in this zone. |
| `CAL_DB_PATH` | `/data/calendar.db` | Where the database lives inside the container. |

There are no secrets yet. Google import URLs arrive in a later phase and will
be set only as Portainer environment variables, never in this repository.

### How an update reaches the server

1. Commit and push to `main` (GitHub Desktop).
2. GitHub Actions runs the tests, then builds and publishes the image
   (`.github/workflows/build-image.yml`). If a test fails, nothing is published.
3. In Portainer (`n4misrv` environment): **Stacks → n4mi-calendar-display →
   Pull and redeploy**.

Portainer cannot build images on `n4misrv`, which is why the compose file only
pulls a finished image (`pull_policy: always`).

### First deployment

1. After the first workflow run, set the GitHub package
   `n4mi-calendar-display` to **Public** (GitHub → Packages → package settings
   → Change visibility). Once only.
2. On `n4misrv`, create the data folder and give it to uid 1000:

   ```bash
   sudo mkdir -p /srv/docker/n4mi-calendar-display/data
   sudo chown 1000:1000 /srv/docker/n4mi-calendar-display/data
   ```

3. Portainer → `n4misrv` environment → **Stacks → Add stack → Repository**:
   - Name: `n4mi-calendar-display`
   - Repository URL: `https://github.com/N4MI73/n4mi-calendar-display`
   - Reference: `refs/heads/main`
   - Compose path: `server/docker-compose.yml`
4. Deploy, then open `http://192.168.6.30:8087/`.

### Health checks

- `GET /healthz` → `{"ok": true}` when the web server is up (also used by the
  container's own health check).
- `GET /api/status` → `{"ok": true, "version": …, "schema": …}` when the
  database can be read; HTTP 503 with `"ok": false` when it can't. For Uptime
  Kuma, use an HTTP keyword monitor on this URL looking for `"ok":true`.

## Database and backups

- SQLite, one file. Schema changes are numbered migrations in
  `app/migrations/` and run automatically at start-up. Before applying a new
  migration to an existing database, the server writes a copy beside it named
  `calendar.db.pre-v<N>-<timestamp>.bak`.
- The host's nightly backup copies the database. Test one restore after the
  first nightly backup has run.
- Never commit a database, export or backup to this repository.

## Repeating events and reminders

- Repeat patterns are stored as standard iCalendar RRULE text (the form
  Google uses), with the series' first date and optional last date in their
  own columns. The editor offers weekly (every week or every other week, on
  chosen days), monthly on the 1st–5th or last weekday, monthly on a date,
  and yearly. Expansion uses `python-dateutil`, so any valid RRULE (for
  example one imported later) still works; the editor shows it as "set
  elsewhere".
- One date of a series can be skipped or changed (time, place, title, notes)
  without touching the others. These are kept in `event_exceptions`, keyed by
  the date the occurrence originally fell on.
- Household reminders show from their appear time (default: the end of night
  mode) until someone taps "I did it". With carry-over off they disappear at
  the end of their day; with it on they stay until done, as a single item.
  "Undo" restores the previous state.

- A repeating event can be changed or deleted for **just one date**, **from a
  date on** (the series ends the day before and a new series starts on that
  date with the same people; adjusted dates that no longer fit the new pattern
  are listed for confirmation first), or **for every date**.

## Reminders on the display

- Lead times are entered as a number and a unit (minutes, hours or days) and
  stored as minutes. Each category has a default for timed events and a
  separate default, in days, for all-day events such as birthdays (blank =
  none). An event can use the default, set its own, or have no reminder.
- The banner reads "Dentist at 2:30 PM today", then "Dentist in 45 minutes"
  (bright) from the lead time, at most 2 hours before. Earlier days read
  "Dentist tomorrow at 2:30 PM" or "Mom's birthday is Thursday". Banners never
  start during night mode; one that would is moved to the end of dark hours.
  When several calm banners apply they take turns every 10 seconds; a bright
  one always wins.

## The display page and API

- `GET /display` — the shelf screen, designed for 1280×800 (it scales with the
  screen). Everything it needs, including the Atkinson Hyperlegible font
  (`app/static/fonts/`, SIL Open Font License), is served from here; nothing
  is loaded from the internet.
  - `?now=2026-11-02T08:05` runs the page as if it were that time (a test
    clock; it keeps ticking from there).
  - `?kiosk=1` hides the mouse pointer.
- The page fetches the API every minute and keeps the last good copy in the
  browser. If the server can't be reached it keeps showing that copy, with an
  amber "Not updated since …" bar once it is older than the stale threshold.
  With no copy at all it says "Can't reach the calendar right now" — never
  "nothing scheduled". When the server has been updated, the page reloads
  itself.
- Night mode: during dark hours the page is black; a tap wakes it for the wake
  time and never presses anything underneath. (Switching the monitor's
  picture off comes with the Pi setup in Phase 6.)
- `GET /api/v1/display[?now=…]` — today plus the Upcoming range plus 7 days:
  each day's events with labels already written ("2:30 PM", "day 2 of 4"),
  people and category, and `remind_at` (when the banner starts); the
  reminders showing now; the display settings. Repeating events are already
  expanded. Fields are only ever added. An unsupported version
  (`/api/v2/...`) answers 404 with `"error": "unsupported_api_version"`.
- `POST /api/v1/reminders/{id}/done` and `/undo` — "I did it" and Undo from
  the display. 409 when there is nothing to tick or undo.

## Times

Times are stored as local wall-clock text (`2026-11-10T14:30`), not converted
to UTC, so a 2:30 PM appointment stays at 2:30 PM across daylight-saving
changes. All-day events store dates only, with an inclusive end date.

## Tests

Plain Python `unittest`, fictional data only. From the `server` folder:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The same tests run in GitHub Actions before every image build.
