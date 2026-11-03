"""N4MI Calendar Display — server.

A FastAPI app with a server-rendered editor for events (including repeating
ones), household "I did it" reminders, people, categories and settings, plus
the shelf display page (/display) and its versioned JSON API (/api/v1/...).
LAN only, no login (decision D10).

Run locally:  uvicorn app.main:app --port 8087
Environment:  CAL_DB_PATH (default /data/calendar.db), TZ (default America/New_York)
"""

from __future__ import annotations

import os
import sqlite3
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, display, ical_import, importer, recurrence, store, timeutil

APP_VERSION = "0.5.0"
BASE_DIR = Path(__file__).parent


def _fmt_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "No reminder"
    if minutes == 0:
        return "At the start time"
    if minutes % 1440 == 0:
        n = minutes // 1440
        return f"{n} day{'s' if n != 1 else ''} before"
    if minutes % 60 == 0:
        n = minutes // 60
        return f"{n} hour{'s' if n != 1 else ''} before"
    return f"{minutes} minutes before"


def _fmt_days(days: int | None) -> str:
    if days is None:
        return "No reminder"
    if days == 0:
        return "On the day"
    return f"{days} day{'s' if days != 1 else ''} before"


def _event_when(event: dict) -> str:
    start = timeutil.parse_local(event["start_local"])
    end = timeutil.parse_local(event["end_local"])
    if event["all_day"]:
        if start.date() == end.date():
            return "All day"
        return f"All day, until {end.strftime('%a %b')} {end.day}"
    text = f"{timeutil.fmt_time(start)} – {timeutil.fmt_time(end)}"
    if end.date() != start.date():
        text += f" ({end.strftime('%a')})"
    return text


def _event_date(event: dict) -> str:
    d = timeutil.parse_local(event["start_local"])
    return f"{d.strftime('%a, %b')} {d.day}, {d.year}"


def _event_start_date(event: dict) -> date:
    return timeutil.parse_local(event["start_local"]).date()


def _fmt_when(value) -> str:
    """'Mon, Nov 2, 6:00 AM' for a datetime or stored local text."""
    dt = timeutil.parse_local(value) if isinstance(value, str) else value
    text = f"{dt.strftime('%a, %b')} {dt.day}"
    if dt.year != timeutil.now_local().year:
        text += f", {dt.year}"
    return text + ", " + timeutil.fmt_time(dt)


