"""The HeartWatch dashboard: one page, JSON underneath.

Stdlib only. All the numbers are computed here, by the same code the command
line uses, so the page only draws them. Intended for the home LAN: there is
no login, and anyone who can reach the port can change workout types and
settings.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from dataclasses import asdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import db, dropbox, healthconnect, load, movement, pacetest, profile, settings, store, thresholdtest
from .config import Config

log = logging.getLogger(__name__)

SPORTS = ("run", "strength", "other")
WORKOUT_PATH = re.compile(r"/api/workouts/(\d+)/sport")
PACE_TEST_PATH = re.compile(r"/api/pacetests/(\d+)")
THRESHOLD_TEST_PATH = re.compile(r"/api/thresholdtests/(\d+)")
UPLOAD_TYPES = ("application/zip", "application/x-zip-compressed", "text/csv", "application/octet-stream")
MAX_UPLOAD = 20 * 1024 * 1024
# The Health Connect key may come in the URL, so keep it out of the logs.
SECRET_QUERY = re.compile(r"([?&]key=)[^&\s]*")


def redact(text: str) -> str:
    return SECRET_QUERY.sub(lambda m: m[1] + "***", text)
SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]")
# For the 80/20 readout: a session with this many minutes in zone 3 or above wasn't an easy one.
HARD_SESSION_MINUTES = 10


def series_json(conn, cfg: Config, today: date | None = None) -> dict:
    """Every day from the first workout to today, as columns, plus what the page needs around them.

    cfg is the settings; resting, max and threshold heart rate from the data are put in here."""
    ws = store.workouts(conn)
    cfg, derived = profile.apply(cfg, conn, ws, today)
    meta = {k: dropbox.get_meta(conn, k) for k in ("last_poll", "last_new_workout")}
    out = {
        "settings": {k: getattr(cfg, k) for k in (*settings.TUNABLE, *settings.SWITCHES, "lthr")},
        "derived": derived,
        "zones": load.zone_bounds(cfg),
        "last_poll": int(meta["last_poll"]) if meta["last_poll"] else None,
        "dropbox": bool(cfg.dropbox_app_key),
        "days": [],
    }
    if not ws:
        return out
    today = today or date.today()
    per_sport: dict[str, dict[date, load.Load]] = {s: {} for s in SPORTS}
    sessions: dict[str, dict[date, int]] = {s: {} for s in SPORTS}
    hard_sessions: dict[str, dict[date, int]] = {s: {} for s in SPORTS}
    minutes: dict[str, dict[date, list[float]]] = {s: {} for s in SPORTS}  # per zone 1-5
    for w in ws:
        d, sport = w.start.date(), w.sport if w.sport in SPORTS else "other"
        per_sport[sport].setdefault(d, load.Load())
        per_sport[sport][d] += load.workout_load(w.hist, cfg)
        if store.is_workout(w, cfg):
            sessions[sport][d] = sessions[sport].get(d, 0) + 1
            if sum(s for b, s in w.hist.items() if load.zone(b, cfg) >= 3) / 60 >= HARD_SESSION_MINUTES:
                hard_sessions[sport][d] = hard_sessions[sport].get(d, 0) + 1
        for bpm, secs in w.hist.items():
            z = load.zone(bpm, cfg)
            if z:  # below zone 1 is standing around, not training
                minutes[sport].setdefault(d, [0.0] * 5)[z - 1] += secs / 60
    total = store.daily_loads(ws, cfg)
    start, end = ws[0].start.date(), max(today, ws[-1].start.date())
    rhr = load.readiness(healthconnect.resting_hr(conn), cfg, start, end)
    hrv = load.baseline_series(store.hrv_readings(conn), start, end)
    # Shown with your normal range like HRV, and like HRV it doesn't change capacity.
    sleep = healthconnect.sleep(conn)
    slept = load.baseline_series(sleep, start, end)
    # Today is still going, so the averages and ranges use finished days; the page shows today apart.
    moved = movement_days(conn, cfg, ws)
    done = {d: m for d, m in moved.items() if d < today}
    steps = load.baseline_series({d: m.steps for d, m in done.items()}, start, end)
    brisk = load.baseline_series({d: m.brisk_minutes for d, m in done.items() if m.brisk_minutes is not None}, start, end)
    days = load.series(total, cfg, start, end, {b.day: b.adjust for b in rhr})
    r = lambda x: round(x, 2)
    rn = lambda x: None if x is None else round(x, 1)
    out["days"] = [d.day.isoformat() for d in days]
    out.update({
        "fitness": [r(d.fitness) for d in days],
        "ltl": [r(d.ltl) for d in days],
        "ltl_aerobic": [r(d.long_term.aerobic) for d in days],
        "ltl_threshold": [r(d.long_term.threshold) for d in days],
        "ltl_anaerobic": [r(d.long_term.anaerobic) for d in days],
        "stl": [r(d.short_term) for d in days],
        "capacity": [r(d.capacity) for d in days],
        "change": [round(d.change, 4) for d in days],
        "state": [d.state for d in days],
        # Each day's own load, per workout type, for the Trends bars.
        "load": {s: {k: [r(getattr(per_sport[s].get(d.day, load.Load()), k)) for d in days]
                     for k in ("aerobic", "threshold", "anaerobic")} for s in SPORTS},
        "sessions": {s: [sessions[s].get(d.day, 0) for d in days] for s in SPORTS},
        "hard_sessions": {s: [hard_sessions[s].get(d.day, 0) for d in days] for s in SPORTS},
        "hard_session_minutes": HARD_SESSION_MINUTES,
        # Minutes in zones 1-5 each day, per workout type, for the time-in-zone bars.
        "zone_minutes": {s: [[round(minutes[s].get(d.day, [0.0] * 5)[z], 1) for d in days] for z in range(5)] for s in SPORTS},
        **baseline_json("rhr", rhr),
        **baseline_json("hrv", hrv),
        **baseline_json("sleep", slept, digits=2),
        **baseline_json("steps", steps, digits=0),
        **baseline_json("brisk", brisk),
        "bursts": [moved[d.day].bursts if d.day in done else None for d in days],
        "readiness_adjust": [r(b.adjust) for b in rhr],
        "min_baseline": load.MIN_BASELINE,
    })
    last = max((w for w in ws if store.is_workout(w, cfg)), key=lambda w: w.start, default=None)
    out["last_workout"] = last.start.isoformat(timespec="minutes") if last else None
    peak = max(ws, key=lambda w: w.peak_hr)
    out["peak_hr"] = {"bpm": peak.peak_hr, "date": peak.start.date().isoformat()}
    # A threshold test's lap is long and steady enough to pass for a pace test too: it's only one of them.
    out["threshold_tests"] = thresholdtest.tests_json(conn, cfg)
    hard = {(t["id"], t["start"]) for t in out["threshold_tests"]}
    out["pace_tests"] = [p for p in pacetest.tests_json(conn) if (p["id"], p["start"]) not in hard]
    out["ring"] = ring_json(conn, today)
    out["phone"] = phone_json(conn, cfg, sleep)
    now = moved.get(today)
    out["movement_today"] = None if now is None else {
        "steps": now.steps, "brisk_minutes": None if now.brisk_minutes is None else round(now.brisk_minutes, 1),
        "bursts": now.bursts, "source": now.source}
    return out


_movement_cache: dict = {}


def movement_days(conn, cfg: Config, ws: list[store.Workout]) -> dict[date, movement.Movement]:
    """movement.daily(), worked out again only when steps or workouts have changed."""
    key = (cfg.db_path, *conn.execute("SELECT COUNT(*), MAX(received) FROM health_records WHERE type = 'steps'").fetchone(),
           len(ws), ws[-1].id if ws else None)
    if cfg.db_path == ":memory:" or _movement_cache.get("key") != key:
        _movement_cache.update(key=key, days=movement.daily(conn, ws))
    return _movement_cache["days"]


def baseline_json(key: str, series: list[load.Baseline], digits: int = 1) -> dict:
    """A ring reading as columns, null where there is none. The normal range (base +- sd)
    is left out until a week of baseline days makes it more than noise."""
    rn = lambda x: None if x is None else round(x, digits)
    band = [b.sd is not None and b.baseline_days >= 7 for b in series]
    return {
        key: [rn(b.value) for b in series],
        f"{key}_week": [rn(b.week) for b in series],
        f"{key}_lo": [rn(b.base - b.sd) if ok else None for b, ok in zip(series, band)],
        f"{key}_hi": [rn(b.base + b.sd) if ok else None for b, ok in zip(series, band)],
        f"{key}_baseline_days": series[-1].baseline_days,
    }


def phone_json(conn, cfg: Config, sleep: dict[date, float] | None = None) -> dict:
    rhr = healthconnect.resting_hr(conn)
    sleep = healthconnect.sleep(conn) if sleep is None else sleep
    last = max(sleep) if sleep else None
    return {"last_received": healthconnect.last_received(conn),
            "last_rhr_day": max(rhr).isoformat() if rhr else None,
            "last_sleep": {"day": last.isoformat(), "hours": round(sleep[last], 2),
                           "stages": healthconnect.sleep_stages(conn).get(last)} if sleep else None,
            "configured": bool(cfg.healthconnect_key)}


def ring_json(conn, today: date) -> dict | None:
    v = store.vitals(conn)
    if not v:
        return None
    lows = sorted(r["min_hr"] for d, r in v.items() if r["min_hr"] is not None and (today - d).days < 30)
    return {"first": min(v).isoformat(), "last": max(v).isoformat(), "days": len(v),
            "imported": max(r["imported"] for r in v.values()),
            # The day's lowest heart rate, a guide to resting heart rate.
            "min_hr_median": lows[len(lows) // 2] if lows else None}


def workouts_json(conn, cfg: Config) -> list[dict]:
    rows = {r["id"]: r for r in conn.execute("SELECT id, sport, sport_override, source, detail FROM workouts")}
    ws = store.workouts(conn)
    cfg, _ = profile.apply(cfg, conn, ws)
    out = []
    for w in reversed(ws):
        row = rows[w.id]
        l = load.workout_load(w.hist, cfg)
        zones = [0.0] * 6
        for b, s in w.hist.items():
            zones[load.zone(b, cfg)] += s / 60
        out.append({
            "id": w.id, "start": w.start.isoformat(timespec="minutes"), "sport": w.sport,
            "file_sport": row["sport"], "overridden": row["sport_override"] is not None,
            "source": row["source"], "detail": row["detail"],
            "minutes": round(w.minutes, 1), "active_minutes": round(w.active_minutes(cfg), 1),
            "workout": store.is_workout(w, cfg),
            "avg_hr": round(w.avg_hr), "peak_hr": w.peak_hr,
            "load": round(l.total, 1), "aerobic": round(l.aerobic, 1), "threshold": round(l.threshold, 1),
            "anaerobic": round(l.anaerobic, 1), "zone_minutes": [round(z, 1) for z in zones],
        })
    return out


def preview_values(query: dict) -> dict:
    out = {}
    for key in settings.TUNABLE:
        if key in query:
            try:
                out[key] = float(query[key][0])
            except ValueError:
                raise ValueError(f"{key} must be a number")
    return out


def make_handler(cfg: Config):
    index_html = resources.files("heartwatch").joinpath("static", "index.html").read_bytes()
    poll_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "HeartWatch"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, index_html, "text/html; charset=utf-8")
            if url.path not in ("/api/series", "/api/workouts", "/api/settings"):
                return self._json({"error": "not found"}, 404)
            conn = db.connect(cfg.db_path)
            try:
                try:
                    eff = settings.effective(cfg, conn, preview_values(parse_qs(url.query)))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                if url.path == "/api/series":
                    return self._json(series_json(conn, eff))
                if url.path == "/api/workouts":
                    return self._json({"workouts": workouts_json(conn, eff)})
                return self._json(self._settings(conn, eff))
            finally:
                conn.close()

        def _settings(self, conn, eff: Config) -> dict:
            defaults = asdict(cfg)
            keys = (*settings.TUNABLE, *settings.SWITCHES)
            return {"values": {k: getattr(eff, k) for k in keys},
                    "defaults": {k: defaults[k] for k in keys},
                    "limits": settings.TUNABLE,
                    "derived": profile.apply(eff, conn)[1]}

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/ringconn":
                return self._ringconn()
            if path == "/api/healthconnect":
                return self._healthconnect()
            # Requiring JSON forces a CORS preflight for cross-site requests, which we
            # never approve, so other websites can't change anything via your browser.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "expected application/json"}, 415)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            if path == "/api/poll":
                return self._poll()
            conn = db.connect(cfg.db_path)
            try:
                if path == "/api/settings":
                    try:
                        eff = settings.save(cfg, conn, body)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(self._settings(conn, eff))
                for pattern, table, name in ((PACE_TEST_PATH, "pace_tests", "pace test"),
                                             (THRESHOLD_TEST_PATH, "threshold_tests", "threshold test")):
                    m = pattern.fullmatch(path)
                    if not m:
                        continue
                    if not isinstance(body.get("not_test"), bool):
                        return self._json({"error": "not_test must be true or false"}, 400)
                    with conn:
                        found = conn.execute(f"UPDATE {table} SET not_test = ? WHERE workout_id = ?",
                                             (int(body["not_test"]), int(m.group(1)))).rowcount
                    return self._json({"ok": True}) if found else self._json({"error": f"unknown {name}"}, 404)
                m = WORKOUT_PATH.fullmatch(path)
                if not m:
                    return self._json({"error": "not found"}, 404)
                sport = body.get("sport")
                if sport is not None and sport not in SPORTS:
                    return self._json({"error": "sport must be run, strength, other or null"}, 400)
                row = conn.execute("SELECT start, end FROM workouts WHERE id = ?", (int(m.group(1)),)).fetchone()
                if row is None:
                    return self._json({"error": "unknown workout"}, 404)
                with conn:
                    conn.execute("UPDATE workouts SET sport_override = ? WHERE id = ?", (sport, int(m.group(1))))
                # The type decides which overlapping recording owns each second.
                store.rebuild(conn, cfg, store.overlapping(conn, row["start"], row["end"]))
                return self._json({"ok": True})
            finally:
                conn.close()

        def _ringconn(self):
            # A zip or CSV body, never a form: those content types also need a CORS
            # preflight, so other websites can't upload through your browser either.
            if self.headers.get("Content-Type", "").split(";")[0] not in UPLOAD_TYPES:
                return self._json({"error": "expected a RingConn export (.zip or .csv)"}, 415)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_UPLOAD:
                return self._json({"error": "file is empty or too big"}, 413)
            data = self.rfile.read(size)
            name = SAFE_NAME.sub("_", Path(unquote(self.headers.get("X-Filename", "")) or "ringconn-export.zip").name)[:120]
            conn = db.connect(cfg.db_path)
            try:
                days = store.import_ringconn(conn, cfg, name, data)
            except (ValueError, UnicodeDecodeError) as e:
                return self._json({"error": f"Couldn't read that file: {e}"}, 400)
            finally:
                conn.close()
            return self._json({"ok": True, "days": len(days), "first": days[0].day.isoformat(), "last": days[-1].day.isoformat()})

        def _healthconnect(self):
            # The phone app proves itself with the shared key, so nothing else on the
            # network can write readings. JSON only, like every other POST.
            def refuse(error, status):
                # Logged so a phone that stopped syncing can be diagnosed; header names only, never the key.
                log.warning("health connect refused (%d): %s; content-type %r, length %r, headers %s", status, error,
                            self.headers.get("Content-Type"), self.headers.get("Content-Length"), sorted(self.headers.keys()))
                return self._json({"error": error}, status)

            if not cfg.healthconnect_key:
                return refuse("healthconnect_key is not set in config.toml", 503)
            # The header, or ?key= in the URL: HC Webhook leaves custom headers off its manual syncs.
            key = (self.headers.get("X-HeartWatch-Key")
                   or (parse_qs(urlparse(self.path).query).get("key") or [""])[0]).strip()
            if not hmac.compare_digest(key.encode(), cfg.healthconnect_key.encode()):
                return refuse("wrong or missing key (X-HeartWatch-Key header or ?key= in the URL)", 401)
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return refuse("expected application/json", 415)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_UPLOAD:
                return refuse("body is empty or too big", 413)
            body = self.rfile.read(size)
            conn = db.connect(cfg.db_path)
            try:
                got = healthconnect.receive(conn, cfg, body)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("bad Health Connect payload: %s", e)
                return self._json({"error": f"couldn't read the payload: {e}"}, 400)
            finally:
                conn.close()
            if got.test:
                log.info("health connect: test payload from the phone, not stored")
            else:
                log.info("health connect: %d resting HR, %d sleep, %d steps, %d deleted", got.resting_hr, got.sleep, got.steps, got.deleted)
            return self._json({"ok": True, "test": got.test, "resting_hr": got.resting_hr, "sleep": got.sleep,
                               "steps": got.steps, "deleted": got.deleted})

        def _poll(self):
            if not cfg.dropbox_app_key:
                return self._json({"error": "Dropbox isn't connected yet: see the README."}, 409)
            if not poll_lock.acquire(blocking=False):
                return self._json({"error": "An update is already running."}, 409)
            conn = db.connect(cfg.db_path)
            try:
                token = Path(cfg.dropbox_token_file).read_text().strip()
                result = dropbox.poll(conn, cfg, dropbox.Client(cfg.dropbox_app_key, token))
            except Exception as e:
                log.warning("dropbox poll from the dashboard failed", exc_info=True)
                return self._json({"error": f"Dropbox update failed: {e}"}, 502)
            finally:
                conn.close()
                poll_lock.release()
            return self._json({"ok": True, "added": result.added, "failed": result.failed, "seen": result.seen})

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), redact(fmt % args))

        def log_request(self, code="-", size="-"):
            # Failed requests are worth seeing (a phone app pointed at the wrong path, say);
            # a ?key= in the request line is masked.
            level = logging.WARNING if str(code).isdigit() and int(code) >= 400 else logging.DEBUG
            log.log(level, "%s \"%s\" %s", self.address_string(), redact(self.requestline), code)

    return Handler


def serve(cfg: Config, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("dashboard on http://%s:%d", host, port)
    server.serve_forever()
