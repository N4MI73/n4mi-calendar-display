-- 0008_out_of_town.sql — v1.1: the Out of town marker.
--
-- events.out_of_town: the event happens away from home (1) or at home or
--   nearby (0, every existing event). Set in the editor and the import review,
--   once for a whole series; never changed by an import.
-- import_places: the answers Dan gave to "Local or out of town?" at import,
--   one per place. `place` is the location as Google wrote it, trimmed, with
--   runs of spaces made single and capitals ignored; `label` is how it was
--   written the first time, for the Settings list.
-- The out-of-town reminder (oot_reminder_days) and the local towns list
--   (local_towns, JSON) live in the config table, with defaults in store.py.
-- The server backs up the database before applying this migration.

ALTER TABLE events ADD COLUMN out_of_town INTEGER NOT NULL DEFAULT 0 CHECK (out_of_town IN (0, 1));

CREATE TABLE import_places (
    place        TEXT    PRIMARY KEY,
    label        TEXT    NOT NULL,
    out_of_town  INTEGER NOT NULL CHECK (out_of_town IN (0, 1)),
    answered     TEXT    NOT NULL
);
