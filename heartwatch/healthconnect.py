"""Daily resting heart rate and sleep from Android Health Connect.

An app on the phone (HC Webhook) posts what's new in Health Connect as JSON.
HeartWatch keeps two kinds of record from it: resting heart rate (the ring
writes about one a night) and sleep sessions. Each payload is archived with
just those two kinds, so the records can be read again if this code changes;
the rest (workout heart rate, steps, SpO2...) is dropped, unless
healthconnect_keep_all asks for the whole payload to be kept as well.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config

# Prefer the ring's own records when several apps write the same type.
RING_ORIGIN = "ringconn"
KEPT = ("resting_heart_rate", "sleep", "deleted")
# RingConn stamps a night's resting heart rate anywhere from bedtime to waking,
# so a reading belongs to the day that starts within 12 hours of it.
DAY_SHIFT = timedelta(hours=12)


@dataclass
class Received:
    resting_hr: int = 0
    sleep: int = 0
    deleted: int = 0
    test: bool = False


def _instant(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _records(payload: dict) -> list[tuple]:
    """(id, type, start, end, value, origin, detail) rows for the record types HeartWatch uses.

    HC Webhook may not send Health Connect's record ids, so a missing id is made
    from the record itself: the same record sent twice still lands once."""
    rows = []
    for r in payload.get("resting_heart_rate") or []:
        meta = r.get("metadata") or {}
        origin = meta.get("data_origin", "")
        t = _instant(r["time"])
        rows.append((meta.get("id") or f"rhr:{origin}:{t}", "resting_hr", t, t, float(r["bpm"]), origin, None))
    for r in payload.get("sleep") or []:
        meta = r.get("metadata") or {}
        origin = meta.get("data_origin", "")
        end = _instant(r["session_end_time"])
        start = end - int(r["duration_seconds"])
        # Time asleep: the session less any awake stages.
        awake = sum(int(s.get("duration_seconds", 0)) for s in r.get("stages") or []
                    if "awake" in str(s.get("stage", "")).lower() or "out_of_bed" in str(s.get("stage", "")).lower())
        rows.append((meta.get("id") or f"sleep:{origin}:{start}:{end}", "sleep", start, end,
                     float(int(r["duration_seconds"]) - awake), origin, json.dumps(r.get("stages") or [])))
    return rows


def _store(conn: sqlite3.Connection, payload: dict, received: int) -> Received:
    rows = _records(payload)
    deleted = [d["id"] for d in payload.get("deleted") or [] if isinstance(d, dict) and d.get("id")]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO health_records (id, type, start, end, value, origin, detail, received)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [row + (received,) for row in rows])
        conn.executemany("DELETE FROM health_records WHERE id = ?", [(i,) for i in deleted])
    return Received(sum(r[1] == "resting_hr" for r in rows), sum(r[1] == "sleep" for r in rows), len(deleted))


def _kept(payload: dict) -> dict:
    """The payload without the record types HeartWatch doesn't use."""
    return {k: v for k, v in payload.items() if k in KEPT or not isinstance(v, (list, dict))}


def receive(conn: sqlite3.Connection, cfg: Config, body: bytes) -> Received:
    """Archive a payload and store its records; a resent record (same id) replaces the old one."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if payload.get("test") is True:
        # The app's Test button sends made-up sample records; they prove the
        # connection works but must not become readings.
        return Received(test=True)
    now = time.time()
    if cfg.healthconnect_keep_all and any(isinstance(v, list) and v for v in payload.values()):
        # As sent, byte for byte: kept apart from the trimmed archive so reload() never reads it.
        _write(Path(cfg.archive_dir) / "healthconnect-all", now, body)
    payload = _kept(payload)
    _records(payload)  # refuse a payload that can't be read before archiving it
    if not any(payload.get(k) for k in KEPT):
        return Received()
    _write(Path(cfg.archive_dir) / "healthconnect", now, json.dumps(payload).encode())
    return _store(conn, payload, int(now))


def _write(folder: Path, now: float, data: bytes) -> None:
    dest = folder / f"{datetime.fromtimestamp(now):%Y}" / f"{now:.3f}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wb") as f:
        f.write(data)


def reload(conn: sqlite3.Connection, cfg: Config) -> int:
    """Read every archived payload again, oldest first, into an emptied table. Returns how many."""
    files = sorted(Path(cfg.archive_dir, "healthconnect").glob("*/*.json.gz"), key=lambda p: float(p.name[:-8]))
    with conn:
        conn.execute("DELETE FROM health_records")
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            _store(conn, _kept(json.load(fh)), int(float(f.name[:-8])))
    return len(files)


def _preferred(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    ring = [r for r in rows if RING_ORIGIN in r["origin"].lower()]
    return ring or rows


def resting_hr(conn: sqlite3.Connection) -> dict[date, float]:
    """One resting heart rate per day, the ring's if it wrote any, averaged if several."""
    days: dict[date, list[float]] = {}
    for r in _preferred(conn.execute("SELECT * FROM health_records WHERE type = 'resting_hr' ORDER BY start").fetchall()):
        days.setdefault((datetime.fromtimestamp(r["start"]) + DAY_SHIFT).date(), []).append(r["value"])
    return {d: sum(v) / len(v) for d, v in days.items()}


def sleep(conn: sqlite3.Connection) -> dict[date, float]:
    """Hours asleep per night, keyed by the day the night ended.

    The longest session of the night: RingConn writes the night as one session
    and its stages again as separate, unlabelled short ones, so adding them up
    would count the night twice."""
    out: dict[date, float] = {}
    for r in _preferred(conn.execute("SELECT * FROM health_records WHERE type = 'sleep' ORDER BY end").fetchall()):
        d = datetime.fromtimestamp(r["end"]).date()
        out[d] = max(out.get(d, 0), r["value"] / 3600)
    return out


def last_received(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(received) FROM health_records").fetchone()[0]
