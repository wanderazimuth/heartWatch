"""Pace tests: the same steady run now and then, to see aerobic fitness change.

A test is a run with a lap pressed by hand at the start and the end of 15 or
more minutes at an even pace, ideally followed by stopping dead for a minute.
HeartWatch finds it in the Wahoo app's FIT file by those laps (the name given
to a run in the app afterwards never reaches the file) and measures, over the
test's last 10 minutes, once heart rate has settled:

- average heart rate, and pace from distance (the per-second speed in the
  file jumps around with GPS noise, distance doesn't);
- beats per km, heart rate times minutes per km, so a test run a little
  faster or slower than the last is still a fair comparison;
- pace spread, how much the minute-by-minute pace varied over the whole test;
- decoupling, how much heart rate rose against pace from the first half of
  the test to the second (Friel's aerobic decoupling; under 5% is steady);
- recovery, how far heart rate fell in the minute after stopping. Only when
  the recording kept running (auto-pause off) and you stood still.
"""
from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import fitdecode

from .config import Config

MIN_LAP = 15 * 60  # seconds
LAST = 10 * 60  # the part of the test that's measured
MAX_SPREAD = 0.15  # minute-to-minute pace variation beyond which a lap is intervals, not a test
STOP_WITHIN = 30  # seconds after the lap press to stop
STOPPED = 2  # metres in 5 seconds: standing still
RECOVERY = 60  # seconds
STILL = 20  # metres moved in the recovery minute, at most


@dataclass
class Test:
    start: int  # unix seconds, when the test lap began
    seconds: float
    distance: float  # metres, whole test
    pace: float  # seconds per km, last 10 minutes
    hr: float  # average, last 10 minutes
    spread: float  # pace variation as a fraction of the mean
    decoupling: float  # fraction
    hr_stop: int | None = None
    hr_after: int | None = None  # RECOVERY seconds after stopping

    @property
    def beats_per_km(self) -> float:
        return self.hr * self.pace / 60


def find(source: str | Path | BinaryIO) -> Test | None:
    """The pace test in a run's FIT file, or None if it has none."""
    laps, hr, dist = read(source)
    found = [t for t in (_measure(hr, dist, a, b) for a, b in laps if b - a >= MIN_LAP) if t]
    return max(found, key=lambda t: t.seconds, default=None)


def read(source: str | Path | BinaryIO) -> tuple[list[tuple[float, float]], list[tuple[float, int]], list[tuple[float, float]]]:
    """A run's manual laps (start, end), heart rate and distance, by unix time."""
    laps: list[tuple[float, float]] = []
    hr: list[tuple[float, int]] = []
    dist: list[tuple[float, float]] = []
    with fitdecode.FitReader(source) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "lap" and frame.get_value("lap_trigger", fallback=None) == "manual":
                start, end = frame.get_value("start_time", fallback=None), frame.get_value("timestamp", fallback=None)
                if start and end:
                    laps.append((start.timestamp(), end.timestamp()))
            elif frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                bpm = frame.get_value("heart_rate", fallback=None)
                d = frame.get_value("distance", fallback=None)
                if bpm:
                    hr.append((ts.timestamp(), int(bpm)))
                if d is not None:
                    dist.append((ts.timestamp(), float(d)))
    hr.sort()
    dist.sort()
    return laps, hr, dist


