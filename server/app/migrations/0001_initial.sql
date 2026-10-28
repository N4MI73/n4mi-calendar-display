-- 0001_initial.sql — N4MI Calendar Display, Phase 1 schema.
--
-- Times are stored as local wall-clock text in America/New_York (no UTC
-- conversion), so an appointment keeps its time across daylight saving changes.
--   timed event:   start_local / end_local = 'YYYY-MM-DDTHH:MM'
--   all-day event: start_local / end_local = 'YYYY-MM-DD' (end date inclusive)
--
-- The recurrence and Google columns are created now (nullable) so later phases
-- add tables instead of reshaping `events`. No people are seeded: people are
-- added in the editor, so no real names ever appear in the code.

CREATE TABLE people (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    color       TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE categories (
    id                        INTEGER PRIMARY KEY,
    name                      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    color                     TEXT    NOT NULL,
    default_reminder_minutes  INTEGER,            -- NULL = no reminder
    sort_order                INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE events (
    id                INTEGER PRIMARY KEY,
    title             TEXT    NOT NULL,
    location          TEXT,
    notes             TEXT,
    all_day           INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
    start_local       TEXT    NOT NULL,
    end_local         TEXT    NOT NULL,
    category_id       INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    reminder_mode     TEXT    NOT NULL DEFAULT 'default'
                              CHECK (reminder_mode IN ('default', 'none', 'custom')),
    reminder_minutes  INTEGER,                     -- used when reminder_mode = 'custom'
    rrule             TEXT,                        -- Phase 4
    recurrence_end    TEXT,                        -- Phase 4
    source            TEXT    NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'google')),
    google_source     TEXT,                        -- Phase 5: which import source
    google_uid        TEXT,                        -- Phase 5
    google_last_seen  TEXT,                        -- Phase 5
    locally_modified  INTEGER NOT NULL DEFAULT 0,  -- Phase 5
    created           TEXT    NOT NULL,
    updated           TEXT    NOT NULL
);

CREATE INDEX idx_events_start ON events (start_local);

-- One or more people per event (decision D36).
CREATE TABLE event_people (
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    PRIMARY KEY (event_id, person_id)
);

CREATE INDEX idx_event_people_person ON event_people (person_id);

CREATE TABLE config (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Starting categories (decisions D26, D27). Names, colors and reminder
-- defaults are editable in the editor.
INSERT INTO categories (name, color, default_reminder_minutes, sort_order) VALUES
    ('Medical',          '#E5484D', 120,  1),
    ('Family',           '#46A758',  60,  2),
    ('Home',             '#F76B15',  60,  3),
    ('Social',           '#12A594',  60,  4),
    ('Travel',           '#FFC53D',  60,  5),
    ('Club / Ham radio', '#99D52A',  60,  6),
    ('Info',             '#C4C7D0', NULL, 7),
    ('Other',            '#8B8D98',  60,  8);

-- Settings defaults (decisions D13, D29).
INSERT INTO config (key, value) VALUES
    ('upcoming_days',      '14'),
    ('stale_minutes',      '30'),
    ('dark_start',         '23:00'),
    ('dark_end',           '06:00'),
    ('wake_seconds',       '120');
