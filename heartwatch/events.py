"""Trips and outings you plan for: the event list on the Plan tab.

An event takes the place of the week's long weekend day. Only a date and a title are
needed; the rest is optional and only makes the weekly suggestion smarter. A past date
means it happened, and deleting one means it was cancelled.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date

KINDS = ("canyon", "hike", "snowboard", "snowshoe", "other")
SIZES = ("easy", "long", "big")  # under 3 h / 3-6 h / over 6 h or several days
RECAPS = ("kept_up", "ok", "struggled")
FIELDS = ("day", "end_day", "title", "kind", "size", "climb_m", "pack_lb", "notes", "recap", "recap_note")
MAX_TEXT = 2000


def _day(v, name: str) -> str:
    try:
        return date.fromisoformat(v).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a date like 2026-10-30") from None


def _number(v, name: str, hi: float, whole: bool):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= hi:
        raise ValueError(f"{name} must be a number from 0 to {hi:g}")
    return round(v) if whole else round(float(v), 1)


def _text(v, name: str, limit: int = MAX_TEXT) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{name} must be text")
    v = v.strip()
    if len(v) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    return v or None


def validate(values: dict) -> dict:
    """A complete event from the page's fields, or ValueError saying what's wrong."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown field: {sorted(unknown)[0]}")
    ev = {"day": _day(values.get("day"), "day")}
    ev["end_day"] = _day(values["end_day"], "end_day") if values.get("end_day") else None
    if ev["end_day"] == ev["day"]:
        ev["end_day"] = None
    if ev["end_day"] and ev["end_day"] < ev["day"]:
        raise ValueError("end_day is before day")
    ev["title"] = _text(values.get("title"), "title", 100)
    if not ev["title"]:
        raise ValueError("title is required")
    ev["kind"] = values.get("kind") or "other"
    if ev["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    ev["size"] = values.get("size") or "long"
    if ev["size"] not in SIZES:
        raise ValueError(f"size must be one of {', '.join(SIZES)}")
    ev["climb_m"] = _number(values.get("climb_m"), "climb_m", 9000, True)
    ev["pack_lb"] = _number(values.get("pack_lb"), "pack_lb", 150, False)
    ev["notes"] = _text(values.get("notes"), "notes")
    ev["recap"] = values.get("recap") or None
    if ev["recap"] is not None and ev["recap"] not in RECAPS:
        raise ValueError(f"recap must be one of {', '.join(RECAPS)}, or empty")
    ev["recap_note"] = _text(values.get("recap_note"), "recap_note")
    return ev


def all_events(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT id, {', '.join(FIELDS)} FROM events ORDER BY day, id").fetchall()
    return [dict(r) for r in rows]


def create(conn: sqlite3.Connection, values: dict) -> dict:
    ev = validate(values)
    now = int(time.time())
    with conn:
        cur = conn.execute(f"INSERT INTO events ({', '.join(FIELDS)}, created, updated)"
                           f" VALUES ({', '.join('?' * len(FIELDS))}, ?, ?)", (*ev.values(), now, now))
    return {"id": cur.lastrowid, **ev}


def update(conn: sqlite3.Connection, event_id: int, values: dict) -> dict | None:
    """Changes only the fields sent; None when there's no such event."""
    row = conn.execute(f"SELECT {', '.join(FIELDS)} FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    ev = validate({**dict(row), **values})
    with conn:
        conn.execute(f"UPDATE events SET {', '.join(f + ' = ?' for f in FIELDS)}, updated = ? WHERE id = ?",
                     (*ev.values(), int(time.time()), event_id))
    return {"id": event_id, **ev}


def delete(conn: sqlite3.Connection, event_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0
