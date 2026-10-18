"""Read workouts from Garmin FIT files, the format the Wahoo app exports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO

import fitdecode

# FIT sport names -> HeartWatch workout types.
SPORTS = {"running": "run", "training": "strength", "fitness_equipment": "strength"}


@dataclass
class Recording:
    start: datetime
    end: datetime
    sport: str  # "run", "strength" or "other"
    fit_sport: str  # what the file said, for the record
    hr: list[tuple[float, int]] = field(default_factory=list)  # (seconds from start, bpm)


def read(source: str | BinaryIO) -> Recording:
    sport = sub_sport = None
    samples: list[tuple[datetime, int]] = []
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
    return Recording(start, end, kind, fit_sport, hr)


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
