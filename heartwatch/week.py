"""This week's suggested training: the weekly template, reshaped around your events.

About 7 hours a week with one long, hard weekend day; a trip simply takes that
day's place. The rules are plain on purpose, and each changed day says why.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import load

# What a template day can be, and how the page names it.
SESSIONS = {
    "rest": "Rest",
    "easy": "Easy run",
    "short": "Easy 30 min, or off",
    "uphill": "Uphill: ruck on stairs, bleachers or hills",
    "strength": "Easy run + strength",
    "weights": "Strength: dumbbells",
    "hard": "Hill repeats or tempo run",
    "long": "Long day: hike, ruck or snowshoe",
}
# Only the rules make these.
MADE = {"event": "", "recovery": "Recovery: rest or an easy walk",
        "light": "Light strength: upper body and core, or rest"}
HARD = ("uphill", "strength", "weights", "hard", "long")
# Monday first. Minutes are what to aim for; the long day is "this much or more".
DEFAULT_TEMPLATE = [
    {"session": "weights", "minutes": 40},
    {"session": "uphill", "minutes": 60},
    {"session": "strength", "minutes": 60},
    {"session": "hard", "minutes": 45},
    {"session": "short", "minutes": 30},
    {"session": "long", "minutes": 150},
    {"session": "rest", "minutes": 0},
]
MAX_MINUTES = 360
EASE_MINUTES = 45      # a hard day made easy is at most this long
DAYS_BEFORE = {"big": 2, "long": 1, "easy": 0}
BUILD_DAYS = range(6, 14)  # a long day 6-13 days before a big hike or canyon runs a little longer
BUILD_MINUTES = 30
RECOVERY_CHECK_DAYS = 4    # after a big event, resting heart rate is checked this many days on


@dataclass
class Day:
    day: date
    session: str
    minutes: int | None
    why: list[str] = field(default_factory=list)
    event: dict | None = None

    def ease(self, minutes: int, why: str, session: str = "easy") -> None:
        if self.session in HARD:
            if self.session == "weights":  # no heavy legs before an event, but no run in its place either
                session = "light"
            self.session, self.minutes = session, min(self.minutes or minutes, minutes)
            self.why.append(why)


def template(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'week_template'").fetchone()
    return json.loads(row["value"]) if row else [dict(t) for t in DEFAULT_TEMPLATE]


def validate_template(days) -> list[dict]:
    if not isinstance(days, list) or len(days) != 7:
        raise ValueError("the week needs 7 days, Monday first")
    out = []
    for t in days:
        if not isinstance(t, dict) or t.get("session") not in SESSIONS:
            raise ValueError(f"each day's session must be one of {', '.join(SESSIONS)}")
        m = t.get("minutes", 0)
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= MAX_MINUTES:
            raise ValueError(f"minutes must be from 0 to {MAX_MINUTES}")
        out.append({"session": t["session"], "minutes": 0 if t["session"] == "rest" else round(m)})
    return out


def save_template(conn: sqlite3.Connection, days: list | None) -> list[dict]:
    """None (or the default itself) goes back to the default week."""
    days = None if days is None else validate_template(days)
    with conn:
        if days is None or days == DEFAULT_TEMPLATE:
            conn.execute("DELETE FROM meta WHERE key = 'week_template'")
        else:
            conn.execute("INSERT INTO meta (key, value) VALUES ('week_template', ?)"
                         " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(days),))
    return template(conn)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _span(ev: dict) -> tuple[date, date]:
    first = date.fromisoformat(ev["day"])
    return first, date.fromisoformat(ev["end_day"]) if ev["end_day"] else first


def _days_word(n: int) -> str:
    return "tomorrow" if n == 1 else f"in {n} days"


def high_rhr(rhr: dict[date, float], start: date, end: date) -> dict[date, tuple[float, float, float]]:
    """Days whose resting heart rate is above your normal range: (reading, low, high)."""
    out = {}
    for b in load.baseline_series(rhr, start, end):
        if b.value is not None and b.sd and b.baseline_days >= 7 and b.value > b.base + b.sd:
            out[b.day] = (b.value, b.base - b.sd, b.base + b.sd)
    return out


def week(start: date, events: list[dict], tmpl: list[dict], today: date,
         high: dict[date, tuple[float, float, float]]) -> list[Day]:
    days = [Day(start + timedelta(days=i), t["session"], t["minutes"]) for i, t in enumerate(tmpl)]
    at = {d.day: d for d in days}
    end = start + timedelta(days=6)
    spans = [(ev, *_span(ev)) for ev in events]
    here = [(ev, a, b) for ev, a, b in spans if a <= end and b >= start]

    # An event fills its days.
    for ev, a, b in here:
        for i in range((b - a).days + 1):
            d = at.get(a + timedelta(days=i))
            if d:
                d.session, d.minutes, d.event, d.why = "event", None, ev, []

    # It's also the week's long day: a template long day it didn't land on steps aside.
    if here:
        title = here[0][0]["title"]
        for d in days:
            if d.session != "long":
                continue
            beside = any(a - timedelta(days=1) <= d.day <= b + timedelta(days=1) for _, a, b in here)
            d.session, d.minutes = ("rest", 0) if beside else ("easy", EASE_MINUTES)
            d.why.append(f"{title} is this week's long day.")

    # Fresh legs before: two easy days before a big one, one before a long one.
    for ev, a, _ in spans:
        for k in range(1, DAYS_BEFORE[ev["size"]] + 1):
            d = at.get(a - timedelta(days=k))
            if not d:
                continue
            if k == 1 and ev["size"] == "big":
                d.ease(30, f"Fresh legs for {ev['title']} tomorrow.", "short")
            else:
                d.ease(EASE_MINUTES, f"Easy: {ev['title']} {_days_word(k)}.")

    # Recovery after: the day after a big one, and on while resting heart rate is still up.
    for ev, _, b in spans:
        if ev["size"] == "easy":
            continue
        after = at.get(b + timedelta(days=1))
        if after and ev["size"] == "long":
            after.ease(EASE_MINUTES, f"Easy after {ev['title']}.")
            continue
        if after and after.session not in ("event", "rest"):
            after.session, after.minutes = "recovery", None
            after.why.append(f"Recovery after {ev['title']}.")
        for k in range(2, RECOVERY_CHECK_DAYS + 1):
            d = at.get(b + timedelta(days=k))
            if not d or d.session in ("event", "rest"):
                continue
            if d.day in high:
                v, lo, hi = high[d.day]
                d.session, d.minutes = "recovery", None
                d.why.append(f"Still recovering from {ev['title']}: resting heart rate {v:.0f}, above your normal {lo:.0f}–{hi:.0f}.")
            elif k == 2 and d.day > today:
                d.ease(EASE_MINUTES, f"Easy unless resting heart rate is back to normal after {ev['title']}.")

    # Building toward a big hike or canyon: the long days before it run a little longer.
    for d in days:
        if d.session != "long":
            continue
        for ev, a, _ in spans:
            if ev["size"] == "big" and ev["kind"] in ("hike", "canyon") and (a - d.day).days in BUILD_DAYS:
                d.minutes += BUILD_MINUTES
                d.why.append(f"Building toward {ev['title']}: a bit longer, with more climbing.")
                break

    # Snowboarding doesn't train climbing.
    if any(ev["kind"] == "snowboard" for ev, _, _ in here):
        for d in days:
            if d.session == "uphill":
                d.why.append("Snowboarding doesn't train climbing, so keep this one.")

    # A hard day today with resting heart rate up: say so, but leave the call to you.
    d = at.get(today)
    if d and d.session in HARD and today in high and not d.why:
        v, lo, hi = high[today]
        d.why.append(f"Resting heart rate is up today ({v:.0f}, normal {lo:.0f}–{hi:.0f}): go easy if you feel flat.")
    return days


def label(d: Day) -> str:
    return d.event["title"] if d.event else SESSIONS.get(d.session) or MADE[d.session]


def plan_json(conn: sqlite3.Connection, events: list[dict], rhr: dict[date, float],
              done: dict[date, list[str]], today: date) -> dict:
    """This week and next, Monday first, with what was recorded on the days gone by."""
    tmpl = template(conn)
    start = monday(today)
    high = high_rhr(rhr, start - timedelta(days=7), start + timedelta(days=13)) if rhr else {}
    weeks = []
    for s in (start, start + timedelta(days=7)):
        days = week(s, events, tmpl, today, high)
        weeks.append({
            "start": s.isoformat(),
            "minutes": sum(d.minutes or 0 for d in days),
            "days": [{"day": d.day.isoformat(), "session": d.session, "label": label(d), "minutes": d.minutes,
                      "why": d.why, "event": d.event["id"] if d.event else None,
                      "done": done.get(d.day, []) if d.day <= today else []} for d in days],
        })
    return {"weeks": weeks, "template": tmpl, "default": DEFAULT_TEMPLATE, "sessions": SESSIONS}
