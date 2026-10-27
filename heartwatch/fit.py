"""Read workouts from Garmin FIT files, the format the Wahoo app exports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO

import fitdecode

# FIT sport names -> HeartWatch workout types.
SPORTS = {"running": "run", "training": "strength", "fitness_equipment": "strength",
          "hiking": "hike", "walking": "hike"}


@dataclass
class Recording:
    start: datetime
    end: datetime
    sport: str  # "run", "strength", "hike" or "other"
    fit_sport: str  # what the file said, for the record
    hr: list[tuple[float, int]] = field(default_factory=list)  # (seconds from start, bpm)
    drive: float | None = None  # seconds from start when a car ride at the end began (find_drive)


def read(source: str | BinaryIO) -> Recording:
    sport = sub_sport = None
    samples: list[tuple[datetime, int]] = []
    speeds: list[tuple[datetime, float]] = []
    with fitdecode.FitReader(source) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name in ("session", "sport") and sport is None:
                sport = frame.get_value("sport", fallback=None)
                sub_sport = frame.get_value("sub_sport", fallback=None)
            elif frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                bpm = frame.get_value("heart_rate", fallback=None)
                if ts is not None and bpm:
                    samples.append((ts, int(bpm)))
                speed = frame.get_value("enhanced_speed", fallback=None)
                if speed is None:
                    speed = frame.get_value("speed", fallback=None)
                if ts is not None and speed is not None:
                    speeds.append((ts, float(speed)))
    if len(samples) < 2:
        raise ValueError("no heart-rate samples")
    samples.sort()
    start, end = samples[0][0], samples[-1][0]
    fit_sport = f"{sport}/{sub_sport}" if sub_sport not in (None, "generic") else str(sport)
    kind = SPORTS.get(str(sport), "other")
    if sub_sport == "strength_training":
        kind = "strength"
    hr = clean([((ts - start).total_seconds(), bpm) for ts, bpm in samples])
    if len(hr) < 2:
        raise ValueError("too short to use")
    drive = find_drive(sorted(((ts - start).total_seconds(), v) for ts, v in speeds))
    return Recording(start, end, kind, fit_sport, hr, drive)


DRIVE_SPEED = 7.0   # m/s, 25 km/h: nobody on foot holds this for half a minute
DRIVE_HOLD = 30     # seconds it has to be held
DRIVE_MEAN = 4.0    # m/s from there to the end of the recording, stops at lights and all
STOPPED = 0.5       # m/s: standing at the car
LOOK_BACK = 300     # how far back from the drive to look for that standstill


def find_drive(speed: list[tuple[float, float]]) -> float | None:
    """When a recording left running in the car stopped being a workout, or None.

    A drive is speed above DRIVE_SPEED for DRIVE_HOLD seconds that lasts to the end
    of the recording (only a drive at the end: the recording was ended late). The cut
    is at the last standstill before it, so pulling out of the parking lot goes too.
    """
    n = len(speed)
    after = [0.0] * (n + 1)  # sums of speed from each sample to the end
    for i in range(n - 1, -1, -1):
        after[i] = after[i + 1] + speed[i][1]
    for i, (t, v) in enumerate(speed):
        if v <= DRIVE_SPEED:
            continue
        held = [x for u, x in speed[i:i + DRIVE_HOLD * 4] if u - t <= DRIVE_HOLD]
        if speed[i + len(held) - 1][0] - t < DRIVE_HOLD * 0.8 or sum(held) / len(held) <= DRIVE_SPEED:
            continue
        if after[i] / (n - i) < DRIVE_MEAN:
            continue  # it went back to walking pace: a fast stretch in a workout, not the ride home
        for u, x in reversed(speed[:i]):
            if t - u > LOOK_BACK:
                break
            if x < STOPPED:
                return u
        return t
    return None


SETTLE_SECONDS = 30  # a strap reads nonsense while its contacts settle
MAX_JUMP = 10  # bpm per second; a heart can't change faster than this


def clean(hr: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Drop the settling period, and samples that jump implausibly from the last good one.

    The allowed jump grows with the time since that sample, so a real change
    after a dropout is accepted rather than locked out.
    """
    out: list[tuple[float, int]] = []
    for t, bpm in hr:
        if t < SETTLE_SECONDS:
            continue
        if out and abs(bpm - out[-1][1]) > MAX_JUMP * max(1.0, t - out[-1][0]):
            continue
        out.append((t, bpm))
    return out


def histogram(hr: list[tuple[float, int]], max_gap: float = 5.0) -> dict[int, float]:
    # Times may be relative or absolute; only differences matter.
    """Seconds spent at each bpm.

    Each sample holds until the next one, but never for more than max_gap
    seconds, so a strap dropout or a paused recording doesn't count as time.
    """
    out: dict[int, float] = {}
    for (t1, bpm), (t2, _) in zip(hr, hr[1:]):
        dt = min(t2 - t1, max_gap)
        if dt > 0:
            out[bpm] = out.get(bpm, 0.0) + dt
    return out
