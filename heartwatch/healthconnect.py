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
# Health Connect's sleep stage numbers; HC Webhook sends them as numbers or names.
STAGE_NAMES = {"0": "unknown", "1": "awake", "2": "sleeping", "3": "out_of_bed",
               "4": "light", "5": "deep", "6": "rem", "7": "awake_in_bed"}
AWAKE = {"awake", "out_of_bed", "awake_in_bed"}
ASLEEP = {"light", "deep", "rem"}


def _stage(s: dict) -> str:
    """A stage's name, from a number ("4") or a name in any spelling ("SLEEP_STAGE_LIGHT")."""
    raw = str(s.get("stage", "")).strip().lower()
    if raw in STAGE_NAMES:
        return STAGE_NAMES[raw]
    for name in ("awake_in_bed", "out_of_bed", "awake", "light", "deep", "rem", "sleeping"):
        if name in raw.replace(" ", "_"):
            return name
    return "unknown"


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
        awake = sum(int(s.get("duration_seconds", 0)) for s in r.get("stages") or [] if _stage(s) in AWAKE)
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
    """Hours asleep per night: sleep that began between noon the day before and noon that day.

    RingConn writes the night as one session and then each stage again as its
    own short session. With HC Webhook's sleep detail set to full those carry
    their stage, and the time covered by light, deep and REM pieces is the time
    asleep (within a couple of minutes of the RingConn app). Covered, not added
    up: when RingConn re-analyses a night it writes the stages again without
    removing the old ones, so pieces overlap. Without stages, the longest
    session of the night stands in."""
    longest, staged = _nights(conn)
    return longest | {d: _covered([s for spans in by.values() for s in spans]) / 3600 for d, by in staged.items()}


def sleep_stages(conn: sqlite3.Connection) -> dict[date, dict[str, int]]:
    """Minutes of light, deep and REM sleep per night, for nights sent with stages.

    Each stage is counted once however many times RingConn rewrote it, so where
    two versions of a night disagree the stages can add up to a few minutes more
    than the time asleep."""
    return {d: {s: round(_covered(by.get(s, [])) / 60) for s in ("light", "deep", "rem")}
            for d, by in _nights(conn)[1].items()}


def _nights(conn: sqlite3.Connection):
    """Per night: the longest session in hours, and the spans of each asleep stage."""
    longest: dict[date, float] = {}
    staged: dict[date, dict[str, list[tuple[int, int]]]] = {}
    for r in _preferred(conn.execute("SELECT * FROM health_records WHERE type = 'sleep' ORDER BY start").fetchall()):
        d = (datetime.fromtimestamp(r["start"]) + DAY_SHIFT).date()
        stages = json.loads(r["detail"] or "[]")
        if len(stages) == 1 and _stage(stages[0]) in ASLEEP:
            staged.setdefault(d, {}).setdefault(_stage(stages[0]), []).append((r["start"], r["end"]))
        longest[d] = max(longest.get(d, 0), r["value"] / 3600)
    return longest, staged


def _covered(spans: list[tuple[int, int]]) -> int:
    """Seconds covered by the spans, overlaps counted once."""
    total, reach = 0, None
    for a, b in sorted(spans):
        if reach is not None and a < reach:
            a = reach
        if b > a:
            total += b - a
        reach = b if reach is None else max(reach, b)
    return total


def last_received(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(received) FROM health_records").fetchone()[0]