def _at(series: list[tuple[float, float]], t: float) -> float | None:
    """The last value at or before t."""
    i = bisect_right(series, (t, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


def _mean_hr(hr: list[tuple[float, int]], a: float, b: float) -> float | None:
    vals = [v for t, v in hr if a <= t <= b]
    return sum(vals) / len(vals) if vals else None


def _speed(dist: list[tuple[float, float]], a: float, b: float) -> float | None:
    da, db = _at(dist, a), _at(dist, b)
    return None if da is None or db is None or db <= da else (db - da) / (b - a)


def _measure(hr: list[tuple[float, int]], dist: list[tuple[float, float]], start: float, end: float) -> Test | None:
    minutes = [_speed(dist, t, t + 60) for t in range(int(start), int(end) - 59, 60)]
    if not minutes or None in minutes:
        return None
    spread = statistics.pstdev(minutes) / statistics.mean(minutes)
    speed, bpm = _speed(dist, end - LAST, end), _mean_hr(hr, end - LAST, end)
    mid = (start + end) / 2
    halves = [(_speed(dist, a, b), _mean_hr(hr, a, b)) for a, b in ((start, mid), (mid, end))]
    if spread > MAX_SPREAD or not speed or not bpm or not all(s and h for s, h in halves):
        return None
    (s1, h1), (s2, h2) = halves
    test = Test(int(start), end - start, _at(dist, end) - _at(dist, start), 1000 / speed, bpm, spread,
                (s1 / h1) / (s2 / h2) - 1)
    # Recovery: from the first moment standing still after the lap press.
    stop = next((r[0] for r in dist if end <= r[0] <= end + STOP_WITHIN
                 and _at(dist, r[0] + 5) - r[1] < STOPPED), None)
    if stop is not None:
        at_stop, after = _near(hr, stop), _near(hr, stop + RECOVERY)
        if at_stop and after and _at(dist, stop + RECOVERY) - _at(dist, stop) <= STILL:
            test.hr_stop, test.hr_after = at_stop, after
    return test


def _near(series: list[tuple[float, int]], t: float, within: float = 3) -> int | None:
    """The last value at or before t, if it's recent: none while the recording was paused."""
    i = bisect_right(series, (t, float("inf"))) - 1
    return series[i][1] if i >= 0 and t - series[i][0] <= within else None


def save(conn: sqlite3.Connection, workout_id: int, test: Test | None) -> None:
    """Store what was found in a workout, keeping a "not a test" mark made by hand."""
    with conn:
        if test is None:
            conn.execute("DELETE FROM pace_tests WHERE workout_id = ?", (workout_id,))
            return
        conn.execute(
            "INSERT INTO pace_tests (workout_id, start, seconds, distance, pace, hr, spread, decoupling, hr_stop, hr_after)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (workout_id) DO UPDATE SET"
            " start = excluded.start, seconds = excluded.seconds, distance = excluded.distance, pace = excluded.pace,"
            " hr = excluded.hr, spread = excluded.spread, decoupling = excluded.decoupling,"
            " hr_stop = excluded.hr_stop, hr_after = excluded.hr_after",
            (workout_id, test.start, test.seconds, test.distance, test.pace, test.hr, test.spread, test.decoupling,
             test.hr_stop, test.hr_after))


def scan(conn: sqlite3.Connection, cfg: Config) -> int:
    """Look for tests in every run's archived FIT file again. Returns how many there are."""
    for row in conn.execute("SELECT id, archive_path FROM workouts WHERE source = 'fit'"
                            " AND COALESCE(sport_override, sport) = 'run'").fetchall():
        try:
            test = find(Path(cfg.archive_dir) / row["archive_path"])
        except Exception:
            test = None
        save(conn, row["id"], test)
    return conn.execute("SELECT COUNT(*) FROM pace_tests").fetchone()[0]


def tests_json(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM pace_tests ORDER BY start"):
        out.append({
            "id": r["workout_id"], "start": datetime.fromtimestamp(r["start"]).isoformat(timespec="minutes"),
            "minutes": round(r["seconds"] / 60, 1), "distance": round(r["distance"]), "pace": round(r["pace"], 2),
            "lap_pace": round(r["seconds"] / r["distance"] * 1000, 2),
            "hr": round(r["hr"], 1), "beats_per_km": round(r["hr"] * r["pace"] / 60),
            "spread": round(r["spread"], 3), "decoupling": round(r["decoupling"], 3),
            "recovery": None if r["hr_after"] is None else r["hr_stop"] - r["hr_after"],
            "hr_stop": r["hr_stop"], "not_test": bool(r["not_test"]),
        })
    return out
