"""Steps and brisk walking outside workouts, from Health Connect step counts.

Kept apart from training load on purpose: the load model was built for
training sessions, and a few minutes of fast walking barely moves it. This is
the everyday movement that adds up for health anyway.

Several apps write steps from the same walking, so each day uses one source:
the phone's own step counter (its records are seconds to a minute long, fine
enough to see a two-minute walk between buildings), and on days the phone
recorded nothing, the ring's half-hour totals for steps alone. Adding sources
up would count the walking two or three times, and the Wahoo app's step
records are nonsense (a million steps in a run), so those are never used.

Brisk walking is any 60 seconds at 100 or more steps a minute, the cadence
research uses for moderate intensity, outside a workout recorded on the
strap. Stairs mostly don't count: climbing is hard work at a slow step rate.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .store import Workout

# The phone's own counters, best first: Health Connect's built-in one, then Google Fit.
PHONE_SOURCES = ("android.healthconnect.phone", "google.android.apps.fitness")
RING_SOURCE = "ringconn"
BRISK_STEPS = 100  # in a minute
BUCKET = 15  # seconds; four make the rolling minute
GAP = 60  # seconds of slower walking that still continue a burst
WORKOUT_MARGIN = timedelta(minutes=5)  # warm-up and cool-down around a strap recording


@dataclass
class Movement:
    steps: int
    brisk_minutes: float | None  # None when the day has only the ring's coarse totals
    bursts: int | None
    source: str  # "phone" or "ring"


def daily(conn: sqlite3.Connection, workouts: list[Workout]) -> dict[date, Movement]:
    phone: dict[date, dict[int, list[tuple[int, int, float]]]] = {}
    ring: dict[date, float] = {}
    for r in conn.execute("SELECT start, end, value, origin FROM health_records WHERE type = 'steps' ORDER BY start"):
        d = datetime.fromtimestamp(r["start"]).date()
        rank = next((i for i, s in enumerate(PHONE_SOURCES) if s in r["origin"]), None)
        if rank is not None:
            phone.setdefault(d, {}).setdefault(rank, []).append((r["start"], r["end"], r["value"]))
        elif RING_SOURCE in r["origin"].lower():
            ring[d] = ring.get(d, 0) + r["value"]
    out = {d: Movement(round(v), None, None, "ring") for d, v in ring.items()}
    for d, by_rank in phone.items():
        rows = by_rank[min(by_rank)]
        brisk, bursts = _brisk(rows, _workout_spans(workouts, d))
        out[d] = Movement(round(sum(c for _, _, c in rows)), brisk, bursts, "phone")
    return out


def _workout_spans(workouts: list[Workout], d: date) -> list[tuple[float, float]]:
    return [((w.start - WORKOUT_MARGIN).timestamp(), (w.end + WORKOUT_MARGIN).timestamp())
            for w in workouts if w.start.date() <= d <= w.end.date()]


def _brisk(rows: list[tuple[int, int, float]], skip: list[tuple[float, float]]) -> tuple[float, int]:
    """Minutes of brisk walking and how many separate bursts, from one source's records."""
    buckets: dict[int, float] = {}
    for start, end, count in rows:
        if end <= start:
            buckets[start // BUCKET] = buckets.get(start // BUCKET, 0) + count
            continue
        # Spread the count evenly over the record's time, bucket by bucket.
        t = start
        while t < end:
            nxt = min(end, (t // BUCKET + 1) * BUCKET)
            buckets[t // BUCKET] = buckets.get(t // BUCKET, 0) + count * (nxt - t) / (end - start)
            t = nxt
    per_minute = 60 // BUCKET
    brisk: set[int] = set()
    for b in buckets:
        window = range(b, b + per_minute)
        if sum(buckets.get(i, 0) for i in window) >= BRISK_STEPS and not any(
                lo <= b * BUCKET < hi for lo, hi in skip):
            brisk.update(window)
    bursts, last = 0, None
    for b in sorted(brisk):
        if last is None or (b - last) * BUCKET > GAP:
            bursts += 1
        last = b
    return len(brisk) * BUCKET / 60, bursts
