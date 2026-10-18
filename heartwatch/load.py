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
           hrv_adjust: dict[date, float] | None = None) -> list[Day]:
    """One Day per date from start to end, with every average starting from zero at start."""
    hrv_adjust = hrv_adjust or {}
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
        capacity = ltl - stl + hrv_adjust.get(d, 0.0)
        prev = days[-28].ltl if len(days) >= 28 else 0.0
        change = (ltl - prev) / prev if prev > 0 else 0.0
        days.append(Day(d, load, Load(la, lt, ln), stl, fit, capacity, change, state_of(capacity, change)))
        d += timedelta(days=1)
    return days