def _date_label(d: str) -> str:
    x = timeutil.parse_date(d)
    return f"{x.strftime('%a, %b')} {x.day}, {x.year}"


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("CAL_DB_PATH", "/data/calendar.db")
    db.migrate(db_path)

    app = FastAPI(title="N4MI Calendar Display", version=APP_VERSION, docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals.update(
        app_version=APP_VERSION,
        fmt_minutes=_fmt_minutes,
        fmt_days=_fmt_days,
        event_when=_event_when,
        event_date=_event_date,
        event_start_date=_event_start_date,
        describe=recurrence.describe,
        fmt_when=_fmt_when,
        fmt_deleted=lambda t: _fmt_when(t[:16]),
    )

    def conn() -> sqlite3.Connection:
        return db.connect(app.state.db_path)

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def redirect(path: str, msg: str = "") -> RedirectResponse:
        url = path + (("?" + urlencode({"msg": msg})) if msg else "")
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------ health

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/status")
    def api_status():
        try:
            c = conn()
            try:
                version = db.schema_version(c)
                events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                people = c.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            finally:
                c.close()
        except Exception as exc:  # missing mount, unreadable file, ...
            return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)
        return {"ok": True, "version": APP_VERSION, "schema": version, "events": events, "people": people}

    # ------------------------------------------------------------ display (Phase 3)

    def _api_error(status: int, error: str, **extra) -> JSONResponse:
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=status)

    def _moment(now: str | None) -> datetime | None:
        """The local time to answer for: now, or the test clock (?now=YYYY-MM-DDTHH:MM)."""
        if not now:
            return timeutil.now_local()
        try:
            return datetime.strptime(now.strip()[:16], timeutil.DT_FMT)
        except ValueError:
            return None

    def _unsupported(version: int) -> JSONResponse:
        return _api_error(404, "unsupported_api_version", supported=[display.API_VERSION])

    @app.get("/display", response_class=HTMLResponse)
    def display_page(request: Request):
        return templates.TemplateResponse(request, "display.html", {"api_version": display.API_VERSION})

    @app.get("/api/v{version:int}/display")
    def api_display(version: int, now: str | None = None):
        if version != display.API_VERSION:
            return _unsupported(version)
        moment = _moment(now)
        if moment is None:
            return _api_error(400, "bad_now", hint="Use now=YYYY-MM-DDTHH:MM")
        c = conn()
        try:
            data = display.build(c, moment)
        finally:
            c.close()
        data["app_version"] = APP_VERSION
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.post("/api/v{version:int}/reminders/{reminder_id}/{action}")
    def api_reminder(version: int, reminder_id: int, action: str, now: str | None = None):
        """"I did it" (action=done) and Undo (action=undo) from the display (D40)."""
        if version != display.API_VERSION:
            return _unsupported(version)
        if action not in ("done", "undo"):
            return _api_error(404, "unknown_action")
        moment = _moment(now)
        if moment is None:
            return _api_error(400, "bad_now", hint="Use now=YYYY-MM-DDTHH:MM")
        c = conn()
        try:
            if not store.get_reminder(c, reminder_id):
                return _api_error(404, "no_such_reminder")
            if action == "done":
                ok = store.mark_reminder_done(c, reminder_id, moment)
            else:
                ok = store.undo_reminder_done(c, reminder_id)
        finally:
            c.close()
        if not ok:
            return _api_error(409, "not_showing" if action == "done" else "nothing_to_undo")
        return {"ok": True, "id": reminder_id, "action": action}

    # ------------------------------------------------ shelf actions (Phase 4)

    async def _body(request: Request) -> dict | None:
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _shelf_call(version: int, fn):
        """Run one shelf action with the API's error handling (D48)."""
        if version != display.API_VERSION:
            return _unsupported(version)
        c = conn()
        try:
            try:
                return fn(c)
            except store.ValidationError as exc:
                return _api_error(400, "invalid", fields=exc.errors)
            except store.NotFoundError as exc:
                return _api_error(404, "not_found", message=str(exc))
        finally:
            c.close()

    @app.post("/api/v{version:int}/events")
    async def api_add_event(version: int, request: Request):
        """Quick add from the shelf: title, date, time or all day, people, category."""
        body = await _body(request)
        if body is None:
            return _api_error(400, "bad_json")

        def go(c):
            event_id = store.add_from_shelf(
                c, str(body.get("title", "")), str(body.get("date", "")), str(body.get("time", "")),
                bool(body.get("all_day")), body.get("people") or [], body.get("category_id"),
            )
            return {"ok": True, "id": event_id}
        return _shelf_call(version, go)

    @app.post("/api/v{version:int}/events/{event_id}/move")
    async def api_move_event(version: int, event_id: int, request: Request):
        """Move one event, or one date of a repeating event (`occ`), keeping its length."""
        body = await _body(request)
        if body is None:
            return _api_error(400, "bad_json")

        def go(c):
            moved = store.move_from_shelf(c, event_id, body.get("occ"), str(body.get("date", "")),
                                          str(body.get("time", "")))
            return {"ok": True, "id": event_id, **moved}
        return _shelf_call(version, go)

    @app.post("/api/v{version:int}/events/{event_id}/delete")
    async def api_delete_event(version: int, event_id: int, request: Request):
        """Delete a one-off event, or one date of a repeating event (`occ`). Restorable for 3 days (D51)."""
        body = await _body(request)
        if body is None:
            return _api_error(400, "bad_json")

        def go(c):
            what = store.delete_from_shelf(c, event_id, body.get("occ"))
            return {"ok": True, "id": event_id, "deleted": what}
        return _shelf_call(version, go)

    # ------------------------------------------------------------ web app

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    # ------------------------------------------------------------ events

    @app.get("/", response_class=HTMLResponse)
    def events_page(request: Request, show: str = "upcoming"):
        today = timeutil.now_local().date()
        if show == "past":
            start, end = today - timedelta(days=60), today - timedelta(days=1)
        else:
            show = "upcoming"
            start, end = today, today + timedelta(days=365)
        c = conn()
        try:
            events = store.list_events(c, start, end)
            people = store.list_people(c)
        finally:
            c.close()
        groups: OrderedDict[date, list[dict]] = OrderedDict()
        for e in events:
            day = max(timeutil.parse_local(e["start_local"]).date(), start)
            groups.setdefault(day, []).append(e)
        # Days in calendar order (newest first for the past view). An event that
        # began before the window is listed on the window's first day.
        groups = OrderedDict(sorted(groups.items(), reverse=(show == "past")))
        day_groups = [(timeutil.fmt_day(d, today), evs) for d, evs in groups.items()]
        return render(request, "events.html", day_groups=day_groups, show=show, no_people=not people)

    def _exception_rows(c, event_id):
        event = store.get_event(c, event_id)
        if not event or not event["rrule"]:
            return []
        rows = []
        for occ_date, x in store.get_exceptions(c, event_id).items():
            change = ""
            if not x["skipped"] and x["start_local"]:
                change = f"{_event_date(x)} · {_event_when(dict(x, all_day=event['all_day']))}"
            rows.append({"occ_date": occ_date, "label": _date_label(occ_date), "skipped": x["skipped"], "change": change})
        return rows

    def _event_form_page(request, c, form: store.EventInput, event_id=None, errors=None, status=200,
                         future=None, dropped=None, shelf_added=False):
        """future: an occurrence date when changing "this and all later dates" (D46)."""
        return render(
            request,
            "event_form.html",
            status_code=status,
            form=form,
            event_id=event_id,
            errors=errors or {},
            people=store.list_people(c),
            categories=store.list_categories(c),
            exceptions=_exception_rows(c, event_id) if event_id and not future else [],
            future=future,
            future_label=_date_label(future) if future else "",
            dropped=[_date_label(x["occ_date"]) + (" (skipped)" if x["skipped"] else " (changed)")
                     for x in (dropped or [])],
            shelf_added=shelf_added,
        )

    def _form_to_input(form) -> store.EventInput:
        return store.EventInput(
            title=form.get("title", ""),
            location=form.get("location", ""),
            notes=form.get("notes", ""),
            all_day=form.get("all_day") == "1",
            start_date=form.get("start_date", ""),
            start_time=form.get("start_time", ""),
            end_date=form.get("end_date", ""),
            end_time=form.get("end_time", ""),
            category_id=form.get("category_id", ""),
            person_ids=form.getlist("people"),
            reminder_mode=form.get("reminder_mode", "default"),
            reminder_minutes=form.get("reminder_minutes", ""),
            reminder_unit=form.get("reminder_unit", "minutes"),
            **_repeat_from_form(form, default="none"),
        )

    def _repeat_from_form(form, default):
        return dict(
            repeat=form.get("repeat", default),
            interval=form.get("interval", "1"),
            weekdays=form.getlist("weekdays"),
            ordinals=form.getlist("ordinals"),
            nth_weekday=form.get("nth_weekday", ""),
            until=form.get("until", ""),
            custom_rrule=form.get("custom_rrule", ""),
        )

    @app.get("/events/new", response_class=HTMLResponse)
    def event_new(request: Request):
        now = timeutil.now_local()
        start = (now + timedelta(hours=1)).replace(minute=0)
        c = conn()
        try:
            people = store.list_people(c)
            categories = store.list_categories(c)
            default_cat = next((k for k in categories if k["name"] == "Other"), categories[0] if categories else None)
            form = store.EventInput(
                start_date=start.strftime(timeutil.DATE_FMT),
                start_time=start.strftime("%H:%M"),
                end_date=start.strftime(timeutil.DATE_FMT),
                end_time=(start + timedelta(hours=1)).strftime("%H:%M"),
                category_id=str(default_cat["id"]) if default_cat else "",
                person_ids=[str(people[0]["id"])] if len(people) == 1 else [],
                **store.repeat_defaults(start.strftime(timeutil.DATE_FMT)),
            )
            return _event_form_page(request, c, form)
        finally:
            c.close()

    @app.post("/events/new", response_class=HTMLResponse)
    async def event_create(request: Request):
        form = _form_to_input(await request.form())
        c = conn()
        try:
            try:
                store.save_event(c, form)
            except store.ValidationError as exc:
                return _event_form_page(request, c, form, errors=exc.errors, status=400)
        finally:
            c.close()
        return redirect("/", f"Saved “{form.title.strip()}”.")

    @app.get("/events/{event_id}/edit", response_class=HTMLResponse)
    def event_edit(request: Request, event_id: int):
        c = conn()
        try:
            event = store.get_event(c, event_id)
            if not event:
                return redirect("/", "That event no longer exists.")
            return _event_form_page(request, c, store.event_to_input(event), event_id=event_id,
                                    shelf_added=bool(event.get("added_on_shelf")))
        finally:
            c.close()

    @app.post("/events/{event_id}/edit", response_class=HTMLResponse)
    async def event_update(request: Request, event_id: int):
        form = _form_to_input(await request.form())
        c = conn()
        try:
            if not store.get_event(c, event_id):
                return redirect("/", "That event no longer exists.")
            try:
                store.save_event(c, form, event_id)
            except store.ValidationError as exc:
                return _event_form_page(request, c, form, event_id=event_id, errors=exc.errors, status=400)
        finally:
            c.close()
        return redirect("/", f"Saved “{form.title.strip()}”.")

    @app.get("/events/{event_id}/delete", response_class=HTMLResponse)
    def event_delete_confirm(request: Request, event_id: int):
        c = conn()
        try:
            event = store.get_event(c, event_id)
        finally:
            c.close()
        if not event:
            return redirect("/", "That event no longer exists.")
        return render(request, "confirm_delete.html", event=event)

    @app.post("/events/{event_id}/delete")
    def event_delete(event_id: int):
        c = conn()
        try:
            event = store.get_event(c, event_id)
            if event:
                store.delete_event(c, event_id)
        finally:
            c.close()
        return redirect("/", f"Deleted “{event['title']}”." if event else "That event no longer exists.")

    # ------------------------------------------------- one date of a series

    def _load_occurrence(c, event_id, occ_date):
        event = store.get_event(c, event_id)
        occ = store.get_occurrence(c, event_id, occ_date) if event else None
        return event, occ

    @app.get("/events/{event_id}/occurrence/{occ_date}", response_class=HTMLResponse)
    def occurrence_page(request: Request, event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
        finally:
            c.close()
        if not occ:
            return redirect("/", "That date is no longer part of the series.")
        return render(request, "occurrence.html", event=event, occ=occ)

    def _occurrence_form(request, event, occ_date, form, errors=None, status=200):
        return render(
            request, "occurrence_form.html", status_code=status, event=event, occ_date=occ_date,
            form=form, errors=errors or {}, original_label=_date_label(occ_date),
        )

    @app.get("/events/{event_id}/occurrence/{occ_date}/edit", response_class=HTMLResponse)
    def occurrence_edit(request: Request, event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
        finally:
            c.close()
        if not occ:
            return redirect("/", "That date is no longer part of the series.")
        return _occurrence_form(request, event, occ_date, store.occurrence_to_input(occ))

    @app.post("/events/{event_id}/occurrence/{occ_date}/edit", response_class=HTMLResponse)
    async def occurrence_update(request: Request, event_id: int, occ_date: str):
        f = await request.form()
        form = store.OccurrenceInput(**{k: f.get(k, "") for k in store.OccurrenceInput.__dataclass_fields__})
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
            if not occ:
                return redirect("/", "That date is no longer part of the series.")
            try:
                store.change_occurrence(c, event, occ_date, form)
            except store.ValidationError as exc:
                return _occurrence_form(request, event, occ_date, form, exc.errors, status=400)
        finally:
            c.close()
        return redirect("/", f"Changed “{form.title.strip()}” for {_date_label(occ_date)} only.")

    @app.get("/events/{event_id}/occurrence/{occ_date}/edit-future", response_class=HTMLResponse)
    def occurrence_edit_future(request: Request, event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
            if not occ:
                return redirect("/", "That date is no longer part of the series.")
            return _event_form_page(request, c, store.future_input(event, occ_date), event_id=event_id,
                                    future=occ_date)
        finally:
            c.close()

    @app.post("/events/{event_id}/occurrence/{occ_date}/edit-future", response_class=HTMLResponse)
    async def occurrence_update_future(request: Request, event_id: int, occ_date: str):
        f = await request.form()
        form = _form_to_input(f)
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
            if not occ:
                return redirect("/", "That date is no longer part of the series.")
            try:
                dropped = store.split_dropped(c, event_id, occ_date, form)
                if dropped and f.get("confirm") != "1":
                    # Show which adjusted dates would go back to normal before saving.
                    return _event_form_page(request, c, form, event_id=event_id, future=occ_date, dropped=dropped)
                store.split_series(c, event_id, occ_date, form)
            except store.ValidationError as exc:
                return _event_form_page(request, c, form, event_id=event_id, errors=exc.errors, status=400,
                                        future=occ_date)
        finally:
            c.close()
        return redirect("/", f"Changed “{form.title.strip()}” from {_date_label(occ_date)} on.")

    @app.get("/events/{event_id}/occurrence/{occ_date}/delete-future", response_class=HTMLResponse)
    def occurrence_delete_future_confirm(request: Request, event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
        finally:
            c.close()
        if not occ:
            return redirect("/", "That date is no longer part of the series.")
        return render(request, "confirm_delete_future.html", event=event, occ_date=occ_date,
                      occ_label=_date_label(occ_date))

    @app.post("/events/{event_id}/occurrence/{occ_date}/delete-future")
    def occurrence_delete_future(event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
            whole = store.end_series_from(c, event_id, occ_date) if occ else None
        finally:
            c.close()
        if not occ:
            return redirect("/", "That date is no longer part of the series.")
        if whole:
            return redirect("/", f"Deleted every date of “{event['title']}”.")
        return redirect("/", f"Deleted “{event['title']}” from {_date_label(occ_date)} on. Earlier dates stay.")

    @app.post("/events/{event_id}/occurrence/{occ_date}/skip")
    def occurrence_skip(event_id: int, occ_date: str):
        c = conn()
        try:
            event, occ = _load_occurrence(c, event_id, occ_date)
            if occ:
                store.skip_occurrence(c, event_id, occ_date)
        finally:
            c.close()
        if not occ:
            return redirect("/", "That date is no longer part of the series.")
        return redirect("/", f"Skipped “{event['title']}” on {_date_label(occ_date)}.")

    @app.post("/events/{event_id}/occurrence/{occ_date}/restore")
    def occurrence_restore(event_id: int, occ_date: str):
        c = conn()
        try:
            event = store.get_event(c, event_id)
            if event:
                store.restore_occurrence(c, event_id, occ_date)
        finally:
            c.close()
        if not event:
            return redirect("/", "That event no longer exists.")
        return redirect(f"/events/{event_id}/edit", f"{_date_label(occ_date)} is back to normal.")

    # ------------------------------------------------------------ reminders

    def _dark_end_label(c):
        return timeutil.fmt_time(timeutil.parse_time(store.get_config(c)["dark_end"]))

    def _reminder_form(request, c, form, reminder_id=None, errors=None, status=200):
        return render(
            request, "reminder_form.html", status_code=status, form=form, reminder_id=reminder_id,
            errors=errors or {}, dark_end_label=_dark_end_label(c),
        )

    def _reminder_input(f) -> store.ReminderInput:
        return store.ReminderInput(
            text=f.get("text", ""),
            start_date=f.get("start_date", ""),
            appear_time=f.get("appear_time", ""),
            carry_over=f.get("carry_over") == "1",
            active=f.get("paused") != "1",
            **_repeat_from_form(f, default="weekly"),
        )

    @app.get("/reminders", response_class=HTMLResponse)
    def reminders_page(request: Request, undo: int | None = None):
        c = conn()
        try:
            reminders = store.list_reminders(c)
        finally:
            c.close()
        return render(request, "reminders.html", reminders=reminders, undo_id=undo)

    @app.get("/reminders/new", response_class=HTMLResponse)
    def reminder_new(request: Request):
        today = timeutil.now_local().date().strftime(timeutil.DATE_FMT)
        defaults = store.repeat_defaults(today)
        defaults["repeat"] = "weekly"
        c = conn()
        try:
            return _reminder_form(request, c, store.ReminderInput(start_date=today, **defaults))
        finally:
            c.close()

    @app.post("/reminders/new", response_class=HTMLResponse)
    async def reminder_create(request: Request):
        form = _reminder_input(await request.form())
        c = conn()
        try:
            try:
                store.save_reminder(c, form)
            except store.ValidationError as exc:
                return _reminder_form(request, c, form, errors=exc.errors, status=400)
        finally:
            c.close()
        return redirect("/reminders", f"Saved “{form.text.strip()}”.")

    @app.get("/reminders/{reminder_id}/edit", response_class=HTMLResponse)
    def reminder_edit(request: Request, reminder_id: int):
        c = conn()
        try:
            rem = store.get_reminder(c, reminder_id)
            if not rem:
                return redirect("/reminders", "That reminder no longer exists.")
            return _reminder_form(request, c, store.reminder_to_input(rem), reminder_id)
        finally:
            c.close()

    @app.post("/reminders/{reminder_id}/edit", response_class=HTMLResponse)
    async def reminder_update(request: Request, reminder_id: int):
        form = _reminder_input(await request.form())
        c = conn()
        try:
            if not store.get_reminder(c, reminder_id):
                return redirect("/reminders", "That reminder no longer exists.")
            try:
                store.save_reminder(c, form, reminder_id)
            except store.ValidationError as exc:
                return _reminder_form(request, c, form, reminder_id, exc.errors, status=400)
        finally:
            c.close()
        return redirect("/reminders", f"Saved “{form.text.strip()}”.")

    @app.post("/reminders/{reminder_id}/delete")
    def reminder_delete(reminder_id: int):
        c = conn()
        try:
            rem = store.get_reminder(c, reminder_id)
            if rem:
                store.delete_reminder(c, reminder_id)
        finally:
            c.close()
        return redirect("/reminders", f"Deleted “{rem['text']}”." if rem else "That reminder no longer exists.")

    @app.post("/reminders/{reminder_id}/done")
    def reminder_done(reminder_id: int):
        c = conn()
        try:
            rem = store.get_reminder(c, reminder_id)
            ok = store.mark_reminder_done(c, reminder_id) if rem else False
        finally:
            c.close()
        if not ok:
            return redirect("/reminders", "That reminder isn't showing right now.")
        return RedirectResponse(
            "/reminders?" + urlencode({"msg": f"Done: “{rem['text']}”.", "undo": reminder_id}), status_code=303
        )

    @app.post("/reminders/{reminder_id}/undo")
    def reminder_undo(reminder_id: int):
        c = conn()
        try:
            rem = store.get_reminder(c, reminder_id)
            ok = store.undo_reminder_done(c, reminder_id) if rem else False
        finally:
            c.close()
        return redirect("/reminders", f"Undone: “{rem['text']}” is back." if ok else "Nothing to undo.")

    # ------------------------------------------------------------ people

    @app.get("/people", response_class=HTMLResponse)
    def people_page(request: Request):
        c = conn()
        try:
            return render(
                request, "people.html",
                people=store.list_people(c),
                new={"name": "", "color": store.suggest_person_color(c)},
                errors={},
            )
        finally:
            c.close()

    @app.post("/people", response_class=HTMLResponse)
    async def person_create(request: Request):
        f = await request.form()
        name, color = f.get("name", ""), f.get("color", "")
        c = conn()
        try:
            try:
                store.save_person(c, name, color)
            except store.ValidationError as exc:
                return render(
                    request, "people.html", status_code=400,
                    people=store.list_people(c), new={"name": name, "color": color}, errors=exc.errors,
                )
        finally:
            c.close()
        return redirect("/people", f"Added {name.strip()}.")

    @app.get("/people/{person_id}/edit", response_class=HTMLResponse)
    def person_edit(request: Request, person_id: int):
        c = conn()
        try:
            person = store.get_person(c, person_id)
        finally:
            c.close()
        if not person:
            return redirect("/people", "That person no longer exists.")
        return render(request, "person_form.html", person=person, errors={})

    @app.post("/people/{person_id}/edit", response_class=HTMLResponse)
    async def person_update(request: Request, person_id: int):
        f = await request.form()
        name, color = f.get("name", ""), f.get("color", "")
        c = conn()
        try:
            if not store.get_person(c, person_id):
                return redirect("/people", "That person no longer exists.")
            try:
                store.save_person(c, name, color, person_id)
            except store.ValidationError as exc:
                person = {"id": person_id, "name": name, "color": color}
                return render(request, "person_form.html", status_code=400, person=person, errors=exc.errors)
        finally:
            c.close()
        return redirect("/people", f"Saved {name.strip()}.")

    @app.post("/people/{person_id}/delete")
    def person_delete(person_id: int):
        c = conn()
        try:
            person = store.get_person(c, person_id)
            if not person:
                return redirect("/people", "That person no longer exists.")
            try:
                store.delete_person(c, person_id)
            except store.InUseError as exc:
                return redirect(
                    "/people",
                    f"{person['name']} is on {exc.count} event{'s' if exc.count != 1 else ''}, so can't be removed. "
                    "Change or delete those events first.",
                )
        finally:
            c.close()
        return redirect("/people", f"Removed {person['name']}.")

    # ------------------------------------------------------------ categories

    def _category_form(f) -> dict:
        return {
            "name": f.get("name", ""), "color": f.get("color", ""),
            "default_reminder": f.get("default_reminder", ""), "default_unit": f.get("default_unit", "minutes"),
            "allday_days": f.get("allday_days", ""),
        }

    @app.get("/categories", response_class=HTMLResponse)
    def categories_page(request: Request):
        c = conn()
        try:
            return render(
                request, "categories.html",
                categories=store.list_categories(c),
                new={"name": "", "color": "#8B8D98", "default_reminder": "1", "default_unit": "hours", "allday_days": ""},
                errors={},
            )
        finally:
            c.close()

    @app.post("/categories", response_class=HTMLResponse)
    async def category_create(request: Request):
        f = await request.form()
        new = _category_form(f)
        c = conn()
        try:
            try:
                store.save_category(c, new["name"], new["color"], new["default_reminder"],
                                    default_unit=new["default_unit"], allday_days=new["allday_days"])
            except store.ValidationError as exc:
                return render(
                    request, "categories.html", status_code=400,
                    categories=store.list_categories(c), new=new, errors=exc.errors,
                )
        finally:
            c.close()
        return redirect("/categories", f"Added {new['name'].strip()}.")

    @app.get("/categories/{category_id}/edit", response_class=HTMLResponse)
    def category_edit(request: Request, category_id: int):
        c = conn()
        try:
            cat = store.get_category(c, category_id)
        finally:
            c.close()
        if not cat:
            return redirect("/categories", "That category no longer exists.")
        amount, unit = store.split_minutes(cat["default_reminder_minutes"])
        form = {
            "id": cat["id"], "name": cat["name"], "color": cat["color"],
            "default_reminder": amount, "default_unit": unit,
            "allday_days": "" if cat["default_allday_days"] is None else str(cat["default_allday_days"]),
        }
        return render(request, "category_form.html", category=form, errors={})

    @app.post("/categories/{category_id}/edit", response_class=HTMLResponse)
    async def category_update(request: Request, category_id: int):
        f = await request.form()
        form = dict(_category_form(f), id=category_id)
        c = conn()
        try:
            if not store.get_category(c, category_id):
                return redirect("/categories", "That category no longer exists.")
            try:
                store.save_category(c, form["name"], form["color"], form["default_reminder"], category_id,
                                    default_unit=form["default_unit"], allday_days=form["allday_days"])
            except store.ValidationError as exc:
                return render(request, "category_form.html", status_code=400, category=form, errors=exc.errors)
        finally:
            c.close()
        return redirect("/categories", f"Saved {form['name'].strip()}.")

    @app.post("/categories/{category_id}/delete")
    def category_delete(category_id: int):
        c = conn()
        try:
            cat = store.get_category(c, category_id)
            if not cat:
                return redirect("/categories", "That category no longer exists.")
            try:
                store.delete_category(c, category_id)
            except store.InUseError as exc:
                return redirect(
                    "/categories",
                    f"{cat['name']} is used by {exc.count} event{'s' if exc.count != 1 else ''}, so can't be removed. "
                    "Change or delete those events first.",
                )
        finally:
            c.close()
        return redirect("/categories", f"Removed {cat['name']}.")

    # ------------------------------------------------ Google import (Phase 5a)

    def _fmt_value(key, v, g):
        if key in ("start_local", "end_local") and v:
            return _fmt_when(v) if "T" in v else _date_label(v)
        if key == "all_day":
            return "yes" if v else "no"
        if key == "rrule":
            return recurrence.describe(v, timeutil.parse_local(g["start_local"]).date()) if v else "doesn't repeat"
        if key == "recurrence_end":
            return _date_label(v) if v else "no end"
        if key == "exdates":
            return ", ".join(_date_label(d) for d in v) or "none"
        if key == "overrides":
            return ", ".join(_date_label(d) for d in v) or "none"
        return v or "(blank)"

    def _rows(here: dict, google: dict) -> list[dict]:
        a, b = importer._norm(here), importer._norm(google)
        return [{"name": importer.FIELD_NAMES[k], "here": _fmt_value(k, a[k], a), "google": _fmt_value(k, b[k], b)}
                for k in a if a[k] != b[k]]

    def _local_when(ev: dict) -> str:
        text = f"{_event_date(ev)} · {_event_when(ev)}"
        if ev["rrule"]:
            text += " · " + recurrence.describe(ev["rrule"], _event_start_date(ev), ev["recurrence_end"])
        return text

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        c = conn()
        try:
            sources = importer.list_sources(c)
            people = {p["id"]: p["name"] for p in store.list_people(c)}
            cats = {k["id"]: k["name"] for k in store.list_categories(c)}
            for s_ in sources:
                s_["people_names"] = [people[i] for i in s_["people_ids"] if i in people]
                s_["category_name"] = cats.get(s_["default_category_id"])
            skipped = len(importer.list_skips(c))
        finally:
            c.close()
        return render(request, "import.html", sources=sources, skipped_count=skipped)

    @app.post("/import/{slot}/fetch")
    def import_fetch(slot: int):
        if slot not in importer.configured_slots():
            return redirect("/import", "That source isn't set up in Portainer.")
        c = conn()
        try:
            try:
                counts = importer.fetch_source(c, slot)
            except ical_import.FetchError as exc:
                return redirect("/import", f"Couldn't read the Google calendar: {exc}")
        finally:
            c.close()
        pending = sum(counts[k] for k in importer.KINDS)
        if not pending:
            return redirect("/import", f"Fetched {counts['events']} events. Nothing new to review.")
        return redirect("/import/review", f"Fetched {counts['events']} events. {pending} to review.")

    @app.get("/import/review", response_class=HTMLResponse)
    def import_review(request: Request):
        c = conn()
        try:
            today = timeutil.now_local().date()
            groups = {k: [] for k in importer.KINDS}
            for it in importer.list_items(c):
                g = it["g"]
                if g:
                    it["when"] = importer.when_label(g, today)
                ev = store.get_event(c, it["event_id"]) if it["event_id"] else None
                if ev:
                    it["local_title"] = ev["title"]
                    it["local_when"] = _local_when(ev)
                if it["kind"] == "new":
                    it["overlaps"] = importer.overlaps(c, g, today)
                if it["kind"] in ("changed", "conflict") and ev:
                    it["rows"] = _rows(importer.local_version(c, ev), g)
                if it["kind"] in ("duplicate", "changed", "conflict", "removed", "same_uid") and not ev:
                    continue   # its event has gone since the fetch; the next fetch tidies up
                groups[it["kind"]].append(it)
            groups["total"] = sum(len(v) for v in groups.values())
            names = []
            for s_ in importer.list_sources(c):
                pn = [p["name"] for p in store.list_people(c) if p["id"] in s_["people_ids"]]
                cat = store.get_category(c, s_["default_category_id"]) if s_["default_category_id"] else None
                names.append(f"{s_['name']}: " + (", ".join(pn) or "no people set") + " · "
                             + (cat["name"] if cat else "no category set"))
        finally:
            c.close()
        return render(request, "import_review.html", groups=groups, defaults_text="; ".join(names))

    @app.post("/import/review/add-ticked")
    async def import_add_ticked(request: Request):
        f = await request.form()
        ids = [int(x) for x in f.getlist("item") if str(x).isdigit()]
        if not ids:
            return redirect("/import/review", "Nothing was ticked.")
        c = conn()
        try:
            try:
                n = importer.add_with_defaults(c, ids)
            except importer.ImportError_ as exc:
                return redirect("/import/review", str(exc))
        finally:
            c.close()
        return redirect("/import/review", f"Added {n} event{'s' if n != 1 else ''}.")

    @app.post("/import/review/apply-all")
    def import_apply_all():
        c = conn()
        try:
            n = importer.apply_all(c)
        finally:
            c.close()
        return redirect("/import/review", f"Applied {n} change{'s' if n != 1 else ''} from Google.")

    @app.get("/import/items/{item_id}/add", response_class=HTMLResponse)
    def import_item_form(request: Request, item_id: int):
        c = conn()
        try:
            it = importer.get_item(c, item_id)
            if not it or it["kind"] != "new":
                return redirect("/import/review", "That item has already been dealt with.")
            return render(request, "event_form.html", form=importer.form_for_item(c, it), event_id=None, errors={},
                          people=store.list_people(c), categories=store.list_categories(c), exceptions=[],
                          future=None, future_label="", dropped=[], shelf_added=False, import_item=it,
                          cancel_href="/import/review")
        finally:
            c.close()

    @app.post("/import/items/{item_id}/add", response_class=HTMLResponse)
    async def import_item_save(request: Request, item_id: int):
        form = _form_to_input(await request.form())
        c = conn()
        try:
            it = importer.get_item(c, item_id)
            if not it or it["kind"] != "new":
                return redirect("/import/review", "That item has already been dealt with.")
            try:
                importer.add_reviewed(c, item_id, form)
            except store.ValidationError as exc:
                return render(request, "event_form.html", status_code=400, form=form, event_id=None,
                              errors=exc.errors, people=store.list_people(c), categories=store.list_categories(c),
                              exceptions=[], future=None, future_label="", dropped=[], shelf_added=False,
                              import_item=it, cancel_href="/import/review")
        finally:
            c.close()
        return redirect("/import/review", f"Added “{form.title.strip()}”.")

    _ITEM_ACTIONS = {
        "skip": (importer.skip, "Skipped. It won't be offered again."),
        "link": (importer.link, "Linked. Your version is kept."),
        "keep-both": (importer.keep_both, "Moved to New."),
        "apply": (importer.apply_change, "Google's version applied."),
        "keep-mine": (importer.keep_mine, "Kept your version."),
        "delete": (importer.removed_delete, "Deleted here. It can be restored from Recently deleted for 3 days."),
        "keep": (importer.removed_keep, "Kept as an ordinary event."),
        "use-this": (importer.use_this_copy, "Done. It follows this calendar from now on."),
    }

    @app.post("/import/items/{item_id}/{action}")
    def import_item_action(item_id: int, action: str):
        if action not in _ITEM_ACTIONS:
            return redirect("/import/review", "Unknown action.")
        fn, done = _ITEM_ACTIONS[action]
        c = conn()
        try:
            try:
                fn(c, item_id)
            except importer.ImportError_ as exc:
                return redirect("/import/review", str(exc))
        finally:
            c.close()
        return redirect("/import/review", done)

    @app.get("/import/sources/{slot}", response_class=HTMLResponse)
    def import_source_page(request: Request, slot: int):
        c = conn()
        try:
            src = importer.get_source(c, slot)
            if not src:
                return redirect("/import", "That source isn't set up.")
            form = {"name": src["name"], "people_ids": src["people_ids"],
                    "category_id": src["default_category_id"] or ""}
            return render(request, "import_source.html", slot=slot, form=form, errors={},
                          people=store.list_people(c), categories=store.list_categories(c))
        finally:
            c.close()

    @app.post("/import/sources/{slot}", response_class=HTMLResponse)
    async def import_source_save(request: Request, slot: int):
        f = await request.form()
        c = conn()
        try:
            if not importer.get_source(c, slot):
                return redirect("/import", "That source isn't set up.")
            try:
                importer.save_source(c, slot, f.get("name", ""), f.getlist("people"), f.get("category_id", ""))
            except store.ValidationError as exc:
                form = {"name": f.get("name", ""), "people_ids": [int(x) for x in f.getlist("people") if x.isdigit()],
                        "category_id": f.get("category_id", "")}
                return render(request, "import_source.html", status_code=400, slot=slot, form=form,
                              errors=exc.errors, people=store.list_people(c), categories=store.list_categories(c))
        finally:
            c.close()
        return redirect("/import", "Saved.")

    @app.get("/import/skipped", response_class=HTMLResponse)
    def import_skipped(request: Request):
        c = conn()
        try:
            skips = importer.list_skips(c)
        finally:
            c.close()
        return render(request, "import_skipped.html", skips=skips)

    @app.post("/import/skipped/unskip")
    async def import_unskip(request: Request):
        f = await request.form()
        c = conn()
        try:
            try:
                importer.unskip(c, int(f.get("slot", "0")), f.get("uid", ""))
            except ValueError:
                pass
        finally:
            c.close()
        return redirect("/import/skipped", "It will be offered again at the next fetch.")

    # ------------------------------------------------- Quick titles (D49)

    def _quick_titles_page(request, c, new=None, errors=None, status=200, show_suggestions=False):
        return render(
            request, "quick_titles.html", status_code=status,
            titles=store.list_quick_titles(c), categories=store.list_categories(c),
            new=new or {"title": "", "category_id": ""}, errors=errors or {},
            suggestions=store.suggest_quick_titles(c) if show_suggestions else None,
            shown_max=store.MAX_QUICK_TITLES_SHOWN,
        )

    @app.get("/quick-titles", response_class=HTMLResponse)
    def quick_titles_page(request: Request, suggest: int = 0):
        c = conn()
        try:
            return _quick_titles_page(request, c, show_suggestions=bool(suggest))
        finally:
            c.close()

    @app.post("/quick-titles", response_class=HTMLResponse)
    async def quick_title_create(request: Request):
        f = await request.form()
        new = {"title": f.get("title", ""), "category_id": f.get("category_id", "")}
        c = conn()
        try:
            try:
                store.save_quick_title(c, new["title"], new["category_id"])
            except store.ValidationError as exc:
                return _quick_titles_page(request, c, new, exc.errors, status=400)
        finally:
            c.close()
        return redirect("/quick-titles", f"Added “{new['title'].strip()}”.")

    @app.post("/quick-titles/add-suggested")
    async def quick_titles_add_suggested(request: Request):
        f = await request.form()
        picked = f.getlist("pick")
        added = 0
        c = conn()
        try:
            for raw in picked:
                title, _, cat = raw.partition("|")
                try:
                    store.save_quick_title(c, title, cat or None)
                    added += 1
                except store.ValidationError:
                    pass
        finally:
            c.close()
        return redirect("/quick-titles", f"Added {added} title{'s' if added != 1 else ''}." if added
                        else "Nothing was ticked.")

    @app.get("/quick-titles/{qt_id}/edit", response_class=HTMLResponse)
    def quick_title_edit(request: Request, qt_id: int):
        c = conn()
        try:
            qt = store.get_quick_title(c, qt_id)
            if not qt:
                return redirect("/quick-titles", "That title is no longer on the list.")
            form = {"id": qt_id, "title": qt["title"], "category_id": str(qt["category_id"] or "")}
            return render(request, "quick_title_form.html", form=form, errors={},
                          categories=store.list_categories(c))
        finally:
            c.close()

    @app.post("/quick-titles/{qt_id}/edit", response_class=HTMLResponse)
    async def quick_title_update(request: Request, qt_id: int):
        f = await request.form()
        form = {"id": qt_id, "title": f.get("title", ""), "category_id": f.get("category_id", "")}
        c = conn()
        try:
            if not store.get_quick_title(c, qt_id):
                return redirect("/quick-titles", "That title is no longer on the list.")
            try:
                store.save_quick_title(c, form["title"], form["category_id"], qt_id)
            except store.ValidationError as exc:
                return render(request, "quick_title_form.html", status_code=400, form=form, errors=exc.errors,
                              categories=store.list_categories(c))
        finally:
            c.close()
        return redirect("/quick-titles", f"Saved “{form['title'].strip()}”.")

    @app.post("/quick-titles/{qt_id}/delete")
    def quick_title_delete(qt_id: int):
        c = conn()
        try:
            qt = store.get_quick_title(c, qt_id)
            if qt:
                store.delete_quick_title(c, qt_id)
        finally:
            c.close()
        return redirect("/quick-titles", f"Removed “{qt['title']}”." if qt else "That title is no longer on the list.")

    # ------------------------------------------------ Recently deleted (D51)

    @app.get("/recently-deleted", response_class=HTMLResponse)
    def recently_deleted_page(request: Request):
        c = conn()
        try:
            items = store.list_recently_deleted(c)
        finally:
            c.close()
        return render(request, "recently_deleted.html", items=items, keep_days=store.DELETED_KEEP_DAYS,
                      keep_max=store.DELETED_KEEP_MAX)

    @app.post("/recently-deleted/{entry_id}/restore")
    def recently_deleted_restore(entry_id: int):
        c = conn()
        try:
            try:
                message = store.restore_deleted(c, entry_id)
            except store.NotFoundError as exc:
                message = str(exc)
        finally:
            c.close()
        return redirect("/recently-deleted", message)

    # ------------------------------------------------------------ settings

    def _settings_page(request, cfg, errors=None, status=200):
        try:
            start = timeutil.parse_time(cfg["dark_start"])
            end = timeutil.parse_time(cfg["dark_end"])
            crosses = start > end
            off = start == end
        except ValueError:
            crosses = off = False
        return render(
            request, "settings.html", status_code=status,
            cfg=cfg, errors=errors or {}, crosses_midnight=crosses, dark_off=off,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        c = conn()
        try:
            cfg = store.get_config(c)
        finally:
            c.close()
        return _settings_page(request, cfg)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        f = await request.form()
        form = {k: f.get(k, "") for k in store.DEFAULT_CONFIG}
        c = conn()
        try:
            try:
                store.save_config(c, form)
            except store.ValidationError as exc:
                return _settings_page(request, form, exc.errors, status=400)
        finally:
            c.close()
        return redirect("/settings", "Settings saved.")

    return app


app = create_app() if os.environ.get("CAL_NO_AUTOAPP") != "1" else None
