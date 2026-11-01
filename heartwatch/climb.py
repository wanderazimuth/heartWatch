"""Climbs: sustained ascents on foot, to see climbing fitness change.

Any recording with altitude counts (a hike, a run, the weekly uphill session):
the Pixel's barometer gives the Wahoo app a clean altitude even where a canyon
blocks GPS. HeartWatch finds every stretch that kept going up, through short
stops and small dips, and measures:

- rate, metres climbed per hour, stops included: what the group sees;
- average and peak heart rate;
- beats per 100 m, the heartbeats above resting it took to climb 100 m. The
  energy cost of lifting yourself a metre hardly depends on how fast you go,
  so a climb done a little faster or slower than the last is still a fair
  comparison, as beats per km is for a pace test (pacetest.py).

A pack makes every metre cost more, so each climb can carry a pack weight,
entered by hand or taken from a trip on the Plan tab that day (events.py).
Rides don't count: anything climbing faster than legs can, or moving at car
speed, and anything after a car ride at the end of the recording (fit.find_drive).
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import fitdecode

from .config import Config

STEP = 10  # seconds between the altitude points climbs are found on
SMOOTH = 30  # seconds of altitude averaged into each point
GAP = 60  # seconds without altitude: the recording was paused, so a climb ends
START_RISE = 10  # metres above the lowest point before it counts as climbing
DIP = 15  # metres below the top that end a climb
STALL = 5 * 60  # seconds without a rise of STALL_RISE end it: a long stop
STALL_RISE = 3  # metres
TRIM = 2  # metres: the ends move in to where the climbing really started and stopped
MIN_GAIN = 60  # metres
MIN_TIME = 8 * 60  # seconds
MAX_RATE = 1500  # metres per hour; faster is a lift or a car, not legs
MAX_SPEED = 3.0  # m/s, median over the climb; walking uphill is well under it
MIN_HR = 0.8  # of the climb's seconds with a heart-rate reading


@dataclass
class Climb:
    start: int  # unix seconds
    seconds: float
    gain: float  # metres
    hr: float  # average
    peak: int

    @property
    def rate(self) -> float:
        return self.gain / self.seconds * 3600


def find(source: str | Path | BinaryIO, until: float | None = None) -> list[Climb]:
    """Every climb in a FIT file, oldest first. until: unix seconds where a car ride began."""
    alt, hr, speed = read(source)
    if until is not None:
        alt = [p for p in alt if p[0] < until]
    out = []
    for a, b, gain in _ascents(_points(alt)):
        c = _measure(hr, speed, a, b, gain)
        if c:
            out.append(c)
    return out


def read(source: str | Path | BinaryIO) -> tuple[list[tuple[float, float]], list[tuple[float, int]], list[tuple[float, float]]]:
    """Altitude (metres), heart rate and speed (m/s), by unix time."""
    alt: list[tuple[float, float]] = []
    hr: list[tuple[float, int]] = []
    speed: list[tuple[float, float]] = []
    with fitdecode.FitReader(source) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage) or frame.name != "record":
                continue
            ts = frame.get_value("timestamp", fallback=None)
            if ts is None:
                continue
            t = ts.timestamp()
            a = frame.get_value("enhanced_altitude", fallback=None)
            if a is None:
                a = frame.get_value("altitude", fallback=None)
            bpm = frame.get_value("heart_rate", fallback=None)
            v = frame.get_value("enhanced_speed", fallback=None)
            if v is None:
                v = frame.get_value("speed", fallback=None)
            if a is not None:
                alt.append((t, float(a)))
            if bpm:
                hr.append((t, int(bpm)))
            if v is not None:
                speed.append((t, float(v)))
    alt.sort()
    hr.sort()
    speed.sort()
    return alt, hr, speed


def _points(alt: list[tuple[float, float]]) -> list[tuple[float, float] | None]:
    """Smoothed altitude every STEP seconds, with None where the recording had a gap."""
    if not alt:
        return []
    times = [t for t, _ in alt]
    out: list[tuple[float, float] | None] = []
    t = alt[0][0]
    while t <= alt[-1][0]:
        lo, hi = bisect_left(times, t - SMOOTH / 2), bisect_left(times, t + SMOOTH / 2)
        near = [a for _, a in alt[lo:hi]]
        out.append((t, sum(near) / len(near)) if near else None)
        t += STEP
    # A gap longer than GAP is a break, however it's smoothed over.
    for i, p in enumerate(out):
        if p is not None:
            j = bisect_left(times, p[0])
            prev = times[j - 1] if j else p[0]
            nxt = times[j] if j < len(times) else p[0]
            if nxt - prev > GAP:
                out[i] = None
    return out


def _ascents(points: list[tuple[float, float] | None]) -> list[tuple[float, float, float]]:
    """(start, end, metres gained) of each stretch that kept climbing, before the size and speed checks."""
    out = []
    run: list[tuple[float, float]] = []  # points since the last climb ended or the last gap

    def close(top: int) -> None:
        low = min(range(top + 1), key=lambda i: (run[i][1], -i))  # the last time at the lowest point
        floor, peak = run[low][1], run[top][1]
        a = max(i for i in range(low, top + 1) if run[i][1] <= floor + TRIM)
        b = min(i for i in range(a, top + 1) if run[i][1] >= peak - TRIM)
        out.append((run[a][0], run[b][0], run[b][1] - run[a][1]))

    top = None  # index in run of the highest point of the climb under way
    lowest = None
    for p in points + [None]:
        if p is None:
            if top is not None:
                close(top)
            run, top, lowest = [], None, None
            continue
        run.append(p)
        lowest = p[1] if lowest is None else min(lowest, p[1])
        if top is None:
            if p[1] - lowest >= START_RISE:
                top = len(run) - 1
        elif p[1] > run[top][1]:
            top = len(run) - 1
        elif run[top][1] - p[1] > DIP or _stalled(run):
            close(top)
            run, top = run[top:], None
            lowest = min(a for _, a in run)
    return out


def _stalled(run: list[tuple[float, float]]) -> bool:
    """Whether nowhere in the last STALL seconds of points rose STALL_RISE: standing, or drifting down."""
    now = run[-1][0]
    if now - run[0][0] < STALL:
        return False
    low, rise = float("inf"), 0.0
    for t, a in run[-(STALL // STEP + 1):]:
        if t >= now - STALL:
            low = min(low, a)
            rise = max(rise, a - low)
    return rise < STALL_RISE


def _measure(hr: list[tuple[float, int]], speed: list[tuple[float, float]], a: float, b: float,
             gain: float) -> Climb | None:
    seconds = b - a
    if gain < MIN_GAIN or seconds < MIN_TIME or gain / seconds * 3600 > MAX_RATE:
        return None
    moving = sorted(v for t, v in speed if a <= t <= b)
    if moving and moving[len(moving) // 2] > MAX_SPEED:
        return None
    beats = [v for t, v in hr if a <= t <= b]
    if len(beats) < MIN_HR * seconds:
        return None
    return Climb(int(a), seconds, gain, sum(beats) / len(beats), max(beats))


def beats_per_100m(hr: float, seconds: float, gain: float, resting_hr: float) -> float:
    """Heartbeats above resting it took to climb 100 m."""
    return (hr - resting_hr) * seconds / 60 / gain * 100


def save(conn: sqlite3.Connection, workout_id: int, climbs: list[Climb]) -> None:
    """Store a workout's climbs, keeping the pack weights and "not a climb" marks made by hand."""
    with conn:
        starts = [c.start for c in climbs]
        conn.execute(f"DELETE FROM climbs WHERE workout_id = ? AND start NOT IN ({','.join('?' * len(starts))})",
                     (workout_id, *starts))
        for c in climbs:
            conn.execute(
                "INSERT INTO climbs (workout_id, start, seconds, gain, hr, peak) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (workout_id, start) DO UPDATE SET"
                " seconds = excluded.seconds, gain = excluded.gain, hr = excluded.hr, peak = excluded.peak",
                (workout_id, c.start, c.seconds, c.gain, c.hr, c.peak))


