from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY,
    start INTEGER NOT NULL,          -- unix seconds, UTC
    end INTEGER NOT NULL,
    sport TEXT NOT NULL,             -- run / strength / other, as read from the file
    sport_override TEXT,             -- set by hand when the file got it wrong
    source TEXT NOT NULL,            -- 'fit', later 'takeout'
    source_ref TEXT NOT NULL UNIQUE, -- sha256 of the file, so re-imports are skipped
    archive_path TEXT,
    detail TEXT,                     -- the file's own sport name
    peak_hr INTEGER,                 -- highest heart rate after removing glitches
    imported INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS workouts_start ON workouts (start);

-- Small bits of state: the Dropbox cursor, when the last poll ran.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Seconds spent at each bpm. Load for any zone settings is computed from this.
CREATE TABLE IF NOT EXISTS hr_hist (
    workout_id INTEGER NOT NULL REFERENCES workouts (id) ON DELETE CASCADE,
    bpm INTEGER NOT NULL,
    seconds REAL NOT NULL,
    PRIMARY KEY (workout_id, bpm)
);

-- One row per day from the ring's exports; a later export of the same day replaces it.
CREATE TABLE IF NOT EXISTS vitals (
    day TEXT PRIMARY KEY,            -- YYYY-MM-DD, local
    avg_hr REAL, min_hr REAL, max_hr REAL,
    spo2 REAL,
    hrv REAL, hrv_min REAL, hrv_max REAL,  -- ms; the ring's daily average, min and max
    imported INTEGER NOT NULL
);

-- Records the phone sends from Health Connect: resting heart rate and sleep.
CREATE TABLE IF NOT EXISTS health_records (
    id TEXT PRIMARY KEY,             -- Health Connect's record id; a resent record replaces the old one
    type TEXT NOT NULL,              -- resting_hr / sleep
    start INTEGER NOT NULL,          -- unix seconds
    end INTEGER NOT NULL,
    value REAL NOT NULL,             -- bpm, or seconds asleep
    origin TEXT NOT NULL,            -- the app that wrote it
    detail TEXT,                     -- sleep stages, as JSON
    received INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS health_records_type ON health_records (type, start);

-- Pace tests found in runs (see pacetest.py), measured once when the run is imported.
CREATE TABLE IF NOT EXISTS pace_tests (
    workout_id INTEGER PRIMARY KEY REFERENCES workouts (id) ON DELETE CASCADE,
    start INTEGER NOT NULL,          -- unix seconds, when the test lap began
    seconds REAL NOT NULL,
    distance REAL NOT NULL,          -- metres
    pace REAL NOT NULL,              -- seconds per km over the last 10 minutes
    hr REAL NOT NULL,                -- average over the last 10 minutes
    spread REAL NOT NULL,            -- minute-to-minute pace variation, a fraction
    decoupling REAL NOT NULL,        -- heart rate against pace, second half vs first
    hr_stop INTEGER,                 -- when you stopped, and a minute later
    hr_after INTEGER,
    not_test INTEGER NOT NULL DEFAULT 0  -- marked by hand on the dashboard
);

-- Threshold test candidates in runs (see thresholdtest.py): 25-40 minute laps. Whether one was
-- hard enough to be a test is decided when it's shown.
CREATE TABLE IF NOT EXISTS threshold_tests (
    workout_id INTEGER PRIMARY KEY REFERENCES workouts (id) ON DELETE CASCADE,
    start INTEGER NOT NULL,          -- unix seconds, when the test lap began
    seconds REAL NOT NULL,
    hr REAL NOT NULL,                -- average over the last 20 minutes: the LTHR
    peak INTEGER NOT NULL,           -- highest in the lap
    distance REAL,                   -- metres, whole lap; none without GPS
    pace REAL,                       -- seconds per km over the last 20 minutes
    not_test INTEGER NOT NULL DEFAULT 0  -- marked by hand on the dashboard
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
