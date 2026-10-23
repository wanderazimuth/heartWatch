"""Resting, max and threshold heart rate from your own data.

Each one falls back to the number in the settings when it's switched off or
there's too little data, and says which it used:

- resting: the ring's daily resting heart rate, averaged over the last 30 days;
- max: the highest heart rate held for 5 seconds in any workout in the last 12
  months, so a one-beat strap glitch doesn't count and max can come down with
  age. Only a hard effort reaches it: a year of easy runs reads low;
- threshold (LTHR): the latest threshold test (thresholdtest.py), which sets
  zones 2-5 (load.py).

Everything is scored with today's numbers, the whole history included, like
any other setting.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, timedelta

from . import healthconnect, load, store, thresholdtest
from .config import Config

RESTING_DAYS = 30
MIN_RESTING_DAYS = 14  # readings needed in those 30 days
MAX_DAYS = 365
HELD = 5  # seconds at or above a heart rate for it to count as reached


def held_peak(hist: dict[int, float], seconds: float = HELD) -> int | None:
    """The highest heart rate a workout spent `seconds` at or above."""
    total = 0.0
    for bpm in sorted(hist, reverse=True):
        total += hist[bpm]
        if total >= seconds:
            return bpm
    return None


def resting(conn: sqlite3.Connection, today: date) -> tuple[int | None, int]:
    """The ring's average over the last 30 days, and how many days had a reading."""
    since = today - timedelta(days=RESTING_DAYS)
    vals = [v for d, v in healthconnect.resting_hr(conn).items() if since < d <= today]
    return (round(sum(vals) / len(vals)) if len(vals) >= MIN_RESTING_DAYS else None), len(vals)


def peak(ws: list[store.Workout], today: date) -> tuple[int, date] | None:
    """The highest held heart rate in the last 12 months, and the day of it."""
    since = today - timedelta(days=MAX_DAYS)
    found = [(p, w.start.date()) for w in ws if since < w.start.date() <= today and (p := held_peak(w.hist))]
    return max(found, default=None)


def apply(cfg: Config, conn: sqlite3.Connection, ws: list[store.Workout] | None = None,
          today: date | None = None) -> tuple[Config, dict]:
    """cfg with resting, max and LTHR from the data where switched on, and where each came from."""
    today = today or date.today()
    ws = store.workouts(conn) if ws is None else ws
    rest, days = resting(conn, today)
    top = peak(ws, today)
    out = cfg
    info = {
        "resting_hr": {"auto": cfg.auto_resting_hr, "bpm": rest, "days": days, "min_days": MIN_RESTING_DAYS},
        "max_hr": {"auto": cfg.auto_max_hr, "bpm": top[0] if top else None, "date": top[1].isoformat() if top else None},
    }
    if cfg.auto_resting_hr and rest is not None:
        out = replace(out, resting_hr=rest)
    if cfg.auto_max_hr and top:
        out = replace(out, max_hr=top[0])
    if out.resting_hr >= out.max_hr:  # odd data: a ring that reads high, a year of no hard efforts
        out = replace(out, resting_hr=cfg.resting_hr, max_hr=cfg.max_hr)
    info["resting_hr"]["used"], info["max_hr"]["used"] = out.resting_hr, out.max_hr

    test = thresholdtest.latest(conn, out)
    lthr, source = (cfg.lthr, "setting") if cfg.lthr else (test["lthr"], "test") if test else (0, None)
    hrr = out.max_hr - out.resting_hr
    # Zone 2 has to start above zone 1, and threshold below max, or the zones make no sense.
    fits = bool(lthr) and (out.resting_hr + load.ZONE_STARTS[0] * hrr < lthr * load.LTHR_ZONE_STARTS[0] / 100
                           and lthr < out.max_hr)
    used = lthr if cfg.lthr_zones and fits else 0
    info["lthr"] = {"auto": cfg.lthr_zones, "bpm": lthr or None, "source": source, "fits": fits, "used": used,
                    "date": test["start"][:10] if source == "test" else None}
    return replace(out, lthr=used), info
