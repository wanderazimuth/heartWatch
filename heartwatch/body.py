"""Weight and body fat: from the Aria scale (aria.py) and from old exports.

Exports are full of weights that were never weighed: the Wahoo app writes your profile
weight with every workout, and so do a few others. Only apps you weighed yourself into
are read: Monitor Your Weight, MyFitnessPal and Google Fit's own entries from Takeout,
and Runkeeper's measurements (weight and body fat) from its export. The readings taken
from an export are kept under archive/body, so `heartwatch rebuild` can read them again.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import aria
from .config import Config

TAKEOUT_PREFIX = "Takeout/Fit/All Data/raw_com.google."
# Apps whose weights in Google Fit were weighed, not copied from a profile.
TAKEOUT_APPS = {"monitoryourweight.bustan.net": "monitoryourweight", "com.myfitnesspal.android": "myfitnesspal",
                "com.google.android.apps.fitness_user_input": "googlefit"}
TREND_DAYS = 14  # the average line: weigh-ins over the last two weeks

Row = tuple[int, int | None, float | None]  # unix seconds, grams, body fat %


def store(conn: sqlite3.Connection, source: str, rows: list[Row], person: str = "me") -> int:
    """Readings from one source; one already there is skipped. Returns how many were new."""
    now = int(time.time())
    with conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO body (taken, weight_g, fat_pct, source, person, imported) VALUES (?, ?, ?, ?, ?, ?)",
                         [(t, g, f, source, person, now) for t, g, f in rows if g])
        return conn.total_changes - before


def keep(cfg: Config, source: str, rows: list[Row]) -> None:
    """Add an export's readings to archive/body/<source>.json."""
    path = Path(cfg.archive_dir) / "body" / f"{source}.json"
    old = {tuple(r) for r in json.loads(path.read_text())} if path.exists() else set()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(old | {tuple(r) for r in rows})))


def takeout_rows(zips: list[Path]) -> dict[str, list[Row]]:
    """Weighed-in weights and body fat from Google Fit Takeout zips, by app."""
    weights: dict[str, dict[int, float]] = {}
    fats: dict[str, dict[int, float]] = {}
    for path in zips:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith(TAKEOUT_PREFIX) or not name.endswith(".json"):
                    continue
                kind = name[len(TAKEOUT_PREFIX):]
                into = weights if kind.startswith("weight_") else fats if kind.startswith("body.fat.percentage_") else None
                app = next((v for k, v in TAKEOUT_APPS.items() if k in kind), None)
                if into is None or app is None:
                    continue
                for p in json.load(zf.open(name)).get("Data Points", []):
                    value = p["fitValue"][0]["value"].get("fpVal")
                    if value:
                        into.setdefault(app, {})[int(p["startTimeNanos"]) // 10**9] = value
    return {app: sorted((t, round(kg * 1000), fats.get(app, {}).get(t)) for t, kg in w.items())
            for app, w in weights.items()}


def runkeeper_rows(path: Path) -> list[Row]:
    """Weight (kg) and body fat from measurements.csv in a Runkeeper export zip; [] if there's none."""
    with zipfile.ZipFile(path) as zf:
        name = next((n for n in zf.namelist() if Path(n).name == "measurements.csv"), None)
        if name is None:
            return []
        by_day: dict[int, dict[str, float]] = {}
        for r in csv.DictReader(io.TextIOWrapper(zf.open(name), "utf-8")):
            if r.get("Type") in ("weight", "fat_percent") and r.get("Value"):
                t = int(datetime.strptime(r["Date"], "%Y-%m-%d %H:%M:%S").timestamp())
                by_day.setdefault(t, {})[r["Type"]] = float(r["Value"])
    return sorted((t, round(v["weight"] * 1000), v.get("fat_percent")) for t, v in by_day.items() if "weight" in v)


def import_takeout(conn: sqlite3.Connection, cfg: Config, zips: list[Path]) -> int:
    new = 0
    for app, rows in takeout_rows(zips).items():
        keep(cfg, app, rows)
        new += store(conn, app, rows)
    return new


def import_runkeeper(conn: sqlite3.Connection, cfg: Config, path: Path) -> int:
    rows = runkeeper_rows(path)
    if rows:
        keep(cfg, "runkeeper", rows)
    return store(conn, "runkeeper", rows)


def reload(conn: sqlite3.Connection, cfg: Config) -> int:
    """Every archived reading again, scale uploads included. Returns how many were new."""
    new = aria.reload(conn, cfg)
    for path in sorted((Path(cfg.archive_dir) / "body").glob("*.json")):
        new += store(conn, path.stem, [tuple(r) for r in json.loads(path.read_text())])
    return new


def daily(conn: sqlite3.Connection) -> dict[date, tuple[float, float | None]]:
    """Your weight (kg) and body fat each day you weighed in, averaged over the day. Guests left out."""
    days: dict[date, list[tuple[int, float | None]]] = {}
    for r in conn.execute("SELECT taken, weight_g, fat_pct FROM body WHERE person != 'guest' AND weight_g IS NOT NULL"):
        days.setdefault(datetime.fromtimestamp(r["taken"]).date(), []).append((r["weight_g"], r["fat_pct"]))
    out = {}
    for d, rs in days.items():
        fat = [f for _, f in rs if f is not None]
        out[d] = (sum(g for g, _ in rs) / len(rs) / 1000, sum(fat) / len(fat) if fat else None)
    return out


def first_day(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MIN(taken) FROM body WHERE person != 'guest'").fetchone()
    return datetime.fromtimestamp(row[0]).date() if row and row[0] else None


def series_json(conn: sqlite3.Connection, cfg: Config, start: date, end: date) -> dict:
    """Day columns for the weight chart, in the scale's units (pounds unless it's set to kg)."""
    per = 1.0 if cfg.aria_units == "kg" else 2.20462
    days = daily(conn)
    n = (end - start).days + 1
    weight = [round(days[d][0] * per, 1) if d in days else None for d in (start + timedelta(i) for i in range(n))]
    fat = [round(days[d][1], 1) if d in days and days[d][1] is not None else None for d in (start + timedelta(i) for i in range(n))]

    def trend(xs: list[float | None]) -> list[float | None]:
        out = []
        for i in range(len(xs)):
            got = [x for x in xs[max(0, i - TREND_DAYS + 1):i + 1] if x is not None]
            out.append(round(sum(got) / len(got), 1) if got else None)
        return out

    last = max(days) if days else None
    return {"weight": weight, "weight_trend": trend(weight), "fat": fat, "fat_trend": trend(fat),
            "weight_unit": "kg" if cfg.aria_units == "kg" else "lb",
            "last_weight": {"day": last.isoformat(), "weight": round(days[last][0] * per, 1),
                            "fat": None if days[last][1] is None else round(days[last][1], 1)} if last else None}
