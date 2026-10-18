"""Backfill from a Google Takeout of Google Fit.

The Wahoo app wrote its heart rate to Google Fit at one reading per second,
but Takeout keeps it as one long stream per source, not per workout. This
splits the stream into sessions wherever it pauses, and labels each from the
Google Fit activity (Strength, Running, ...) that overlaps it most. The
activity files themselves have no heart rate.
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HR_PREFIX = "Takeout/Fit/All Data/raw_com.google.heart_rate.bpm_com.wahoofitness.fitness_"
ACTIVITY_DIR = "Takeout/Fit/Activities/"

SESSION_GAP = 600  # seconds without a reading that end a session
MIN_SESSION = 300  # shorter sessions are strap fiddling, not workouts

ACTIVITY_NAME = re.compile(
    r"(?P<start>\d{4}-\d\d-\d\dT\d\d_\d\d_\d\d(?:\.\d+)?)(?P<tz>[+-]\d\d_\d\d)_(?P<dur>PT[0-9HMS.]+)_(?P<types>[^/]*)\.tcx$")
DURATION = re.compile(r"PT(?:(?P<h>[\d.]+)H)?(?:(?P<m>[\d.]+)M)?(?:(?P<s>[\d.]+)S)?$")


@dataclass
class Activity:
    start: float  # unix seconds
    end: float
    types: list[str]

    @property
    def sport(self) -> str:
        # Google Fit names vary ("Strength training", "Treadmill running", "Walking,Running").
        names = ",".join(self.types).lower()
        if "strength" in names or "calisthenics" in names:
            return "strength"
        if "running" in names or "treadmill" in names:
            return "run"
        return "other"


@dataclass
class Session:
    samples: list[tuple[float, int]]  # (unix seconds, bpm)
    sport: str = "other"
    label: str = ""  # the Google Fit activity it was named from

    @property
    def start(self) -> float:
        return self.samples[0][0]

    @property
    def end(self) -> float:
        return self.samples[-1][0]


def heart_rate(zips: list[Path]) -> list[tuple[float, int]]:
    """Every Wahoo heart-rate reading in the Takeout zips, one per second at most."""
    by_time: dict[float, int] = {}
    for path in zips:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.startswith(HR_PREFIX) and name.endswith(".json"):
                    for p in json.load(zf.open(name)).get("Data Points", []):
                        bpm = p["fitValue"][0]["value"].get("fpVal")
                        if bpm:
                            by_time[int(p["startTimeNanos"]) // 10**9] = round(bpm)
    return sorted(by_time.items())


def activities(zips: list[Path]) -> list[Activity]:
    out = []
    for path in zips:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith(ACTIVITY_DIR):
                    continue
                m = ACTIVITY_NAME.search(name)
                d = m and DURATION.match(m["dur"])
                if not m or not d or not m["types"]:
                    continue
                sign = 1 if m["tz"][0] == "+" else -1
                hours, minutes = m["tz"][1:].split("_")
                tz = timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
                start = datetime.strptime(m["start"].split(".")[0], "%Y-%m-%dT%H_%M_%S").replace(tzinfo=tz).timestamp()
                seconds = sum(float(d[k] or 0) * f for k, f in (("h", 3600), ("m", 60), ("s", 1)))
                out.append(Activity(start, start + seconds, m["types"].split(",")))
    return sorted(out, key=lambda a: a.start)


def sessions(samples: list[tuple[float, int]], acts: list[Activity] = ()) -> list[Session]:
    groups: list[list[tuple[float, int]]] = []
    for s in samples:
        if groups and s[0] - groups[-1][-1][0] <= SESSION_GAP:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = [Session(g) for g in groups if g[-1][0] - g[0][0] >= MIN_SESSION]
    for session in out:
        label(session, acts)
    return out


def label(session: Session, acts: list[Activity]) -> None:
    """Name the session after the typed activity overlapping it most.

    Phones log walks all day, so an activity that knows the workout (strength,
    run) beats one that doesn't, however much the walk overlaps.
    """
    best = None
    for a in acts:
        overlap = min(a.end, session.end) - max(a.start, session.start)
        if overlap <= 0:
            continue
        key = (a.sport != "other", overlap)
        if best is None or key > best[0]:
            best = (key, a)
    if best:
        session.sport = best[1].sport
        session.label = ",".join(best[1].types)
