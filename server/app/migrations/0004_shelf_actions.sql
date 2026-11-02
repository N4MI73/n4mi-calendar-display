-- 0004_shelf_actions.sql — Phase 4: actions on the shelf display.
--
-- Quick titles (decision D49): the one-tap titles offered by the display's Add
-- form, kept by Dan in the editor. A title may carry a category, which the
-- chip also selects; deleting the category just clears it.
--
-- "Added on the shelf" (D52): events added from the display are marked so the
-- editor can show them.
--
-- Recently deleted (D51): a deleted event, or one date deleted from the shelf,
-- is kept here for 3 days (at most 20 entries) so it can be restored from the
-- editor. `snapshot` is JSON holding what is needed to put it back.
-- The server backs up the database before applying this migration.

CREATE TABLE quick_titles (
    id           INTEGER PRIMARY KEY,
    title        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    created      TEXT    NOT NULL
);

ALTER TABLE events ADD COLUMN added_on_shelf INTEGER NOT NULL DEFAULT 0 CHECK (added_on_shelf IN (0, 1));

CREATE TABLE recently_deleted (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('event', 'date')),
    event_id    INTEGER,                       -- the event's id when it was deleted
    occ_date    TEXT,                          -- kind = 'date': the date removed
    title       TEXT    NOT NULL,
    when_label  TEXT    NOT NULL,              -- for the list, e.g. "Fri, Nov 13 at 3:00 PM"
    from_shelf  INTEGER NOT NULL DEFAULT 0 CHECK (from_shelf IN (0, 1)),
    snapshot    TEXT    NOT NULL,
    deleted_at  TEXT    NOT NULL               -- local time, YYYY-MM-DDTHH:MM:SS
);

CREATE INDEX idx_recently_deleted_at ON recently_deleted (deleted_at);
