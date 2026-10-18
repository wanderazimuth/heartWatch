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

from . import db, dropbox, healthconnect, load, settings, store
from .config import Config

log = logging.getLogger(__name__)

SPORTS = ("run", "strength", "other")
WORKOUT_PATH = re.compile(r"/api/workouts/(\d+)/sport")
UPLOAD_TYPES = ("application/zip", "application/x-zip-compressed", "text/csv", "application/octet-stream")
MAX_UPLOAD = 20 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]")


def series_json(conn, cfg: Config, today: date | None = None) -> dict:
    """Every day from the first workout to today, as columns, plus what the page needs around them."""
    ws = store.workouts(conn)
    meta = {k: dropbox.get_meta(conn, k) for k in ("last_poll", "last_new_workout")}
    out = {
        "settings": {k: getattr(cfg, k) for k in settings.TUNABLE},
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
    for w in ws:
        d, sport = w.start.date(), w.sport if w.sport in SPORTS else "other"
        per_sport[sport].setdefault(d, load.Load())
        per_sport[sport][d] += load.workout_load(w.hist, cfg)
        if store.is_workout(w, cfg):
            sessions[sport][d] = sessions[sport].get(d, 0) + 1
    total = store.daily_loads(ws, cfg)
    start, end = ws[0].start.date(), max(today, ws[-1].start.date())
    rhr = load.readiness(healthconnect.resting_hr(conn), cfg, start, end)
    hrv = load.baseline_series(store.hrv_readings(conn), start, end)
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
        **baseline_json("rhr", rhr),
        **baseline_json("hrv", hrv),
        "readiness_adjust": [r(b.adjust) for b in rhr],
        "min_baseline": load.MIN_BASELINE,
    })
    last = max((w for w in ws if store.is_workout(w, cfg)), key=lambda w: w.start, default=None)
    out["last_workout"] = last.start.isoformat(timespec="minutes") if last else None
    peak = max(ws, key=lambda w: w.peak_hr)
    out["peak_hr"] = {"bpm": peak.peak_hr, "date": peak.start.date().isoformat()}
    out["ring"] = ring_json(conn, today)
    out["phone"] = phone_json(conn, cfg)
    return out


def baseline_json(key: str, series: list[load.Baseline]) -> dict:
    """A ring reading as columns, null where there is none. The normal range (base +- sd)
    is left out until a week of baseline days makes it more than noise."""
    rn = lambda x: None if x is None else round(x, 1)
    band = [b.sd is not None and b.baseline_days >= 7 for b in series]
    return {
        key: [rn(b.value) for b in series],
        f"{key}_week": [rn(b.week) for b in series],
        f"{key}_lo": [rn(b.base - b.sd) if ok else None for b, ok in zip(series, band)],
        f"{key}_hi": [rn(b.base + b.sd) if ok else None for b, ok in zip(series, band)],
        f"{key}_baseline_days": series[-1].baseline_days,
    }


def phone_json(conn, cfg: Config) -> dict:
    rhr, sleep = healthconnect.resting_hr(conn), healthconnect.sleep(conn)
    return {"last_received": healthconnect.last_received(conn),
            "last_rhr_day": max(rhr).isoformat() if rhr else None,
            "last_sleep": {"day": max(sleep).isoformat(), "hours": round(sleep[max(sleep)], 2)} if sleep else None,
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
    out = []
    for w in reversed(store.workouts(conn)):
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
            return {"values": {k: getattr(eff, k) for k in settings.TUNABLE},
                    "defaults": {k: defaults[k] for k in settings.TUNABLE},
                    "limits": settings.TUNABLE}

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
            if not cfg.healthconnect_key:
                return self._json({"error": "healthconnect_key is not set in config.toml"}, 503)
            key = self.headers.get("X-HeartWatch-Key", "")
            if not hmac.compare_digest(key.encode(), cfg.healthconnect_key.encode()):
                return self._json({"error": "wrong or missing X-HeartWatch-Key"}, 401)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "expected application/json"}, 415)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_UPLOAD:
                return self._json({"error": "body is empty or too big"}, 413)
            body = self.rfile.read(size)
            conn = db.connect(cfg.db_path)
            try:
                got = healthconnect.receive(conn, cfg, body)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("bad Health Connect payload: %s", e)
                return self._json({"error": f"couldn't read the payload: {e}"}, 400)
            finally:
                conn.close()
            log.info("health connect: %d resting HR, %d sleep, %d deleted", got.resting_hr, got.sleep, got.deleted)
            return self._json({"ok": True, "resting_hr": got.resting_hr, "sleep": got.sleep, "deleted": got.deleted})

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
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(cfg: Config, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(cfg))
    log.info("dashboard on http://%s:%d", host, port)
    server.serve_forever()
