-- 0002_recurrence_reminders.sql — Phase 2: repeating events and "I did it" reminders.
--
-- Repeating events use the existing events.rrule (RRULE text, no DTSTART/UNTIL)
-- and events.recurrence_end (optional last date, YYYY-MM-DD, inclusive).
-- The server backs up the database before applying this migration.

-- One row per occurrence that was skipped or changed, keyed by the date the
-- occurrence originally fell on. A changed occurrence carries its own times and
-- text; people, category and reminder always follow the series.
CREATE TABLE event_exceptions (
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    occ_date     TEXT    NOT NULL,                 -- original date, YYYY-MM-DD
    skipped      INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0, 1)),
    start_local  TEXT,                             -- changed occurrence only
    end_local    TEXT,
    title        TEXT,
    location     TEXT,
    notes        TEXT,
    updated      TEXT    NOT NULL,
    PRIMARY KEY (event_id, occ_date)
);

-- Household "I did it" reminders (decision D37). One-line, recurring, shown on
-- the display from their appear time until someone taps "I did it".
--   carry_over = 0: disappears at the end of its day if not done.
--   carry_over = 1: stays until done; a later occurrence never adds a second copy.
-- last_done_* records the occurrence most recently ticked; prev_done_* keeps the
-- values before that, so "Undo" can restore them.
CREATE TABLE household_reminders (
    id              INTEGER PRIMARY KEY,
    text            TEXT    NOT NULL,
    rrule           TEXT    NOT NULL,
    start_date      TEXT    NOT NULL,             -- YYYY-MM-DD, first possible occurrence
    recurrence_end  TEXT,                         -- optional last date, inclusive
    appear_time     TEXT,                         -- HH:MM; NULL = end of dark hours
    carry_over      INTEGER NOT NULL DEFAULT 0 CHECK (carry_over IN (0, 1)),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_done_occ   TEXT,                         -- YYYY-MM-DD of the occurrence ticked
    last_done_at    TEXT,                         -- local time it was ticked
    prev_done_occ   TEXT,
    prev_done_at    TEXT,
    created         TEXT    NOT NULL,
    updated         TEXT    NOT NULL
);
