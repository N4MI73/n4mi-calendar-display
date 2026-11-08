-- Phase 6a (D61): the shelf Pi's health reports, when the display last fetched
-- the schedule, and Restart / Shut down requests from the health panel.

-- One row: the latest report from the Pi helper, and the display's last contact.
CREATE TABLE device_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    report_received TEXT,          -- server local time, 'YYYY-MM-DDTHH:MM:SS'
    report_json     TEXT,          -- the readings as accepted (JSON)
    display_seen    TEXT           -- server local time of the last /api/v1/display
);
INSERT INTO device_state (id) VALUES (1);

-- Restart / Shut down. At most one is waiting; it expires after a few minutes.
CREATE TABLE device_commands (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    action    TEXT NOT NULL CHECK (action IN ('restart', 'shutdown')),
    requested TEXT NOT NULL,       -- server local time
    delivered TEXT                 -- when the Pi helper picked it up
);