def scan(conn: sqlite3.Connection, cfg: Config) -> int:
    """Look for climbs in every archived FIT file again. Returns how many there are."""
    for row in conn.execute("SELECT id, archive_path, drive_at, keep_drive FROM workouts WHERE source = 'fit'").fetchall():
        until = None if row["keep_drive"] else row["drive_at"]
        try:
            climbs = find(Path(cfg.archive_dir) / row["archive_path"], until)
        except Exception:
            climbs = []
        save(conn, row["id"], climbs)
    return conn.execute("SELECT COUNT(*) FROM climbs").fetchone()[0]


def climbs_json(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Every climb, oldest first. A climb without its own pack weight takes that day's trip's."""
    packs = {r["day"]: r["pack_lb"] for r in conn.execute(
        "SELECT day, pack_lb FROM events WHERE pack_lb IS NOT NULL ORDER BY updated")}
    out = []
    for r in conn.execute("SELECT * FROM climbs ORDER BY start"):
        start = datetime.fromtimestamp(r["start"])
        pack, from_event = r["pack_lb"], False
        if pack is None and start.date().isoformat() in packs:
            pack, from_event = packs[start.date().isoformat()], True
        out.append({
            "id": r["id"], "workout_id": r["workout_id"], "start": start.isoformat(timespec="minutes"),
            "minutes": round(r["seconds"] / 60, 1), "gain": round(r["gain"]),
            "rate": round(r["gain"] / r["seconds"] * 3600), "hr": round(r["hr"]), "peak": r["peak"],
            "beats_per_100m": round(beats_per_100m(r["hr"], r["seconds"], r["gain"], cfg.resting_hr)),
            "pack_lb": pack, "pack_from_event": from_event, "not_climb": bool(r["not_climb"]),
        })
    return out


def update(conn: sqlite3.Connection, climb_id: int, values: dict) -> bool:
    """Set a climb's pack weight (lb, or None for none entered) and "not a climb" mark. False if there's no such climb."""
    sets, args = [], []
    if "pack_lb" in values:
        pack = values["pack_lb"]
        if pack is not None and (isinstance(pack, bool) or not isinstance(pack, (int, float)) or not 0 <= pack <= 150):
            raise ValueError("pack_lb must be a number from 0 to 150, or null")
        sets.append("pack_lb = ?")
        args.append(pack)
    if "not_climb" in values:
        if not isinstance(values["not_climb"], bool):
            raise ValueError("not_climb must be true or false")
        sets.append("not_climb = ?")
        args.append(int(values["not_climb"]))
    if not sets:
        raise ValueError("send pack_lb or not_climb")
    with conn:
        return conn.execute(f"UPDATE climbs SET {', '.join(sets)} WHERE id = ?", (*args, climb_id)).rowcount > 0
