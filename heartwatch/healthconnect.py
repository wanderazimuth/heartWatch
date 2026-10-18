"""Daily resting heart rate and sleep from Android Health Connect.

An app on the phone (HC Webhook) posts what's new in Health Connect as JSON.
HeartWatch keeps two kinds of record from it: resting heart rate (the ring
writes one a day) and sleep sessions. Every payload is archived as received,
so the records can be read again if this code changes.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Config

# Prefer the ring's own records when several apps write the same type.
RING_ORIGIN = "ringconn"


@dataclass
class Received:
    resting_hr: int = 0
    sleep: int = 0
    deleted: int = 0


def _instant(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _records(payload: dict) -> list[tuple]:
    """(id, type, start, end, value, origin, detail) rows for the record types HeartWatch uses."""
    rows = []
    for r in payload.get("resting_heart_rate") or []:
        meta = r.get("metadata") or {}
        t = _instant(r["time"])
        rows.append((meta.get("id") or f"rhr:{t}", "resting_hr", t, t, float(r["bpm"]), meta.get("data_origin", ""), None))
    for r in payload.get("sleep") or []:
        meta = r.get("metadata") or {}
        end = _instant(r["session_end_time"])
        start = end - int(r["duration_seconds"])
        # Time asleep: the session less any awake stages.
        awake = sum(int(s.get("duration_seconds", 0)) for s in r.get("stages") or []
                    if "awake" in str(s.get("stage", "")).lower() or "out_of_bed" in str(s.get("stage", "")).lower())
        rows.append((meta.get("id") or f"sleep:{end}", "sleep", start, end, float(int(r["duration_seconds"]) - awake),
                     meta.get("data_origin", ""), json.dumps(r.get("stages") or [])))
    return rows


def receive(conn: sqlite3.Connection, cfg: Config, body: bytes) -> Received:
    """Archive a payload and store its records; a resent record (same id) replaces the old one."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    rows = _records(payload)
    deleted = [d["id"] for d in payload.get("deleted") or [] if isinstance(d, dict) and d.get("id")]
    if not rows and not deleted:
        return Received()
    now = time.time()
    dest = Path(cfg.archive_dir) / "healthconnect" / f"{datetime.fromtimestamp(now):%Y}" / f"{now:.3f}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wb") as f:
        f.write(body)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO health_records (id, type, start, end, value, origin, detail, received)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [row + (int(now),) for row in rows])
        conn.executemany("DELETE FROM health_records WHERE id = ?", [(i,) for i in deleted])
    return Received(sum(r[1] == "resting_hr" for r in rows), sum(r[1] == "sleep" for r in rows), len(deleted))


def _preferred(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    ring = [r for r in rows if RING_ORIGIN in r["origin"].lower()]
    return ring or rows


def resting_hr(conn: sqlite3.Connection) -> dict[date, float]:
    """One resting heart rate per local day, the ring's if it wrote one, the latest if several."""
    rows = _preferred(conn.execute("SELECT * FROM health_records WHERE type = 'resting_hr' ORDER BY start").fetchall())
    return {datetime.fromtimestamp(r["start"]).date(): r["value"] for r in rows}


def sleep(conn: sqlite3.Connection) -> dict[date, float]:
    """Hours asleep per night, keyed by the day the night ended."""
    out: dict[date, float] = {}
    for r in _preferred(conn.execute("SELECT * FROM health_records WHERE type = 'sleep' ORDER BY end").fetchall()):
        d = datetime.fromtimestamp(r["end"]).date()
        out[d] = out.get(d, 0) + r["value"] / 3600
    return out


def last_received(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(received) FROM health_records").fetchone()[0]
