"""Training load from heart rate, and the averages built on it.

All of HeartWatch's numbers come from here, so tuning means changing Config,
never this file.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config

# Heart-rate reserve fractions where zones 1-5 start.
ZONE_STARTS = (0.5, 0.6, 0.7, 0.8, 0.9)
THRESHOLD_FROM = 0.7  # zone 3
ANAEROBIC_FROM = 0.9  # zone 5

OVERTRAINING_BELOW = -20


def reserve(bpm: float, cfg: Config) -> float:
    """Where a heart rate sits between resting (0) and max (1)."""
    return min(1.0, max(0.0, (bpm - cfg.resting_hr) / (cfg.max_hr - cfg.resting_hr)))


def zone(bpm: float, cfg: Config) -> int:
    """0 below zone 1, else 1-5."""
    r = reserve(bpm, cfg)
    return sum(r >= s for s in ZONE_STARTS)


def zone_bounds(cfg: Config) -> list[tuple[int, int]]:
    """bpm range of each zone, for display."""
    hrr = cfg.max_hr - cfg.resting_hr
    edges = [round(cfg.resting_hr + s * hrr) for s in ZONE_STARTS] + [cfg.max_hr]
    return list(zip(edges, edges[1:]))


@dataclass
class Load:
    aerobic: float = 0.0
    threshold: float = 0.0
    anaerobic: float = 0.0

    @property
    def total(self) -> float:
        return self.aerobic + self.threshold + self.anaerobic

    def __iadd__(self, other: Load) -> Load:
        self.aerobic += other.aerobic
        self.threshold += other.threshold
        self.anaerobic += other.anaerobic
        return self


def workout_load(hist: dict[int, float], cfg: Config) -> Load:
    """Banister TRIMP from seconds-per-bpm, split by intensity."""
    out = Load()
    for bpm, seconds in hist.items():
        r = reserve(bpm, cfg)
        trimp = seconds / 60 * r * 0.64 * math.exp(1.92 * r)
        if r >= ANAEROBIC_FROM:
            out.anaerobic += trimp
        elif r >= THRESHOLD_FROM:
            out.threshold += trimp
        else:
            out.aerobic += trimp
    return out


@dataclass
class Day:
    day: date
    load: Load  # that day's workouts
    long_term: Load  # per-dimension long-term load
    short_term: float  # fatigue
    fitness: float
    capacity: float
    change: float  # long-term load vs 28 days earlier, as a fraction
    state: str

    @property
    def ltl(self) -> float:
        return self.long_term.total


def state_of(capacity: float, change: float) -> str:
    if capacity < OVERTRAINING_BELOW:
        return "Overtraining risk"
    if change < -0.08:
        return "Detraining"
    if capacity > 8:
        return "Recovering"
    if capacity < -3 and change > 0.02:
        return "Building"
    return "Maintaining"


def series(daily: dict[date, Load], cfg: Config, start: date, end: date,
           readiness_adjust: dict[date, float] | None = None) -> list[Day]:
    """One Day per date from start to end, with every average starting from zero at start."""
    readiness_adjust = readiness_adjust or {}
    la = lt = ln = stl = 0.0
    fit: float | None = None
    days: list[Day] = []
    d = start
    while d <= end:
        load = daily.get(d, Load())
        la += (load.aerobic - la) / cfg.aerobic_days
        lt += (load.threshold - lt) / cfg.threshold_days
        ln += (load.anaerobic - ln) / cfg.anaerobic_days
        stl += (load.aerobic + cfg.threshold_fatigue * load.threshold
                + cfg.anaerobic_fatigue * load.anaerobic - stl) / cfg.fatigue_days
        ltl = la + lt + ln
        fit = ltl if fit is None else fit + (ltl - fit) / cfg.fitness_days
        capacity = ltl - stl + readiness_adjust.get(d, 0.0)
        prev = days[-28].ltl if len(days) >= 28 else 0.0
        change = (ltl - prev) / prev if prev > 0 else 0.0
        days.append(Day(d, load, Load(la, lt, ln), stl, fit, capacity, change, state_of(capacity, change)))
        d += timedelta(days=1)
    return days


# Readiness from the ring: today's 7-day average of a daily reading against
# your normal range, the average of the 30 days before that week plus or
# minus one standard deviation. (The week itself is left out, so a bad week
# can't drag "normal" down with it.) Two standard deviations on the good side
# adds the full amount to training capacity, on the bad side takes it away.
# Resting heart rate drives it (higher is worse); HRV is charted the same way.
BASE_WEEK = 7
BASE_DAYS = 30
MIN_WEEK = 4       # days with a reading needed in the last 7
MIN_BASELINE = 21  # and in the 30 before that, before any adjustment


@dataclass
class Baseline:
    day: date
    value: float | None      # that day's reading
    week: float | None       # 7-day average
    base: float | None       # average of the 30 days before the week
    sd: float | None
    adjust: float            # added to training capacity
    baseline_days: int       # days with a reading in those 30


def baseline_series(readings: dict[date, float], start: date, end: date,
                    adjust_max: float = 0, higher_is_better: bool = True) -> list[Baseline]:
    out = []
    # Readings by day, from the start of the first day's baseline: slices, not a lookup per day.
    back = BASE_WEEK + BASE_DAYS - 1
    vals = [readings.get(start + timedelta(days=i - back)) for i in range((end - start).days + 1 + back)]
    d = start
    while d <= end:
        j = (d - start).days + back
        week = [v for v in vals[j - BASE_WEEK + 1:j + 1] if v is not None]
        month = [v for v in vals[j - back:j - BASE_WEEK + 1] if v is not None]
        w = sum(week) / len(week) if len(week) >= MIN_WEEK else None
        base = sd = None
        adjust = 0.0
        if len(month) >= 2:
            base = sum(month) / len(month)
            sd = math.sqrt(sum((x - base) ** 2 for x in month) / (len(month) - 1))
        if w is not None and sd and len(month) >= MIN_BASELINE:
            z = (w - base) / sd * (1 if higher_is_better else -1)
            adjust = max(-adjust_max, min(adjust_max, z * adjust_max / 2))
        out.append(Baseline(d, readings.get(d), w, base, sd, adjust, len(month)))
        d += timedelta(days=1)
    return out


def readiness(rhr: dict[date, float], cfg: Config, start: date, end: date) -> list[Baseline]:
    """Resting heart rate above your normal lowers training capacity; below it raises it."""
    return baseline_series(rhr, start, end, cfg.readiness_adjust_max, higher_is_better=False)
