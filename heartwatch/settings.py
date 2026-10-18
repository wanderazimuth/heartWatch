"""Settings changed from the dashboard.

config.toml holds the defaults; anything saved from the dashboard is kept in
the database and wins over them, so tuning doesn't need root on the Pi.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace

from .config import Config

# Name -> (lowest, highest) allowed.
TUNABLE = {
    "resting_hr": (30, 100),
    "max_hr": (120, 230),
    "fitness_days": (3, 120),
    "aerobic_days": (7, 180),
    "threshold_days": (7, 180),
    "anaerobic_days": (3, 120),
    "fatigue_days": (2, 28),
    "readiness_adjust_max": (0, 20),
}


def validate(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key not in TUNABLE:
            raise ValueError(f"unknown setting {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        lo, hi = TUNABLE[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        out[key] = int(value) if key.endswith("_hr") else value
    return out


def check(cfg: Config) -> Config:
    if cfg.resting_hr >= cfg.max_hr:
        raise ValueError("resting heart rate must be below max heart rate")
    return cfg


def saved(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
    # A setting that was renamed or dropped since it was saved is ignored.
    return {k: v for k, v in json.loads(row["value"]).items() if k in TUNABLE} if row else {}


def effective(cfg: Config, conn: sqlite3.Connection, preview: dict | None = None) -> Config:
    """config.toml, then saved dashboard settings, then unsaved preview values on top."""
    return check(replace(cfg, **saved(conn), **validate(preview or {})))


def save(cfg: Config, conn: sqlite3.Connection, values: dict) -> Config:
    """Replace the saved settings. A value equal to config.toml's is dropped, so 'reset' is saving {}."""
    values = validate(values)
    check(replace(cfg, **values))
    base = asdict(cfg)
    values = {k: v for k, v in values.items() if base[k] != v}
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('settings', ?)"
                     " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(values),))
    return effective(cfg, conn)
