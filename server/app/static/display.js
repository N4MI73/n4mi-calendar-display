/* N4MI Calendar Display — the shelf screen (Phases 3 and 4).
 *
 * Reads /api/v1/display every minute and keeps the last good copy in the
 * browser, so the screen keeps working from it when the server can't be
 * reached, with a clear "Not updated since" bar (decision D12). It never shows
 * "nothing scheduled" unless the schedule is current (D45).
 *
 * The server has already expanded repeating events and worked out when each
 * banner starts (`remind_at`); this page only picks what to show now.
 *
 * Phase 4 (D48): tap an entry for its details, then Move or Delete it; "+ Add"
 * opens a two-step form with quick titles and the page's own keyboard; Coming
 * up scrolls and returns to the top by itself. Changes need the server: when
 * the schedule isn't current they're refused plainly, never queued.
 *
 * Test clock: /display?now=2026-11-02T08:05 runs the page as if it were that
 * time (it keeps ticking from there). /display?kiosk=1 hides the mouse pointer.
 * Plain JavaScript, no build step, no internet.
 */
(function () {
  "use strict";

  var API = "/api/v1/display";
  var REFRESH_MS = 60 * 1000;      // fetch from the server
  var RENDER_MS = 10 * 1000;       // redraw (clock, banners, "earlier today")
  var ROTATE_MS = 10 * 1000;       // calm banners take turns when several apply
  var FETCH_TIMEOUT_MS = 10 * 1000;
  var UNDO_SECONDS = 5;            // "I did it" gives a few seconds to undo (D37)
  var SOON_CAP_MIN = 120;          // on the day, the bright banner starts at most 2 hours before
  var DEFAULTS = { upcoming_days: 14, stale_minutes: 30, dark_start: "23:00", dark_end: "06:00", wake_seconds: 120 };
  var SHEET_IDLE_MS = 60 * 1000;   // a panel left untouched closes by itself (D48)
  var TOP_RETURN_MS = 30 * 1000;   // Coming up goes back to the top after this long untouched
  var TOAST_MS = 4000;

  var params = new URLSearchParams(location.search);
  var testNow = params.get("now");
  var offsetMs = 0;
  if (testNow) {
    var t0 = parseLocal(testNow);
    if (t0) offsetMs = t0.getTime() - Date.now();
  }
  if (params.get("kiosk") === "1") document.documentElement.classList.add("kiosk");
  var CACHE_KEY = "n4mi-calendar-display-v1" + (testNow ? "-test" : "");
  var pageVersion = (document.querySelector('meta[name="app-version"]') || {}).content || "";

  var data = null;        // last good payload
  var lastOk = null;      // when it arrived (ms, page clock)
  var everLoaded = false; // false until the first fetch attempt finishes
  var wakeUntil = 0;      // dark hours: awake until (ms, page clock)
  var local = {};         // reminder id -> {state: "done", until: ms} | {state: "problem", until: ms}
  var ui = null;          // the open panel (details, delete, move, add), or null
  var lastTouch = 0;      // last touch anywhere (ms)
  var comingTouched = 0;  // last scroll of Coming up (ms)
  var comingSig = "";     // what Coming up last showed, so it isn't rebuilt (and its scroll lost) needlessly
  var toastTimer = null;

  // ----------------------------------------------------------------- time

  function now() { return new Date(Date.now() + offsetMs); }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function parseLocal(text) {
    // "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" as local wall-clock time.
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(text || "");
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], m[4] ? +m[4] : 0, m[5] ? +m[5] : 0, 0, 0);
  }

  function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function stamp(d) { return ymd(d) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()); }

  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"];

  function clock(d) {
    var h = d.getHours() % 12 || 12;
    return h + ":" + pad(d.getMinutes()) + " " + (d.getHours() < 12 ? "AM" : "PM");
  }

  function daysBetween(a, b) {
    // Whole calendar days from date a to date b (both Date, times ignored).
    var x = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate());
    var y = Date.UTC(b.getFullYear(), b.getMonth(), b.getDate());
    return Math.round((y - x) / 86400000);
  }

  function minutesOf(hhmm) {
    var m = /^(\d{2}):(\d{2})$/.exec(hhmm || "");
    return m ? +m[1] * 60 + +m[2] : null;
  }

  function inDarkHours(d, cfg) {
    var s = minutesOf(cfg.dark_start), e = minutesOf(cfg.dark_end);
    if (s === null || e === null || s === e) return false;
    var t = d.getHours() * 60 + d.getMinutes();
    return s < e ? (t >= s && t < e) : (t >= s || t < e);
  }

  function dayWord(target, today) {
    // "today", "tomorrow", "Thursday" (within a week) or "Nov 12".
    var n = daysBetween(today, target);
    if (n === 0) return "today";
    if (n === 1) return "tomorrow";
    if (n > 1 && n < 7) return DAYS[target.getDay()];
    return "on " + MONTHS[target.getMonth()].slice(0, 3) + " " + target.getDate();
  }

  function inWords(mins) {
    if (mins <= 0) return "starting now";
    if (mins < 60) return "in " + mins + " minute" + (mins === 1 ? "" : "s");
    var h = Math.floor(mins / 60), m = mins % 60;
    var text = "in " + h + " hour" + (h === 1 ? "" : "s");
    return m ? text + " " + m + " minute" + (m === 1 ? "" : "s") : text;
  }

  function shortIn(mins) {
    // "in 45 min", "in 1 h 20 min" (beside the time in the Today list).
    if (mins <= 0) return "now";
    if (mins < 60) return "in " + mins + " min";
    var h = Math.floor(mins / 60), m = mins % 60;
    return "in " + h + " h" + (m ? " " + m + " min" : "");
  }

  // ------------------------------------------------------------------ DOM

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function textOn(hex) {
    // Dark or light text, whichever reads better on this color.
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return "#0E1116";
    var n = parseInt(m[1], 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    var lum = function (c) { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    var L = 0.2126 * lum(r) + 0.7152 * lum(g) + 0.0722 * lum(b);
    return L > 0.25 ? "#0E1116" : "#FFFFFF";
  }

  function personPill(p) {
    var s = el("span", "pill person", p.name);
    s.style.background = p.color;
    s.style.color = textOn(p.color);
    return s;
  }

  function categoryPill(c) {
    var s = el("span", "pill category");
    var sq = el("span", "sq");
    sq.style.background = c.color;
    sq.setAttribute("aria-hidden", "true");
    s.appendChild(sq);
    s.appendChild(document.createTextNode(c.name));
    return s;
  }

  function notesPill() {
    var s = el("span", "pill notes");
    s.appendChild(icon("lines"));
    s.appendChild(document.createTextNode("Notes"));
    return s;
  }

  function tags(ev, withWhere) {
    var box = el("div", "tags");
    if (!ev.info) ev.people.forEach(function (p) { box.appendChild(personPill(p)); });
    box.appendChild(categoryPill(ev.category));
    if (withWhere && ev.location) box.appendChild(el("span", "where", ev.location));
    if (ev.notes) box.appendChild(notesPill());
    return box;
  }

  // Inline icons (stroke paths, 24x24).
  var ICONS = {
    lines: ["M5 7h14", "M5 12h14", "M5 17h9"],
    move: ["M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M3 10h18", "M8 3v4", "M16 3v4",
           "M10 15h5", "M13 13l2 2-2 2"],
    trash: ["M4 7h16", "M9 7V4h6v3", "M6 7l1 13h10l1-13"],
    pin: ["M12 21s7-6.2 7-11a7 7 0 0 0-14 0c0 4.8 7 11 7 11z", "M14.5 10a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0"],
    warn: ["M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
    back: ["M21 5H9l-6 7 6 7h12z", "M17 9l-5 6", "M12 9l5 6"],
    tick: ["M5 12.5l4.5 4.5L19 7.5"],
    left: ["M15 5l-7 7 7 7"],
    right: ["M9 5l7 7-7 7"],
    // Phase 6a: health levels, Restart, Shut down.
    ok: ["M5 12.5l4.5 4.5L19 7.5"],
    check: ["M12 4l9 16H3z", "M12 10v4", "M12 17.5v.5"],
    alert: ["M8 3h8l5 5v8l-5 5H8l-5-5V8z", "M9 9l6 6", "M15 9l-6 6"],
    none: ["M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18", "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.4", "M12 17.5v.5"],
    restart: ["M20 11a8 8 0 1 0-2.3 5.7", "M20 4v7h-7"],
    power: ["M12 3v9", "M6.3 7.5a8 8 0 1 0 11.4 0"]
  };

  function icon(name) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    ICONS[name].forEach(function (d) {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }

  function tappable(node, ev) {
    node.classList.add("tap");
    node.addEventListener("click", function () { openDetails(ev); });
    return node;
  }

  // -------------------------------------------------------------- choosing

  function cfg() { return (data && data.config) || DEFAULTS; }

  function dayMap() {
    var m = {};
    if (data) data.days.forEach(function (d) { m[d.date] = d; });
    return m;
  }

  function isCurrent(n) {
    return lastOk !== null && (n.getTime() - lastOk) <= cfg().stale_minutes * 60000;
  }

  function soonWindow(ev) {
    // Minutes before the start when the bright "in N minutes" banner begins.
    var st = parseLocal(ev.start), ra = parseLocal(ev.remind_at);
    var lead = Math.round((st - ra) / 60000);
    return Math.min(Math.max(lead, 0), SOON_CAP_MIN);
  }

  function pickBanner(n, days) {
    // Returns {soon: bool, text, sub, ev} or null.
    var today = parseLocal(ymd(n));
    var seen = {}, soon = [], laterToday = [], alldayToday = [], ahead = [];
    days.forEach(function (day) {
      day.events.forEach(function (ev) {
        if (!ev.remind_at) return;
        var key = ev.id + "|" + ev.occ + "|" + ev.start;
        if (seen[key]) return;
        seen[key] = true;
        var st = parseLocal(ev.start), en = parseLocal(ev.end), ra = parseLocal(ev.remind_at);
        var startDay = parseLocal(ev.start.slice(0, 10));
        var dd = daysBetween(today, startDay);
        if (ev.all_day) {
          if (daysBetween(today, parseLocal(ev.end.slice(0, 10))) < 0) return;  // over
          if (n < ra) return;
          (dd <= 0 ? alldayToday : ahead).push(ev);
          return;
        }
        if (n >= st) return;                            // started: no banner
        if (dd === 0) {
          var left = Math.ceil((st - n) / 60000);
          if (left <= soonWindow(ev) && n >= ra) soon.push(ev); else laterToday.push(ev);
        } else if (n >= ra) {
          ahead.push(ev);
        }
      });
    });
    var byStart = function (a, b) { return a.start < b.start ? -1 : a.start > b.start ? 1 : 0; };
    // An appointment due soon always wins, and doesn't take turns with anything.
    if (soon.length) {
      var ev = soon.sort(byStart)[0];
      var mins = Math.ceil((parseLocal(ev.start) - n) / 60000);
      return { soon: true, text: ev.title + " " + inWords(mins), sub: ev.location, ev: ev, mins: mins };
    }
    // Otherwise the calm banners take turns: later today, all-day today, then days ahead.
    var calm = [];
    laterToday.sort(byStart).forEach(function (e) {
      calm.push({ soon: false, text: e.title + " at " + e.time + " today", ev: e });
    });
    alldayToday.sort(byStart).forEach(function (e) {
      calm.push({ soon: false, text: e.title + " today", ev: e });
    });
    ahead.sort(byStart).forEach(function (e) {
      var w = dayWord(parseLocal(e.start.slice(0, 10)), today);
      calm.push({ soon: false, text: e.all_day ? e.title + " is " + w : e.title + " " + w + " at " + e.time, ev: e });
    });
    if (!calm.length) return null;
    return calm[Math.floor(Date.now() / ROTATE_MS) % calm.length];
  }

  // -------------------------------------------------------------- drawing

  function showMessage(html) {
    $("app").hidden = true;
    $("message").hidden = false;
    $("message-text").innerHTML = html;   // fixed text only, never calendar data
  }

  function render() {
    var n = now();
    var c = cfg();

    // Night: fully black during dark hours unless woken by a tap (D13).
    $("night").hidden = !(inDarkHours(n, c) && Date.now() > wakeUntil);
    if (!$("night").hidden && ui) closeSheet();

    var days = dayMap();
    var todayKey = ymd(n);
    var today = days[todayKey];
    if (!data || !today) {
      if (!everLoaded) return showMessage("Loading the calendar…");
      return showMessage("Can't reach the calendar right now.<small>It will appear as soon as the connection is back.</small>");
    }
    $("message").hidden = true;
    $("app").hidden = false;

    var current = isCurrent(n);
    var list = data.days;
    $("add-btn").classList.toggle("dim", !current);

    // Header: date, title (D65), clock and the health button (D61).
    $("date").textContent = DAYS[n.getDay()] + ", " + MONTHS[n.getMonth()] + " " + n.getDate();
    $("title").textContent = data.title || "";
    $("clock").textContent = clock(n);
    drawHealthButton(healthView(n));
    if (ui && ui.mode === "health" && !ui.busy) drawSheet();

    // Banner.
    var b = pickBanner(n, list);
    var banner = $("banner");
    if (b) {
      banner.hidden = false;
      banner.classList.toggle("soon", b.soon);
      $("banner-text").textContent = b.text;
      $("banner-sub").textContent = b.sub || "";
      var pills = $("banner-pills");
      pills.textContent = "";
      if (!b.ev.info) b.ev.people.forEach(function (p) { pills.appendChild(personPill(p)); });
    } else {
      banner.hidden = true;
    }

    drawReminders(n);
    drawToday(n, today, list, current, b);
    drawComing(n, list, c);

    // Stale bar.
    var stale = $("stale");
    if (current) {
      stale.hidden = true;
    } else {
      var at = new Date(lastOk);
      var when = daysBetween(at, n) === 0 ? clock(at) : DAYS[at.getDay()].slice(0, 3) + " " + clock(at);
      $("stale-when").textContent = "Not updated since " + when + ".";
      stale.hidden = false;
    }
  }

  function drawReminders(n) {
    var strip = $("strip");
    strip.textContent = "";
    var todayKey = ymd(n);
    var shown = 0;
    (data.reminders || []).forEach(function (r) {
      // Offline, a non-carry-over reminder from an earlier day has expired.
      if (!r.carry_over && r.due && r.due < todayKey && !local[r.id]) return;
      strip.appendChild(reminderCard(r, n));
      shown++;
    });
    strip.hidden = shown === 0;
  }

  function reminderCard(r, n) {
    var st = local[r.id];
    var card = el("div", "rem");
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "ring");
    svg.setAttribute("aria-hidden", "true");
    var circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", "12"); circle.setAttribute("cy", "12"); circle.setAttribute("r", "9");
    svg.appendChild(circle);
    card.appendChild(svg);
    var words = el("div", "rem-words");
    card.appendChild(words);

    if (st && st.state === "done") {
      card.classList.add("done");
      var tick = document.createElementNS(ns, "path");
      tick.setAttribute("d", "M8 12.5l2.8 2.8L16 10");
      svg.appendChild(tick);
      var line = el("div", "rem-text");
      line.appendChild(el("strong", null, "Done: "));
      line.appendChild(document.createTextNode(r.text));
      words.appendChild(line);
      var left = Math.max(1, Math.ceil((st.until - Date.now()) / 1000));
      var undo = el("button", null, "Undo ");
      undo.type = "button";
      undo.appendChild(el("span", "count", "· " + left));
      undo.addEventListener("click", function () { act(r.id, "undo"); });
      card.appendChild(undo);
      return card;
    }

    words.appendChild(el("div", "rem-text", r.text));
    if (st && st.state === "problem") {
      words.appendChild(el("div", "rem-sub problem", "Couldn't reach the calendar. Try again."));
    } else if (r.due && r.due < ymd(n)) {
      var d = parseLocal(r.due), gap = daysBetween(d, n);
      words.appendChild(el("div", "rem-sub", "Waiting since " + (gap < 7 ? DAYS[d.getDay()] : MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate())));
    }
    var btn = el("button", null, "I did it");
    btn.type = "button";
    btn.addEventListener("click", function () { act(r.id, "done"); });
    card.appendChild(btn);
    return card;
  }

  function todayRow(ev, n, highlight) {
    var row = el("div", "row" + (ev.info ? " info" : "") + (highlight ? " soon" : ""));
    var time = el("div", "time", ev.time);
    if (highlight) time.appendChild(el("span", "left", shortIn(highlight)));
    row.appendChild(time);
    var what = el("div", "what");
    what.appendChild(el("div", "title", ev.title));
    what.appendChild(tags(ev, true));
    row.appendChild(what);
    return tappable(row, ev);
  }

  function alldayRow(ev) {
    var row = el("div", "row allday" + (ev.info ? " info" : ""));
    row.appendChild(el("div", "time", "All day"));
    var what = el("div", "what");
    var title = el("div", "title", ev.title + " ");
    if (ev.day_count > 1) title.appendChild(el("span", "part", "day " + ev.day_index + " of " + ev.day_count));
    what.appendChild(title);
    var t = tags(ev, false);
    while (t.firstChild) what.appendChild(t.firstChild);
    row.appendChild(what);
    return tappable(row, ev);
  }

  // Holidays (Phase 5b, D59): quiet reference lines, like Info events, with nothing to tap.
  function holidayRow(name) {
    var row = el("div", "row allday info holiday");
    row.appendChild(el("div", "time", "Holiday"));
    var what = el("div", "what");
    what.appendChild(el("div", "title", name));
    row.appendChild(what);
    return row;
  }

  function holidayItem(name) {
    var item = el("div", "item info holiday");
    var line = el("div", "line");
    line.appendChild(el("span", "t", "Holiday"));
    line.appendChild(document.createTextNode(name));
    item.appendChild(line);
    return item;
  }

  function nextEventAfterToday(n, list) {
    var todayKey = ymd(n);
    for (var i = 0; i < list.length; i++) {
      if (list[i].date <= todayKey) continue;
      if (list[i].events.length) return { day: list[i], ev: list[i].events[0] };
    }
    return null;
  }

  function drawToday(n, today, list, current, banner) {
    var box = $("today");
    box.textContent = "";
    var events = today.events;
    var hols = today.holidays || [];
    hols.forEach(function (h) { box.appendChild(holidayRow(h)); });

    if (!events.length) {
      var q = el("div", "quiet");
      q.appendChild(el("div", "big", current ? "Nothing scheduled today" : "Nothing scheduled today in the last schedule received"));
      var nx = nextEventAfterToday(n, list);
      if (nx) {
        var d = parseLocal(nx.day.date);
        var when = dayWord(d, parseLocal(ymd(n)));
        when = when === "tomorrow" ? "Tomorrow" : when.charAt(0).toUpperCase() + when.slice(1);
        q.appendChild(el("div", "next", "Next: " + nx.ev.title + ", " + when + (nx.ev.time ? " " + nx.ev.time : "")));
      }
      box.appendChild(q);
      return;
    }

    var soonKey = banner && banner.soon ? banner.ev.id + "|" + banner.ev.occ + "|" + banner.ev.start : null;
    var allday = [], earlier = [], later = [];
    events.forEach(function (ev) {
      if (ev.all_day) allday.push(ev);
      else if (parseLocal(ev.end) <= n) earlier.push(ev);
      else later.push(ev);
    });

    allday.forEach(function (ev) { box.appendChild(alldayRow(ev)); });
    if (earlier.length) {
      box.appendChild(el("div", "earlier", "Earlier today: " + earlier.map(function (ev) {
        return ev.title + " (" + ev.time + ")";
      }).join(", ")));
    }
    var rows = later.map(function (ev) {
      var key = ev.id + "|" + ev.occ + "|" + ev.start;
      var hl = key === soonKey ? banner.mins : 0;
      var r = todayRow(ev, n, hl);
      box.appendChild(r);
      return { row: r, ev: ev };
    });
    if (!later.length && !allday.length) {
      box.appendChild(el("div", "later", "Nothing more today."));
    }

    // If it doesn't all fit, drop the latest events and say how many more there are.
    var hidden = [];
    while (box.scrollHeight > box.clientHeight + 1 && rows.length > 1) {
      var last = rows.pop();
      box.removeChild(last.row);
      hidden.unshift(last.ev);
    }
    if (hidden.length) {
      var more = el("div", "later", hidden.length + " more later today: " + hidden.map(function (ev) {
        return ev.title + " (" + ev.time + ")";
      }).join(", "));
      box.appendChild(more);
      while (box.scrollHeight > box.clientHeight + 1 && rows.length > 1) {
        var l2 = rows.pop();
        box.removeChild(l2.row);
        hidden.unshift(l2.ev);
        more.textContent = hidden.length + " more later today: " + hidden.map(function (ev) {
          return ev.title + " (" + ev.time + ")";
        }).join(", ");
      }
    }
  }

  function comingItem(ev) {
    var item = el("div", "item" + (ev.info ? " info" : ""));
    var line = el("div", "line");
    var lead = ev.all_day ? "All day" : ev.time;
    line.appendChild(el("span", "t", lead));
    var title = ev.title;
    if (ev.all_day && ev.day_count > 1) title += ", until " + ev.until;
    line.appendChild(document.createTextNode(title));
    item.appendChild(line);
    item.appendChild(tags(ev, false));
    return tappable(item, ev);
  }

  function drawComing(n, list, c) {
    var box = $("coming");
    var more = $("coming-more");
    var today = parseLocal(ymd(n));
    var range = c.upcoming_days;
    var rangeWords = range === 14 ? "the next two weeks" : range === 7 ? "the next week" : "the next " + range + " days";
    var groups = [];
    list.forEach(function (day) {
      var d = parseLocal(day.date);
      var off = daysBetween(today, d);
      if (off < 1 || off > range) return;
      // A multi-day event shows once, on its first day ("until Sat"); one already under way is in Today.
      var evs = day.events.filter(function (ev) { return !(ev.all_day && ev.day_index > 1); });
      var hols = day.holidays || [];
      if (!evs.length && !hols.length) return;
      var label = (off === 1 ? "Tomorrow · " + DAYS[d.getDay()].slice(0, 3) + ", " : DAYS[d.getDay()] + " · ")
        + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate();
      groups.push({ label: label, events: evs, holidays: hols });
    });
    // Rebuild only when something changed, so a scroll in progress isn't lost (D48).
    var sig = JSON.stringify(groups) + rangeWords;
    if (sig === comingSig) { updateComingMore(); return; }
    comingSig = sig;
    var top = box.scrollTop;
    box.textContent = "";
    more.textContent = "";
    if (!groups.length) {
      more.textContent = "Nothing in " + rangeWords;
      $("top-btn").hidden = true;
      return;
    }
    groups.forEach(function (g) {
      var day = el("div", "day");
      day.appendChild(el("div", "day-name", g.label));
      g.holidays.forEach(function (h) { day.appendChild(holidayItem(h)); });
      g.events.forEach(function (ev) { day.appendChild(comingItem(ev)); });
      box.appendChild(day);
    });
    box.scrollTop = top;
    updateComingMore();
  }

  function updateComingMore() {
    // "Scroll for N more": entries wholly below what can be seen now.
    var box = $("coming"), more = $("coming-more");
    if (!box.firstChild) return;
    var bottom = box.getBoundingClientRect().bottom;
    var below = 0;
    Array.prototype.forEach.call(box.querySelectorAll(".item"), function (it) {
      if (it.getBoundingClientRect().bottom > bottom + 2) below++;
    });
    more.textContent = below ? "Scroll for " + below + " more" : "";
    $("top-btn").hidden = box.scrollTop < 4;
  }

  // ------------------------------------------------- panels (Phase 4, D48)
  //
  // One panel at a time over the screen: details, delete, move, add (2 steps).
  // `ui` holds its state; drawSheet() rebuilds it after every change. A panel
  // left untouched for a minute closes by itself, and night closes it.

  function canAct() { return isCurrent(now()); }

  function b(label, cls, onTap, opts) {
    // A button: label text, optional icon, classes, and a handler.
    opts = opts || {};
    var btn = el("button", cls);
    btn.type = "button";
    if (opts.icon) btn.appendChild(icon(opts.icon));
    if (label !== null) btn.appendChild(document.createTextNode(label));
    if (opts.disabled || (ui && ui.busy && !opts.always)) btn.disabled = true;
    if (opts.pressed !== undefined) btn.setAttribute("aria-pressed", opts.pressed ? "true" : "false");
    if (onTap) btn.addEventListener("click", onTap);
    return btn;
  }

  function longDate(d) { return DAYS[d.getDay()] + ", " + MONTHS[d.getMonth()] + " " + d.getDate(); }
  function midDate(d) { return DAYS[d.getDay()] + ", " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate(); }
  function shortDate(d) { return DAYS[d.getDay()].slice(0, 3) + ", " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate(); }

  function timeRange(ev) {
    // "7:00 – 9:00 PM", "11:30 AM – 12:30 PM", "10:00 PM – 1:00 AM (Sat)".
    var a = ev.time, z = ev.end_time;
    var sameHalf = a.slice(-2) === z.slice(-2);
    var text = (sameHalf ? a.slice(0, -3) : a) + " – " + z;
    if (ev.end.slice(0, 10) !== ev.start.slice(0, 10)) text += " (" + DAYS[parseLocal(ev.end).getDay()].slice(0, 3) + ")";
    return text;
  }

  function whenLine(ev) {
    var st = parseLocal(ev.start), en = parseLocal(ev.end);
    if (ev.all_day) {
      if (ev.start.slice(0, 10) === ev.end.slice(0, 10)) return longDate(st) + " · All day";
      return shortDate(st) + " – " + shortDate(en) + " · All day";
    }
    return longDate(st) + " · " + timeRange(ev);
  }

  function openSheet(state) {
    ui = state;
    ui.busy = false;
    ui.error = "";
    lastTouch = Date.now();
    drawSheet();
  }

  function closeSheet() {
    ui = null;
    var sh = $("sheet");
    sh.hidden = true;
    sh.textContent = "";
  }

  function toast(text) {
    var t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, TOAST_MS);
  }

  function offlineWarn(text) {
    var w = el("div", "warn");
    w.appendChild(icon("warn"));
    w.appendChild(document.createTextNode(text));
    return w;
  }

  function drawSheet() {
    var sh = $("sheet");
    sh.textContent = "";
    if (!ui) { sh.hidden = true; return; }
    var card = ({ details: drawDetails, delete: drawDelete, move: drawMove, add1: drawAddWhat, add2: drawAddWhen,
                  health: drawHealth, restart: drawDeviceConfirm, shutdown: drawDeviceConfirm })[ui.mode]();
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    sh.appendChild(card);
    sh.hidden = false;
  }

  // Tapping the dimmed area around a panel closes it (except mid-way through Add).
  function onOverlayTap(e) {
    if (e.target === $("sheet") && ui && ui.mode !== "add1" && ui.mode !== "add2" && !ui.busy) closeSheet();
  }

  // ------------------------------------------------------------ details

  function openDetails(ev) { openSheet({ mode: "details", ev: ev }); }

  function drawDetails() {
    var ev = ui.ev, ok = canAct();
    var card = el("div", "sheet");
    card.appendChild(el("h3", null, ev.title));
    card.appendChild(el("div", "when", whenLine(ev)));
    if (ev.repeat_text) card.appendChild(el("div", "rep", "Repeats: " + ev.repeat_text));
    if (ev.location) {
      var loc = el("div", "loc");
      loc.appendChild(icon("pin"));
      loc.appendChild(document.createTextNode(ev.location));
      card.appendChild(loc);
    }
    var t = el("div", "tags");
    if (!ev.info) ev.people.forEach(function (p) { t.appendChild(personPill(p)); });
    t.appendChild(categoryPill(ev.category));
    card.appendChild(t);
    if (ev.notes) {
      var nb = el("div", "notes");
      nb.appendChild(el("div", "lbl", "Notes"));
      nb.appendChild(el("div", "txt", ev.notes));
      card.appendChild(nb);
    }
    if (!ok) card.appendChild(offlineWarn("Can't reach the calendar right now. Moving and deleting will work again when the connection is back."));
    var bar = el("div", "bar");
    bar.appendChild(b("Move", "b w", function () {
      var st = parseLocal(ev.start);
      openSheet({ mode: "move", ev: ev, day: parseLocal(ev.start.slice(0, 10)), hour: st.getHours(), min: st.getMinutes(),
                  month: null, late: st.getHours() < 7 || st.getHours() > 20 });
    }, { icon: "move", disabled: !ok }));
    bar.appendChild(b("Delete", "b w dangerText", function () { openSheet({ mode: "delete", ev: ev }); },
                      { icon: "trash", disabled: !ok }));
    bar.appendChild(el("div", "aside", "Closes by itself after a minute"));
    bar.appendChild(b("Close", "b w primary", closeSheet, { always: true }));
    card.appendChild(bar);
    return card;
  }

  // ------------------------------------------------------------- delete

  function drawDelete() {
    var ev = ui.ev;
    var st = parseLocal(ev.start);
    var card = el("div", "sheet narrow");
    card.appendChild(el("h3", null, "Delete " + ev.title + " on " + midDate(st) + "?"));
    if (ev.repeats) card.appendChild(el("div", "say", "It repeats. Only " + midDate(st) + " is removed; the other dates stay."));
    else if (ev.all_day && ev.day_count > 1) card.appendChild(el("div", "say", "All " + ev.day_count + " days are removed."));
    card.appendChild(el("div", "say small", "Changed your mind later? It can be brought back from the editor for 3 days."));
    if (ui.error) card.appendChild(el("div", "err", ui.error));
    // "Delete it" sits far left, away from where the first Delete button was.
    var bar = el("div", "bar");
    bar.appendChild(b("Delete it", "b danger", function () {
      send("/api/v1/events/" + ev.id + "/delete", { occ: ev.occ }, "Deleted " + ev.title + " (" + midDate(st) + ")");
    }, { icon: "trash" }));
    bar.appendChild(el("div", "fill"));
    bar.appendChild(b("Keep it", "b w primary", closeSheet, { always: true }));
    card.appendChild(bar);
    return card;
  }

  // --------------------------------------------------------- day / time

  var DAY_HOURS = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];
  var LATE_HOURS = [0, 1, 2, 3, 4, 5, 6, 21, 22, 23];

  function hourLabel(h) { return (h % 12 || 12) + (h < 12 ? " AM" : " PM"); }

  function sameDay(a, b2) { return a && b2 && ymd(a) === ymd(b2); }

  function dayPicker() {
    // Today and the next 13 days, plus "Later…" for a month grid.
    var grp = el("div", "grp");
    if (ui.month) return monthPicker();
    var today = parseLocal(ymd(now()));
    var rows = [el("div", "rowb"), el("div", "rowb")];
    for (var i = 0; i < 14; i++) {
      var d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
      var btn = b(null, "c dayb" + (sameDay(d, ui.day) ? " on" : ""), pickDay(d), { pressed: sameDay(d, ui.day) });
      btn.appendChild(el("small", null, i === 0 ? "Today" : DAYS[d.getDay()].slice(0, 3)));
      btn.appendChild(el("span", null, String(d.getDate())));
      btn.setAttribute("aria-label", longDate(d));
      rows[i < 7 ? 0 : 1].appendChild(btn);
    }
    var far = ui.day && daysBetween(today, ui.day) >= 14;
    rows[1].appendChild(b(far ? MONTHS[ui.day.getMonth()].slice(0, 3) + " " + ui.day.getDate() : "Later…",
                          "c dayb" + (far ? " on" : ""), function () {
      var base = ui.day || today;
      ui.month = new Date(base.getFullYear(), base.getMonth(), 1);
      drawSheet();
    }));
    grp.appendChild(rows[0]);
    grp.appendChild(rows[1]);
    return grp;
  }

  function pickDay(d) { return function () { ui.day = d; ui.error = ""; drawSheet(); }; }

  function monthPicker() {
    var m = ui.month, today = parseLocal(ymd(now()));
    var grp = el("div", "grp");
    var nav = el("div", "rowb");
    var first = new Date(today.getFullYear(), today.getMonth(), 1);
    nav.appendChild(b(null, "c", function () { ui.month = new Date(m.getFullYear(), m.getMonth() - 1, 1); drawSheet(); },
                      { icon: "left", disabled: m <= first }));
    nav.lastChild.setAttribute("aria-label", "Previous month");
    nav.appendChild(el("div", "summary", MONTHS[m.getMonth()] + " " + m.getFullYear()));
    nav.appendChild(b(null, "c", function () { ui.month = new Date(m.getFullYear(), m.getMonth() + 1, 1); drawSheet(); },
                      { icon: "right" }));
    nav.lastChild.setAttribute("aria-label", "Next month");
    nav.appendChild(el("div", "fill"));
    nav.appendChild(b("Back to the next two weeks", "c", function () { ui.month = null; drawSheet(); }));
    grp.appendChild(nav);
    var grid = el("div", "month");
    ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].forEach(function (w) { grid.appendChild(el("div", "dow", w)); });
    for (var i = 0; i < m.getDay(); i++) grid.appendChild(el("div"));
    var days = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    for (var dnum = 1; dnum <= days; dnum++) {
      var d = new Date(m.getFullYear(), m.getMonth(), dnum);
      grid.appendChild(b(String(dnum), "c" + (sameDay(d, ui.day) ? " on" : ""), (function (dd) {
        return function () { ui.day = dd; ui.month = null; drawSheet(); };
      })(d), { disabled: d < today, pressed: sameDay(d, ui.day) }));
    }
    grp.appendChild(grid);
    return grp;
  }

  function timeSection() {
    // "Time" with its extra choices on the label line, then hours (daytime, or early/late) and minutes.
    var grp = el("div", "grp");
    var top = el("div", "rowb");
    top.appendChild(el("div", "lbl", ui.mode === "move" ? "New time" : "Time"));
    top.appendChild(el("div", "fill"));
    top.appendChild(b(ui.late ? "Show daytime hours" : "Earlier or later hours", "c small", function () {
      ui.late = !ui.late; drawSheet();
    }));
    if (ui.mode === "add2") {
      top.appendChild(b("All day", "c small" + (ui.allDay ? " on" : ""), function () {
        ui.allDay = !ui.allDay; drawSheet();
      }, { pressed: !!ui.allDay }));
    }
    grp.appendChild(top);
    var cols = el("div", "cols");
    var hours = ui.late ? LATE_HOURS : DAY_HOURS;
    var hb = el("div", "grp");
    var r1 = el("div", "rowb"), r2 = el("div", "rowb");
    hours.forEach(function (h, i) {
      var on = !ui.allDay && ui.hour === h;
      (i < Math.ceil(hours.length / 2) ? r1 : r2).appendChild(b(hourLabel(h), "c hour" + (on ? " on" : ""), function () {
        ui.hour = h; ui.allDay = false; ui.error = ""; drawSheet();
      }, { pressed: on }));
    });
    hb.appendChild(r1);
    hb.appendChild(r2);
    cols.appendChild(hb);
    var mins = el("div", "grp");
    var m1 = el("div", "rowb"), m2 = el("div", "rowb");
    [0, 15, 30, 45].forEach(function (mm, i) {
      var on = !ui.allDay && ui.min === mm;
      (i < 2 ? m1 : m2).appendChild(b(":" + pad(mm), "c min" + (on ? " on" : ""), function () {
        ui.min = mm; ui.allDay = false; drawSheet();
      }, { pressed: on }));
    });
    mins.appendChild(m1);
    mins.appendChild(m2);
    cols.appendChild(mins);
    grp.appendChild(cols);
    return grp;
  }

  function chosenText() {
    // "Monday, Nov 16 at 10:30 AM", or "Monday, Nov 16 · All day".
    if (!ui.day) return "";
    var d = midDate(ui.day);
    if (ui.allDay || (ui.mode === "move" && ui.ev.all_day)) return d + " · All day";
    if (ui.hour === null || ui.hour === undefined) return d;
    var h = ui.hour % 12 || 12;
    return d + " at " + h + ":" + pad(ui.min) + " " + (ui.hour < 12 ? "AM" : "PM");
  }

  // --------------------------------------------------------------- move

  function drawMove() {
    var ev = ui.ev;
    var card = el("div", "sheet wide");
    var head = el("div", "shead");
    head.appendChild(el("div", "t", "Move " + ev.title));
    var length = ev.all_day ? (ev.day_count > 1 ? ev.day_count + " days" : "all day")
      : lengthWords(Math.round((parseLocal(ev.end) - parseLocal(ev.start)) / 60000));
    head.appendChild(el("div", "s", "Now: " + midDate(parseLocal(ev.start)) + (ev.all_day ? "" : " at " + ev.time) + " · " + length));
    card.appendChild(head);
    var g1 = el("div", "grp");
    g1.appendChild(el("div", "lbl", ev.all_day && ev.day_count > 1 ? "New first day" : "New day"));
    g1.appendChild(dayPicker());
    card.appendChild(g1);
    if (!ev.all_day && !ui.month) card.appendChild(timeSection());
    if (ui.error) card.appendChild(el("div", "err", ui.error));
    var bar = el("div", "bar");
    bar.style.marginTop = "auto";
    bar.appendChild(el("div", "summary", chosenText()));
    bar.appendChild(el("div", "fill"));
    bar.appendChild(b("Cancel", "b", closeSheet, { always: true }));
    var st = parseLocal(ev.start);
    var unchanged = sameDay(ui.day, parseLocal(ev.start.slice(0, 10))) &&
      (ev.all_day || (ui.hour === st.getHours() && ui.min === st.getMinutes()));
    bar.appendChild(b("Move it", "b w primary", function () {
      var body = { occ: ev.occ, date: ymd(ui.day), time: ev.all_day ? "" : pad(ui.hour) + ":" + pad(ui.min) };
      send("/api/v1/events/" + ev.id + "/move", body, "Moved " + ev.title + " to " + chosenText());
    }, { disabled: unchanged }));
    card.appendChild(bar);
    return card;
  }

  function lengthWords(mins) {
    if (mins < 60) return mins + " minutes";
    var h = Math.floor(mins / 60), m = mins % 60;
    return h + " hour" + (h === 1 ? "" : "s") + (m ? " " + m + " min" : "");
  }

  // ---------------------------------------------------------------- add

  function openAdd() {
    if (!canAct()) { toast("Can't reach the calendar right now. Try again in a little while."); return; }
    var people = (data && data.people) || [];
    openSheet({ mode: "add1", title: "", shift: true, cat: null, day: parseLocal(ymd(now())), hour: null, min: 0,
                allDay: false, late: false, month: null, people: people.length === 1 ? [people[0].id] : [] });
  }

  var KEYS = ["1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm,.-"];

  function typeKey(ch) {
    if (ui.title.length >= 120) return;
    ui.title += ui.shift ? ch.toUpperCase() : ch;
    ui.shift = false;
    drawSheet();
  }

  function drawAddWhat() {
    var card = el("div", "sheet wide");
    var head = el("div", "shead");
    head.appendChild(el("div", "t", "Add · What is it?"));
    head.appendChild(el("div", "s", "Step 1 of 2"));
    card.appendChild(head);
    var line = el("div", "rowb");
    line.style.flexWrap = "nowrap";
    // inputmode="none": the page's own keyboard is used, so no system keyboard pops up over it.
    var box = el("input", "titlebox");
    box.setAttribute("inputmode", "none");
    box.setAttribute("aria-label", "Title");
    box.readOnly = true;
    box.value = ui.title;
    line.appendChild(box);
    line.appendChild(b("Cancel", "b", closeSheet, { always: true }));
    line.appendChild(b("Next", "b primary", function () {
      if (ui.title.trim()) { ui.mode = "add2"; drawSheet(); }
    }, { disabled: !ui.title.trim() }));
    card.appendChild(line);
    // Quick titles (D49): typing narrows them; a tap fills in the title (and category) and moves on.
    var titles = (data && data.quick_titles) || [];
    var typed = ui.title.trim().toLowerCase();
    var shown = titles.filter(function (q) { return !typed || q.title.toLowerCase().indexOf(typed) >= 0; });
    if (titles.length) {
      var g = el("div", "grp");
      g.appendChild(el("div", "lbl", shown.length ? "Or tap one" : "No quick title matches; keep typing"));
      var chips = el("div", "rowb");
      shown.forEach(function (q) {
        chips.appendChild(b(q.title, "c chip", function () {
          ui.title = q.title;
          if (q.category_id) ui.cat = q.category_id;
          ui.mode = "add2";
          drawSheet();
        }));
      });
      g.appendChild(chips);
      card.appendChild(g);
    }
    var kb = el("div", "kb");
    KEYS.forEach(function (row) {
      var r = el("div", "rowb");
      row.split("").forEach(function (ch) {
        r.appendChild(b(/[a-z]/.test(ch) && ui.shift ? ch.toUpperCase() : (/[a-z]/.test(ch) ? ch.toUpperCase() : ch),
                        "c", function () { typeKey(ch); }));
      });
      kb.appendChild(r);
    });
    var last = el("div", "rowb");
    last.appendChild(b("Shift", "c wide shift" + (ui.shift ? " on" : ""), function () { ui.shift = !ui.shift; drawSheet(); },
                       { pressed: ui.shift }));
    last.appendChild(b("Space", "c space", function () {
      if (ui.title && !/ $/.test(ui.title) && ui.title.length < 120) { ui.title += " "; drawSheet(); }
    }));
    last.appendChild(b("Delete", "c del", function () {
      ui.title = ui.title.slice(0, -1);
      if (!ui.title) ui.shift = true;
      drawSheet();
    }, { icon: "back" }));
    kb.appendChild(last);
    card.appendChild(kb);
    return card;
  }

  function drawAddWhen() {
    var card = el("div", "sheet wide");
    var head = el("div", "shead");
    head.appendChild(el("div", "t", "Add · " + ui.title.trim()));
    head.appendChild(el("div", "s", "Step 2 of 2 · when and who"));
    card.appendChild(head);
    var g1 = el("div", "grp");
    g1.appendChild(el("div", "lbl", "Day"));
    g1.appendChild(dayPicker());
    card.appendChild(g1);
    if (!ui.month) {
      card.appendChild(timeSection());

      var low = el("div", "bar");
      var who = el("div", "grp");
      who.appendChild(el("div", "lbl", "Who"));
      var wr = el("div", "rowb");
      ((data && data.people) || []).forEach(function (p) {
        var on = ui.people.indexOf(p.id) >= 0;
        var pb = b(null, "c person", function () {
          var i = ui.people.indexOf(p.id);
          if (i >= 0) ui.people.splice(i, 1); else ui.people.push(p.id);
          drawSheet();
        }, { pressed: on, icon: on ? "tick" : undefined });
        pb.appendChild(document.createTextNode(p.name));
        pb.style.borderColor = p.color;
        if (on) { pb.style.background = p.color; pb.style.color = textOn(p.color); }
        wr.appendChild(pb);
      });
      who.appendChild(wr);
      low.appendChild(who);
      low.appendChild(el("div", "fill"));
      low.appendChild(b("Back", "b", function () { ui.mode = "add1"; drawSheet(); }));
      var missing = [];
      if (!ui.allDay && (ui.hour === null || ui.hour === undefined)) missing.push("a time");
      if (!ui.people.length) missing.push("who");
      if (!ui.cat) missing.push("a category");
      low.appendChild(b("Save", "b w primary", saveAdd, { disabled: missing.length > 0 }));
      card.appendChild(low);

      var g3 = el("div", "grp");
      g3.appendChild(el("div", "lbl", "Category"));
      var cr = el("div", "rowb");
      ((data && data.categories) || []).forEach(function (c) {
        var on = ui.cat === c.id;
        var cb = b(null, "c cat" + (on ? " on" : ""), function () { ui.cat = c.id; drawSheet(); }, { pressed: on });
        var sq = el("span", "sq");
        sq.style.background = c.color;
        cb.appendChild(sq);
        cb.appendChild(document.createTextNode(c.name));
        cr.appendChild(cb);
      });
      g3.appendChild(cr);
      card.appendChild(g3);
      if (ui.error) card.appendChild(el("div", "err", ui.error));
      else if (missing.length) card.appendChild(el("div", "say small", "Still to choose: " + missing.join(", ") + "."));
    }
    return card;
  }

  function saveAdd() {
    var body = {
      title: ui.title.trim(), date: ymd(ui.day), all_day: !!ui.allDay,
      time: ui.allDay ? "" : pad(ui.hour) + ":" + pad(ui.min), people: ui.people.slice(), category_id: ui.cat
    };
    send("/api/v1/events", body, "Added " + body.title + " on " + chosenText());
  }

  // ------------------------------------------------- health (Phase 6a, D61)
  //
  // The server works out the levels from the Pi's reports. When the server
  // can't be reached the page decides for itself: Check after a few minutes,
  // Alert once the schedule is stale, in step with the "Not updated" bar.

  var HEALTH_WORDS = { none: "Not set up", ok: "OK", check: "Check", alert: "Alert" };
  var OFFLINE_CHECK_MIN = 5;

  function healthView(n) {
    if (!data || !data.health) return null;
    var h = data.health;
    var ageMin = lastOk ? (n.getTime() - lastOk) / 60000 : Infinity;
    if (ageMin <= OFFLINE_CHECK_MIN) return h;
    var at = new Date(lastOk);
    var when = daysBetween(at, n) === 0 ? clock(at) : DAYS[at.getDay()].slice(0, 3) + " " + clock(at);
    var level = isCurrent(n) ? "check" : "alert";
    return { level: level, label: HEALTH_WORDS[level], head: "Can't reach the calendar",
             sub: "Last heard from it at " + when + ". The readings below are from then.",
             rows: h.rows || [], versions: h.versions || "", set_up: h.set_up, offline: true };
  }

  function drawHealthButton(h) {
    var btn = $("health-btn");
    if (!h) { btn.hidden = true; return; }
    btn.hidden = false;
    btn.className = "hbtn lv-" + h.level;
    btn.textContent = "";
    btn.appendChild(icon(h.level));
    btn.appendChild(document.createTextNode(h.label));
    btn.setAttribute("aria-label", "Calendar health: " + h.label);
  }

  function lvChip(level) {
    var c = el("span", "lvchip lv-" + level);
    c.appendChild(icon(level));
    c.appendChild(document.createTextNode(HEALTH_WORDS[level]));
    return c;
  }

  function drawHealth() {
    var h = healthView(now()) || { level: "none", label: "Not set up", head: "Not set up yet", sub: "", rows: [], versions: "" };
    var card = el("div", "sheet health");
    var head = el("div", "hhead");
    var dot = el("span", "dot lv-" + h.level);
    dot.appendChild(icon(h.level));
    head.appendChild(dot);
    var words = el("div");
    words.appendChild(el("div", "t", h.head));
    if (h.sub) words.appendChild(el("div", "s", h.sub));
    head.appendChild(words);
    card.appendChild(head);
    var rows = el("div", "hrows");
    (h.rows || []).forEach(function (r) {
      var row = el("div", "hrow");
      row.appendChild(el("div", "k", r.label));
      row.appendChild(el("div", "v", r.value));
      var right = el("div", "r");
      if (r.note) right.appendChild(el("span", "n", r.note));
      right.appendChild(lvChip(r.level));
      row.appendChild(right);
      rows.appendChild(row);
    });
    if (h.versions) {
      var ver = el("div", "hrow ver");
      ver.appendChild(el("div", "k", "Versions"));
      ver.appendChild(el("div", "v", h.versions));
      ver.appendChild(el("div", "r"));
      rows.appendChild(ver);
    }
    card.appendChild(rows);
    var able = canAct() && h.set_up && !h.offline;
    if (!h.set_up) card.appendChild(el("div", "say small", "Restart and Shut down work once the Pi's health helper is set up."));
    else if (!able) card.appendChild(offlineWarn("Can't reach the calendar right now, so Restart and Shut down can't be sent."));
    var bar = el("div", "bar");
    bar.appendChild(b("Restart", "b w", function () { openSheet({ mode: "restart" }); }, { icon: "restart", disabled: !able }));
    bar.appendChild(b("Shut down", "b w dangerText", function () { openSheet({ mode: "shutdown" }); }, { icon: "power", disabled: !able }));
    var aside = el("div", "aside");
    aside.appendChild(document.createTextNode("More on the editor's Health page"));
    aside.appendChild(el("br"));
    aside.appendChild(document.createTextNode("Closes by itself after a minute"));
    bar.appendChild(aside);
    bar.appendChild(b("Close", "b w primary", closeSheet, { always: true }));
    card.appendChild(bar);
    return card;
  }

  function drawDeviceConfirm() {
    var off = ui.mode === "shutdown";
    var card = el("div", "sheet narrow");
    card.appendChild(el("h3", null, off ? "Turn the calendar off?" : "Restart the calendar?"));
    card.appendChild(el("div", "say", off
      ? "It stays off until someone presses the button on the back of the Pi. Pulling the plug and plugging it back in also starts it."
      : "The screen goes dark for about a minute, then the calendar comes back by itself."));
    if (ui.error) card.appendChild(el("div", "err", ui.error));
    // Cancel is first; the button that acts sits at the far right, away from it.
    var bar = el("div", "bar");
    bar.appendChild(b("Cancel", "b w primary", function () { openSheet({ mode: "health" }); }, { always: true }));
    bar.appendChild(el("div", "fill"));
    bar.appendChild(b(off ? "Turn off" : "Restart", "b go" + (off ? " off" : ""), function () {
      send("/api/v1/device/command", { action: off ? "shutdown" : "restart" },
           off ? "Turning off… press the button on the Pi to start it again." : "Restarting… back in about a minute.");
    }, { icon: off ? "power" : "restart" }));
    card.appendChild(bar);
    return card;
  }

  // ------------------------------------------------------------ sending

  function send(path, body, doneText) {
    if (!canAct()) {
      ui.error = "Can't reach the calendar right now. Try again in a little while.";
      drawSheet();
      return;
    }
    ui.busy = true;
    ui.error = "";
    drawSheet();
    fetchJson(apiUrl(path), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (r) {
      if (r.status === 200 && r.body && r.body.ok) {
        closeSheet();
        toast(doneText);
        comingSig = "";
        refresh();
        return;
      }
      if (!ui) return;
      ui.busy = false;
      if (r.status === 409 && r.body && r.body.error === "not_set_up") {
        ui.error = "The Pi's health helper isn't set up yet, so this can't work yet.";
      } else if (r.status === 404) {
        ui.error = "This has changed since the screen last updated. Close this and try again.";
        refresh();
      } else {
        ui.error = "That didn't work. Please check the choices and try again.";
      }
      drawSheet();
    }).catch(function () {
      if (!ui) return;
      ui.busy = false;
      ui.error = "Couldn't reach the calendar. Try again in a little while.";
      drawSheet();
    });
  }

  // ------------------------------------------------------------- network

  function apiUrl(path) {
    return path + (testNow ? (path.indexOf("?") < 0 ? "?" : "&") + "now=" + encodeURIComponent(stamp(now())) : "");
  }

  function fetchJson(url, opts) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
    opts = opts || {};
    opts.cache = "no-store";
    if (ctrl) opts.signal = ctrl.signal;
    return fetch(url, opts).then(function (res) {
      clearTimeout(timer);
      return res.json().then(function (body) { return { status: res.status, body: body }; });
    }, function (err) { clearTimeout(timer); throw err; });
  }

  function saveCache() {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify({ data: data, lastOk: lastOk })); } catch (e) { /* storage off */ }
  }

  function loadCache() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (saved && saved.data && saved.data.api === 1 && saved.data.days) {
        data = saved.data;
        lastOk = saved.lastOk;
      }
    } catch (e) { /* storage off or damaged: start empty */ }
  }

  function refresh() {
    return fetchJson(apiUrl(API)).then(function (r) {
      if (r.status === 200 && r.body && r.body.ok && r.body.api === 1) {
        if (pageVersion && r.body.app_version && r.body.app_version !== pageVersion) {
          // The server was updated: load the new page.
          location.reload();
          return;
        }
        data = r.body;
        lastOk = now().getTime();
        saveCache();
      }
    }).catch(function () { /* unreachable: keep showing the saved copy */ }).then(function () {
      everLoaded = true;
      render();
    });
  }

  function act(id, action) {
    fetchJson(apiUrl("/api/v1/reminders/" + id + "/" + action), { method: "POST" }).then(function (r) {
      if (action === "done") {
        if (r.status === 200) {
          local[id] = { state: "done", until: Date.now() + UNDO_SECONDS * 1000 };
        } else {
          delete local[id];   // no longer showing (someone else ticked it): refresh tidies up
          refresh();
        }
      } else {
        delete local[id];
        refresh();
      }
      render();
    }).catch(function () {
      local[id] = { state: "problem", until: Date.now() + 4000 };
      render();
    });
  }

  function tick() {
    // A panel left alone closes by itself; Coming up goes back to the top (D48).
    if (ui && !ui.busy && Date.now() - lastTouch > SHEET_IDLE_MS) closeSheet();
    var box = $("coming");
    if (box.scrollTop > 0 && Date.now() - Math.max(comingTouched, lastTouch) > TOP_RETURN_MS) {
      box.scrollTop = 0;
      updateComingMore();
    }
    // Count down Undo; when it runs out the reminder leaves the screen.
    var changed = false, expired = false;
    Object.keys(local).forEach(function (id) {
      var st = local[id];
      if (Date.now() >= st.until) {
        if (st.state === "done") expired = true;
        delete local[id];
        changed = true;
      } else if (st.state === "done") {
        changed = true;
      }
    });
    if (expired) refresh();
    else if (changed) render();
  }

  // -------------------------------------------------------------- wiring

  // A tap on the black night screen only wakes it; it can't press anything underneath.
  function wake(e) {
    e.preventDefault();
    e.stopPropagation();
    wakeUntil = Date.now() + cfg().wake_seconds * 1000;
    $("night").hidden = true;
  }
  $("night").addEventListener("pointerdown", wake, true);
  $("night").addEventListener("touchstart", wake, { capture: true, passive: false });
  $("night").addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); }, true);
  // While awake at night, any tap keeps it awake a little longer.
  document.addEventListener("pointerdown", function () {
    if (inDarkHours(now(), cfg()) && Date.now() <= wakeUntil) wakeUntil = Date.now() + cfg().wake_seconds * 1000;
  }, true);
  document.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  document.addEventListener("pointerdown", function () { lastTouch = Date.now(); }, true);
  $("sheet").addEventListener("click", onOverlayTap);
  $("add-btn").addEventListener("click", openAdd);
  $("health-btn").addEventListener("click", function () { openSheet({ mode: "health" }); });
  $("top-btn").addEventListener("click", function () { $("coming").scrollTop = 0; updateComingMore(); });
  $("coming").addEventListener("scroll", function () { comingTouched = Date.now(); updateComingMore(); }, { passive: true });

  loadCache();
  render();
  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(render, RENDER_MS);
  setInterval(tick, 1000);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
  window.addEventListener("resize", function () { comingSig = ""; render(); });
})();
