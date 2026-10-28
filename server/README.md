# N4MI Calendar Display — server

FastAPI + SQLite. Serves the editor (works on a computer or a phone) and, in a
later phase, the shelf display page and its JSON API.

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
