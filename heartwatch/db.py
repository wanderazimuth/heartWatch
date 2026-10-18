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
