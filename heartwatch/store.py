"""Importing workouts into the database and reading them back."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from . import fit, pacetest, ringconn, takeout, thresholdtest
from .config import Config
from .load import Load, workout_load, zone

log = logging.getLogger("heartwatch")


@dataclass
class Imported:
    name: str
    status: str  # "added", "duplicate" or "failed: <why>"
    recording: fit.Recording | None = None


def fit_files(paths: list[Path]) -> Iterator[tuple[str, bytes]]:
    """(name, contents) of every .fit file in the given files, folders and zips."""
    for path in paths:
        if path.is_dir():
            yield from fit_files(sorted(path.rglob("*")))
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                for name in sorted(zf.namelist()):
                    if name.lower().endswith(".fit"):
                        yield Path(name).name, zf.read(name)
        elif path.suffix.lower() == ".fit":
            yield path.name, path.read_bytes()


def import_fit(conn: sqlite3.Connection, cfg: Config, name: str, data: bytes) -> Imported:
    ref = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM workouts WHERE source_ref = ?", (ref,)).fetchone():
        return Imported(name, "duplicate")
    try:
        rec = fit.read(io.BytesIO(data))
    except Exception as e:
        return Imported(name, f"failed: {e}")
    dest = Path(cfg.archive_dir) / f"{rec.start:%Y}" / name
    if dest.exists() and dest.read_bytes() != data:
        dest = dest.with_name(f"{dest.stem}-{ref[:8]}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    rel = dest.relative_to(cfg.archive_dir).as_posix()
    start, end = int(rec.start.timestamp()), int(rec.end.timestamp())
    with conn:
        cur = conn.execute(
            "INSERT INTO workouts (start, end, sport, source, source_ref, archive_path, detail, peak_hr, imported)"
            " VALUES (?, ?, ?, 'fit', ?, ?, ?, ?, ?)",
            (start, end, rec.sport, ref, rel, rec.fit_sport, max(b for _, b in rec.hr), int(time.time())))
        rebuild(conn, cfg, overlapping(conn, start, end))
    if rec.sport == "run":
        for kind, mod in (("pace", pacetest), ("threshold", thresholdtest)):
            try:
                mod.save(conn, cur.lastrowid, mod.find(io.BytesIO(data)))
            except Exception as e:  # a test that can't be measured mustn't lose the workout
                log.warning("%s test in %s: %s", kind, name, e)
    return Imported(name, "added", rec)


# A Takeout session mostly covered by .FIT files is the same workout again.
COVERED = 0.8


def import_takeout(conn: sqlite3.Connection, cfg: Config, session: takeout.Session) -> Imported:
    start, end = int(session.start), int(session.end)
    name = f"{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} {session.label or 'unlabelled'}"
    ref = "takeout:" + hashlib.sha256(json.dumps(session.samples).encode()).hexdigest()
    if conn.execute("SELECT 1 FROM workouts WHERE source_ref = ?", (ref,)).fetchone():
        return Imported(name, "duplicate")
    covered = conn.execute(
        "SELECT COALESCE(SUM(MIN(end, ?) - MAX(start, ?)), 0) FROM workouts WHERE source = 'fit' AND start < ? AND end > ?",
        (end, start, end, start)).fetchone()[0]
    if covered >= COVERED * (end - start):
        return Imported(name, "duplicate")
    hr = fit.clean([(t - start, bpm) for t, bpm in session.samples])
    if len(hr) < 2:
        return Imported(name, "failed: too short to use")
    dest = Path(cfg.archive_dir) / "takeout" / f"{datetime.fromtimestamp(start):%Y}" / f"{start}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wt") as f:
        json.dump({"sport": session.sport, "label": session.label, "samples": session.samples}, f)
    with conn:
        conn.execute(
            "INSERT INTO workouts (start, end, sport, source, source_ref, archive_path, detail, peak_hr, imported)"
            " VALUES (?, ?, ?, 'takeout', ?, ?, ?, ?, ?)",
            (start, end, session.sport, ref, dest.relative_to(cfg.archive_dir).as_posix(), session.label,
             max(b for _, b in hr), int(time.time())))
        rebuild(conn, cfg, overlapping(conn, start, end))
    return Imported(name, "added")


def import_ringconn(conn: sqlite3.Connection, cfg: Config, name: str, data: bytes) -> list[ringconn.Vitals]:
    """Store a RingConn export's days (newer exports win) and archive the file. Raises ValueError."""
    days = ringconn.read(name, data)
    if not days:
        raise ValueError("no days with ring data in this file")
    name = Path(name).name
    dest = Path(cfg.archive_dir) / "ringconn" / name
    if dest.exists() and dest.read_bytes() != data:
        dest = dest.with_name(f"{dest.stem}-{hashlib.sha256(data).hexdigest()[:8]}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    now = int(time.time())
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO vitals (day, avg_hr, min_hr, max_hr, spo2, hrv, hrv_min, hrv_max, imported)"
            " VALUES (:day, :avg_hr, :min_hr, :max_hr, :spo2, :hrv, :hrv_min, :hrv_max, :imported)",
            [{"day": v.day.isoformat(), **v.values(), "imported": now} for v in days])
    return days


def vitals(conn: sqlite3.Connection) -> dict[date, sqlite3.Row]:
    return {date.fromisoformat(r["day"]): r for r in conn.execute("SELECT * FROM vitals ORDER BY day")}


