"""Threshold tests: 30 minutes as hard as you can hold, to find your lactate threshold heart rate.

Friel's field test: warm up, press lap, run 30 minutes alone at the hardest
effort you can keep up evenly, press lap. The average heart rate over the
last 20 minutes (the first 10 are heart rate catching up) is your lactate
threshold heart rate, LTHR, and the zones are set from it (load.py).

HeartWatch finds it like a pace test (pacetest.py), by its laps: a manual lap
of 25 to 40 minutes. Whether that lap was hard enough to be a test is decided
when it's shown, against your resting and max heart rate at the time, since
those can come from the data too (profile.py). An easy lap that long is a
pace test instead.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import load, pacetest
from .config import Config

MIN_LAP, MAX_LAP = 25 * 60, 40 * 60  # seconds
LAST = 20 * 60  # the part of the test that's measured
MIN_SAMPLES = 10 * 60  # heart-rate readings needed in those 20 minutes
HARD_FROM = 0.8  # heart-rate reserve over the last 20 minutes; below it, the lap wasn't a threshold effort


@dataclass
class Test:
    start: int  # unix seconds, when the test lap began
    seconds: float
    hr: float  # average over the last 20 minutes
    peak: int
    distance: float | None = None  # metres, whole lap
    pace: float | None = None  # seconds per km, last 20 minutes


def find(source: str | Path | BinaryIO) -> Test | None:
    """The hardest lap in a run's FIT file that could be a threshold test, or None."""
    laps, hr, dist = pacetest.read(source)
    found = [t for t in (_measure(hr, dist, a, b) for a, b in laps if MIN_LAP <= b - a <= MAX_LAP) if t]
    return max(found, key=lambda t: t.hr, default=None)


def _measure(hr: list[tuple[float, int]], dist: list[tuple[float, float]], start: float, end: float) -> Test | None:
    last = [v for t, v in hr if end - LAST <= t <= end]
    if len(last) < MIN_SAMPLES:
        return None
    test = Test(int(start), end - start, sum(last) / len(last), max(v for t, v in hr if start <= t <= end))
    a, b = pacetest._at(dist, start), pacetest._at(dist, end)
    if a is not None and b is not None and b > a:
        test.distance = b - a
        speed = pacetest._speed(dist, end - LAST, end)
        test.pace = 1000 / speed if speed else None
    return test


def save(conn: sqlite3.Connection, workout_id: int, test: Test | None) -> None:
    """Store what was found in a workout, keeping a "not a test" mark made by hand."""
    with conn:
        if test is None:
            conn.execute("DELETE FROM threshold_tests WHERE workout_id = ?", (workout_id,))
            return
        conn.execute(
            "INSERT INTO threshold_tests (workout_id, start, seconds, hr, peak, distance, pace)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (workout_id) DO UPDATE SET"
            " start = excluded.start, seconds = excluded.seconds, hr = excluded.hr, peak = excluded.peak,"
            " distance = excluded.distance, pace = excluded.pace",
            (workout_id, test.start, test.seconds, test.hr, test.peak, test.distance, test.pace))


def scan(conn: sqlite3.Connection, cfg: Config) -> int:
    """Look for tests in every run's archived FIT file again. Returns how many laps could be one."""
    for row in conn.execute("SELECT id, archive_path FROM workouts WHERE source = 'fit'"
                            " AND COALESCE(sport_override, sport) = 'run'").fetchall():
        try:
            test = find(Path(cfg.archive_dir) / row["archive_path"])
        except Exception:
            test = None
        save(conn, row["id"], test)
    return conn.execute("SELECT COUNT(*) FROM threshold_tests").fetchone()[0]


def hard(hr: float, cfg: Config) -> bool:
    return load.reserve(hr, cfg) >= HARD_FROM


def tests_json(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """The laps hard enough to be tests, oldest first, with any marked "not a test"."""
    out = []
    for r in conn.execute("SELECT * FROM threshold_tests ORDER BY start"):
        if not hard(r["hr"], cfg):
            continue
        out.append({
            "id": r["workout_id"], "start": datetime.fromtimestamp(r["start"]).isoformat(timespec="minutes"),
            "minutes": round(r["seconds"] / 60, 1), "lthr": round(r["hr"]), "peak": r["peak"],
            "distance": None if r["distance"] is None else round(r["distance"]),
            "pace": None if r["pace"] is None else round(r["pace"], 2),
            "not_test": bool(r["not_test"]),
        })
    return out


def latest(conn: sqlite3.Connection, cfg: Config) -> dict | None:
    """The newest test not marked "not a test"."""
    return next((t for t in reversed(tests_json(conn, cfg)) if not t["not_test"]), None)
