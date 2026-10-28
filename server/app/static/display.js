/* N4MI Calendar Display — the shelf screen (Phase 3).
 *
 * Reads /api/v1/display every minute and keeps the last good copy in the
 * browser, so the screen keeps working from it when the server can't be
 * reached, with a clear "Not updated since" bar (decision D12). It never shows
 * "nothing scheduled" unless the schedule is current (D45).
 *
 * The server has already expanded repeating events and worked out when each
 * banner starts (`remind_at`); this page only picks what to show now.
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

  function tags(ev, withWhere) {
    var box = el("div", "tags");
    if (!ev.info) ev.people.forEach(function (p) { box.appendChild(personPill(p)); });
    box.appendChild(categoryPill(ev.category));
    if (withWhere && ev.location) box.appendChild(el("span", "where", ev.location));
    return box;
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

    // Header.
    $("date").textContent = DAYS[n.getDay()] + ", " + MONTHS[n.getMonth()] + " " + n.getDate();
    $("clock").textContent = clock(n);

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
    return row;
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
    return row;
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
    return item;
  }

  function drawComing(n, list, c) {
    var box = $("coming");
    var more = $("coming-more");
    box.textContent = "";
    more.textContent = "";
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
      if (!evs.length) return;
      var label = (off === 1 ? "Tomorrow · " + DAYS[d.getDay()].slice(0, 3) + ", " : DAYS[d.getDay()] + " · ")
        + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate();
      groups.push({ label: label, events: evs });
    });
    if (!groups.length) {
      more.textContent = "Nothing in " + rangeWords;
      return;
    }
    var nodes = groups.map(function (g) {
      var day = el("div", "day");
      day.appendChild(el("div", "day-name", g.label));
      var items = g.events.map(function (ev) { var it = comingItem(ev); day.appendChild(it); return it; });
      box.appendChild(day);
      return { node: day, items: items };
    });
    // If it doesn't all fit, take events off the end one at a time (whole days once empty).
    var hiddenCount = 0;
    while (box.scrollHeight > box.clientHeight + 1 && nodes.length) {
      var last = nodes[nodes.length - 1];
      if (nodes.length === 1 && last.items.length === 1) break;
      last.node.removeChild(last.items.pop());
      hiddenCount++;
      if (!last.items.length) { box.removeChild(last.node); nodes.pop(); }
    }
    if (hiddenCount) more.textContent = hiddenCount + " more in " + rangeWords;
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

  loadCache();
  render();
  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(render, RENDER_MS);
  setInterval(tick, 1000);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
  window.addEventListener("resize", render);
})();
