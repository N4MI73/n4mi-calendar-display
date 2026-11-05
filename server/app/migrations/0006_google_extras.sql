-- 0006_google_extras.sql — Phase 5b (decisions D59–D60 and the "Not in Google" list).
--
-- events.not_needed_in_google: Dan marked a local event as not wanted in Google,
--   so the "Not in Google" list leaves it out.
-- import_rules: per-source title rules (D60). A new Google event whose title
--   contains `word` (any capitals) takes the rule's category and people instead
--   of the source's defaults. people is a JSON list of person ids ('[]' = use
--   the source's default people).
-- Holidays need no table: they are calculated (app/holidays.py); the choices
--   live in the config table (holidays_on, holidays).
-- The server backs up the database before applying this migration.

ALTER TABLE events ADD COLUMN not_needed_in_google INTEGER NOT NULL DEFAULT 0 CHECK (not_needed_in_google IN (0, 1));

CREATE TABLE import_rules (
    id           INTEGER PRIMARY KEY,
    slot         INTEGER NOT NULL,
    word         TEXT    NOT NULL,
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    people       TEXT    NOT NULL DEFAULT '[]',
    created      TEXT    NOT NULL
);