def hrv_readings(conn: sqlite3.Connection) -> dict[date, float]:
    return {d: r["hrv"] for d, r in vitals(conn).items() if r["hrv"] is not None}


def samples(cfg: Config, row: sqlite3.Row) -> list[tuple[float, int]]:
    """A workout's cleaned heart rate as (unix seconds, bpm), read back from the archive."""
    # archive_path is relative to archive_dir, so the archive can move between machines.
    path = Path(cfg.archive_dir) / row["archive_path"]
    if row["source"] == "takeout":
        with gzip.open(path, "rt") as f:
            raw = json.load(f)["samples"]
        t0 = raw[0][0]
        return [(t0 + t, bpm) for t, bpm in fit.clean([(t - t0, bpm) for t, bpm in raw])]
    rec = fit.read(path)
    t0 = rec.start.timestamp()
    return [(t0 + t, bpm) for t, bpm in rec.hr]


# The TICKR X records on its own whenever it's worn, so most app recordings
# sit inside a longer strap recording of the same heartbeats, and Takeout has
# the app's heart rate a second time. Each second is counted once, by the best
# recording of it: a .FIT file over Takeout, a typed workout (run, strength)
# over an untyped one, then the shorter over the longer.
def _priority(row: sqlite3.Row) -> tuple:
    sport = row["sport_override"] or row["sport"]
    return (row["source"] != "fit", sport == "other", row["end"] - row["start"], row["id"])


def overlapping(conn: sqlite3.Connection, start: int, end: int) -> list[int]:
    """Ids of every workout linked to [start, end] through a chain of overlaps."""
    ids: set[int] = set()
    lo, hi = start, end
    while True:
        rows = conn.execute("SELECT id, start, end FROM workouts WHERE start < ? AND end > ?", (hi, lo)).fetchall()
        if {r["id"] for r in rows} <= ids:
            return sorted(ids)
        ids |= {r["id"] for r in rows}
        lo, hi = min([lo] + [r["start"] for r in rows]), max([hi] + [r["end"] for r in rows])


def rebuild(conn: sqlite3.Connection, cfg: Config, ids: list[int]) -> None:
    """Recompute the histograms of these workouts from their archived files."""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = sorted(conn.execute(f"SELECT * FROM workouts WHERE id IN ({marks})", ids).fetchall(), key=_priority)
    claimed: list[tuple[int, int]] = []
    with conn:
        for row in rows:
            own = [(t, bpm) for t, bpm in samples(cfg, row) if not any(s <= t <= e for s, e in claimed)]
            conn.execute("DELETE FROM hr_hist WHERE workout_id = ?", (row["id"],))
            conn.executemany("INSERT INTO hr_hist (workout_id, bpm, seconds) VALUES (?, ?, ?)",
                             [(row["id"], bpm, s) for bpm, s in fit.histogram(own).items()])
            claimed.append((row["start"], row["end"]))


def rebuild_all(conn: sqlite3.Connection, cfg: Config) -> int:
    done: set[int] = set()
    for row in conn.execute("SELECT id, start, end FROM workouts ORDER BY start").fetchall():
        if row["id"] not in done:
            group = overlapping(conn, row["start"], row["end"])
            rebuild(conn, cfg, group)
            done |= set(group)
    return len(done)


@dataclass
class Workout:
    id: int
    start: datetime  # local time
    end: datetime
    sport: str
    peak_hr: int
    hist: dict[int, float]  # only the seconds this workout owns; see rebuild()

    @property
    def minutes(self) -> float:
        return sum(self.hist.values()) / 60

    def active_minutes(self, cfg: Config) -> float:
        """Minutes in zone 1 or above, so a strap left on at rest isn't a workout."""
        return sum(s for b, s in self.hist.items() if zone(b, cfg) >= 1) / 60

    @property
    def avg_hr(self) -> float:
        total = sum(self.hist.values())
        return sum(b * s for b, s in self.hist.items()) / total if total else 0.0

    @property
    def max_hr(self) -> int:
        return max(self.hist, default=0)


MIN_ACTIVE_MINUTES = 5


def is_workout(w: Workout, cfg: Config) -> bool:
    """Leftovers of a strap recording (warm-up, strap on at rest) aren't workouts of their own."""
    return w.active_minutes(cfg) >= MIN_ACTIVE_MINUTES


def workouts(conn: sqlite3.Connection) -> list[Workout]:
    hists: dict[int, dict[int, float]] = {}
    for row in conn.execute("SELECT workout_id, bpm, seconds FROM hr_hist"):
        hists.setdefault(row["workout_id"], {})[row["bpm"]] = row["seconds"]
    return [
        Workout(row["id"], datetime.fromtimestamp(row["start"]), datetime.fromtimestamp(row["end"]),
                row["sport_override"] or row["sport"], row["peak_hr"] or 0, hists.get(row["id"], {}))
        for row in conn.execute("SELECT * FROM workouts ORDER BY start")
    ]


def daily_loads(ws: list[Workout], cfg: Config) -> dict[date, Load]:
    out: dict[date, Load] = {}
    for w in ws:
        out.setdefault(w.start.date(), Load())
        out[w.start.date()] += workout_load(w.hist, cfg)
    return out
