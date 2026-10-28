"""N4MI Calendar Display — server (Phase 1: core and editor).

A FastAPI app with a server-rendered editor for events, people, categories and
settings. LAN only, no login (decision D10). The display page and its JSON API
arrive in Phase 2.

Run locally:  uvicorn app.main:app --port 8087
Environment:  CAL_DB_PATH (default /data/calendar.db), TZ (default America/New_York)
"""

from __future__ import annotations

import os
import sqlite3
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, store, timeutil

APP_VERSION = "0.1.0"
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
        event_when=_event_when,
        event_date=_event_date,
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

    def _event_form_page(request, c, form: store.EventInput, event_id=None, errors=None, status=200):
        return render(
            request,
            "event_form.html",
            status_code=status,
            form=form,
            event_id=event_id,
            errors=errors or {},
            people=store.list_people(c),
            categories=store.list_categories(c),
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
            return _event_form_page(request, c, store.event_to_input(event), event_id=event_id)
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

    @app.get("/categories", response_class=HTMLResponse)
    def categories_page(request: Request):
        c = conn()
        try:
            return render(
                request, "categories.html",
                categories=store.list_categories(c),
                new={"name": "", "color": "#8B8D98", "default_reminder": "60"},
                errors={},
            )
        finally:
            c.close()

    @app.post("/categories", response_class=HTMLResponse)
    async def category_create(request: Request):
        f = await request.form()
        new = {"name": f.get("name", ""), "color": f.get("color", ""), "default_reminder": f.get("default_reminder", "")}
        c = conn()
        try:
            try:
                store.save_category(c, new["name"], new["color"], new["default_reminder"])
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
        form = {
            "id": cat["id"], "name": cat["name"], "color": cat["color"],
            "default_reminder": "" if cat["default_reminder_minutes"] is None else str(cat["default_reminder_minutes"]),
        }
        return render(request, "category_form.html", category=form, errors={})

    @app.post("/categories/{category_id}/edit", response_class=HTMLResponse)
    async def category_update(request: Request, category_id: int):
        f = await request.form()
        form = {
            "id": category_id, "name": f.get("name", ""), "color": f.get("color", ""),
            "default_reminder": f.get("default_reminder", ""),
        }
        c = conn()
        try:
            if not store.get_category(c, category_id):
                return redirect("/categories", "That category no longer exists.")
            try:
                store.save_category(c, form["name"], form["color"], form["default_reminder"], category_id)
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
