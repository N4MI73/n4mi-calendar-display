-- 0005_import.sql — Phase 5a: reading Google calendars (decisions D4, D5, D53–D58).
--
-- import_sources: one row per Google calendar (a "slot"). The calendar's private
--   address is NOT stored here: it is a credential and lives only in the
--   Portainer variable CAL_SOURCE_<slot>_URL. This table holds its name and the
--   defaults used by "Add ticked with defaults" (D55).
-- import_items: what the last fetch found that needs Dan's decision (new events,
--   possible duplicates, changes, removals). Rebuilt on every fetch; the
--   decisions themselves live in events and import_skips.
-- import_skips: Google events Dan chose not to import, so they aren't offered
--   again (also set when an imported event is deleted).
-- events.google_snapshot: Google's version of an imported or linked event as
--   last accepted (JSON), used to tell "changed in Google" from "edited here".
-- The server backs up the database before applying this migration.

CREATE TABLE import_sources (
    slot                 INTEGER PRIMARY KEY,
    name                 TEXT    NOT NULL,
    default_category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    default_people       TEXT    NOT NULL DEFAULT '[]',    -- JSON list of person ids
    last_fetch           TEXT,                             -- local time of the last fetch
    last_result          TEXT                              -- plain-language summary or error
);

CREATE TABLE import_items (
    id          INTEGER PRIMARY KEY,
    slot        INTEGER NOT NULL,
    uid         TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('new', 'duplicate', 'changed', 'conflict', 'removed', 'same_uid')),
    event_id    INTEGER,                                   -- the local event involved, if any
    data        TEXT    NOT NULL,                          -- Google's version (JSON); '{}' for removed
    created     TEXT    NOT NULL,
    UNIQUE (slot, uid)
);

CREATE TABLE import_skips (
    slot     INTEGER NOT NULL,
    uid      TEXT    NOT NULL,
    title    TEXT,
    created  TEXT    NOT NULL,
    PRIMARY KEY (slot, uid)
);

ALTER TABLE events ADD COLUMN google_snapshot TEXT;
