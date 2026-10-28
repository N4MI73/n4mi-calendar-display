-- 0003_allday_reminders.sql — Phase 3: reminders for all-day events (decision D44).
--
-- Each category gains a second default, in whole days, used for all-day events
-- (for example Family = 2 days before a birthday). NULL = no reminder, which is
-- the starting value for every category. An event can still override it with
-- its own reminder or "no reminder" (events.reminder_mode / reminder_minutes;
-- an all-day event's custom reminder is stored in minutes, a multiple of 1440).
-- The server backs up the database before applying this migration.

ALTER TABLE categories ADD COLUMN default_allday_days INTEGER;  -- NULL = no reminder
