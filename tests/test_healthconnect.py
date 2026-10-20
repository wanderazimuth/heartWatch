import gzip
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from heartwatch import db, healthconnect, load, store, web
from heartwatch.config import Config

from fitwriter import write

TZ = timezone(timedelta(hours=-6))
RING = "com.example.ringconn"


def rhr(day: date, bpm: int, origin: str = RING, rid: str | None = None) -> dict:
    return {"bpm": bpm, "time": datetime(day.year, day.month, day.day, 7, 0, tzinfo=TZ).isoformat(),
            "metadata": {"id": rid or f"rhr-{day}-{origin}", "data_origin": origin}}


def night(end_day: date, hours: float, awake_min: int = 0) -> dict:
    end = datetime(end_day.year, end_day.month, end_day.day, 6, 30, tzinfo=TZ)
    stages = [{"stage": "AWAKE", "duration_seconds": awake_min * 60}] if awake_min else []
    return {"session_end_time": end.isoformat(), "duration_seconds": int(hours * 3600), "stages": stages,
            "metadata": {"id": f"sleep-{end_day}", "data_origin": RING}}


class ReceiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=":memory:", archive_dir=tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def send(self, payload: dict):
        return healthconnect.receive(self.conn, self.cfg, json.dumps(payload).encode())

    def test_resting_hr_and_sleep(self):
        d = date(2024, 3, 4)
        got = self.send({"timestamp": "x", "resting_heart_rate": [rhr(d, 51)], "sleep": [night(d, 7.5, awake_min=30)],
                         "heart_rate": [{"bpm": 90, "time": "2024-03-04T12:00:00Z"}]})
        self.assertEqual((got.resting_hr, got.sleep), (1, 1))
        self.assertEqual(healthconnect.resting_hr(self.conn), {d: 51})
        self.assertEqual(healthconnect.sleep(self.conn), {d: 7.0})
        self.assertEqual(len(list(Path(self.cfg.archive_dir, "healthconnect").rglob("*.json.gz"))), 1)

    def test_resent_and_deleted_records(self):
        d = date(2024, 3, 4)
        self.send({"resting_heart_rate": [rhr(d, 51, rid="a")]})
        self.send({"resting_heart_rate": [rhr(d, 53, rid="a")]})
        self.assertEqual(healthconnect.resting_hr(self.conn), {d: 53})
        self.send({"deleted": [{"type": "resting_heart_rate", "id": "a"}]})
        self.assertEqual(healthconnect.resting_hr(self.conn), {})

    def test_the_ring_wins_over_other_apps(self):
        d = date(2024, 3, 4)
        self.send({"resting_heart_rate": [rhr(d, 60, origin="com.other.app"), rhr(d, 51)]})
        self.assertEqual(healthconnect.resting_hr(self.conn), {d: 51})

    def test_test_payloads_are_not_stored(self):
        got = self.send({"test": True, "resting_heart_rate": [rhr(date(2024, 3, 4), 57)]})
        self.assertTrue(got.test)
        self.assertEqual(healthconnect.resting_hr(self.conn), {})
        self.assertFalse(Path(self.cfg.archive_dir, "healthconnect").exists())

    def test_ring_style_nights_and_bedtime_readings(self):
        # RingConn: the night as one session plus its stages as unlabelled sessions, no ids,
        # and the resting heart rate stamped at bedtime the evening before.
        end = datetime(2024, 3, 5, 6, 30, tzinfo=TZ)
        part = lambda secs, back: {"session_end_time": (end - timedelta(seconds=back)).isoformat(), "duration_seconds": secs,
                                   "stages": [], "metadata": {"data_origin": RING}}
        evening = {"bpm": 52, "time": datetime(2024, 3, 4, 23, 40, tzinfo=TZ).isoformat(), "metadata": {"data_origin": RING}}
        payload = {"sleep": [part(7 * 3600, 0), part(1800, 0), part(3600, 1800), part(300, 7000)],
                   "resting_heart_rate": [evening], "heart_rate": [{"bpm": 140, "time": end.isoformat()}]}
        self.send(payload)
        self.send(payload)
        self.assertEqual(healthconnect.sleep(self.conn), {date(2024, 3, 5): 7.0})
        self.assertEqual(healthconnect.resting_hr(self.conn), {date(2024, 3, 5): 52})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM health_records").fetchone()[0], 5)
        archived = [json.loads(gzip.decompress(p.read_bytes())) for p in Path(self.cfg.archive_dir, "healthconnect").rglob("*.json.gz")]
        self.assertTrue(archived and all("heart_rate" not in a for a in archived))
        self.assertEqual(healthconnect.reload(self.conn, self.cfg), 2)
        self.assertEqual(healthconnect.resting_hr(self.conn), {date(2024, 3, 5): 52})

    def test_keep_all(self):
        body = {"steps": [{"count": 5}], "resting_heart_rate": [rhr(date(2024, 3, 4), 51)]}
        self.send(body)
        self.assertFalse(Path(self.cfg.archive_dir, "healthconnect-all").exists())
        self.cfg.healthconnect_keep_all = True
        self.send(body)
        self.send({"test": True, "steps": [{"count": 5}]})
        self.send({"steps": [{"count": 7}]})  # nothing HeartWatch uses, still kept whole
        kept = sorted(Path(self.cfg.archive_dir, "healthconnect-all").rglob("*.json.gz"))
        self.assertEqual([json.loads(gzip.decompress(p.read_bytes()))["steps"][0]["count"] for p in kept], [5, 7])
        self.assertEqual(healthconnect.reload(self.conn, self.cfg), 2)

    def test_staged_night(self):
        # Full sleep detail: the whole night as stage 2 ("sleeping"), then each stage as its own
        # session with Health Connect's number. Time asleep is light + deep + REM.
        end = datetime(2024, 3, 5, 6, 30, tzinfo=TZ)
        def piece(stage, secs, back):
            e = end - timedelta(seconds=back)
            return {"session_end_time": e.isoformat(), "duration_seconds": secs, "metadata": {"data_origin": RING},
                    "stages": [{"stage": stage, "start_time": (e - timedelta(seconds=secs)).isoformat(),
                                "end_time": e.isoformat(), "duration_seconds": secs}]}
        self.send({"sleep": [piece("2", 8 * 3600, 0), piece("4", 3 * 3600, 5 * 3600), piece("1", 3600, 4 * 3600),
                             piece("5", 3600, 3 * 3600), piece("6", 2 * 3600, 3600), piece("7", 3600, 0)]})
        self.assertEqual(healthconnect.sleep(self.conn), {date(2024, 3, 5): 6.0})
        self.assertEqual([healthconnect._stage({"stage": s}) for s in ("4", "SLEEP_STAGE_DEEP", "rem", "Awake in bed", "9")],
                         ["light", "deep", "rem", "awake_in_bed", "unknown"])

    def test_nothing_useful_is_not_archived(self):
        self.assertEqual(self.send({"timestamp": "x", "steps": [{"count": 5}]}).resting_hr, 0)
        self.assertFalse(Path(self.cfg.archive_dir, "healthconnect").exists())


class ReadinessTest(unittest.TestCase):
    start = date(2024, 1, 1)

    def days(self, values):
        return {self.start + timedelta(days=i): v for i, v in enumerate(values)}

    def run_(self, values, cfg=Config()):
        return load.readiness(self.days(values), cfg, self.start, self.start + timedelta(days=len(values) - 1))[-1]

    def test_no_adjustment_while_learning(self):
        # 27 days: the 7-day week plus 20 days before it, one short of a baseline.
        b = self.run_([50, 54] * 13 + [50])
        self.assertEqual(b.baseline_days, 20)
        self.assertEqual(b.adjust, 0)
        self.assertIsNotNone(b.week)

    def test_raised_resting_hr_takes_away_and_is_capped(self):
        b = self.run_([50, 54] * 12 + [60] * 7)
        self.assertGreater(b.week, b.base + b.sd)
        self.assertEqual(b.adjust, -12)
        self.assertEqual(self.run_([50, 54] * 12 + [60] * 7, Config(readiness_adjust_max=0)).adjust, 0)

    def test_low_resting_hr_adds(self):
        self.assertGreater(self.run_([50, 54] * 12 + [49] * 7).adjust, 5)

    def test_normal_week_is_about_zero(self):
        self.assertLess(abs(self.run_([50, 54] * 20).adjust), 2)

    def test_hrv_charts_without_adjusting(self):
        b = load.baseline_series(self.days([30, 34] * 12 + [20] * 7), self.start, self.start + timedelta(days=30))[-1]
        self.assertEqual(b.adjust, 0)
        self.assertIsNotNone(b.week)


class EndpointTest(unittest.TestCase):
    KEY = "test-key-123"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=str(Path(tmp.name) / "hw.db"), archive_dir=tmp.name, healthconnect_key=self.KEY)
        conn = db.connect(self.cfg.db_path)
        store.import_fit(conn, self.cfg, "run.fit", write(datetime(2026, 10, 1, 14, tzinfo=timezone.utc), [150] * 1200))
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, payload, key=KEY, content_type="application/json", query=""):
        headers = {"Content-Type": content_type}
        if key is not None:
            headers["X-HeartWatch-Key"] = key
        req = urllib.request.Request(self.base + "/api/healthconnect" + query, json.dumps(payload).encode(), headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_key_is_required(self):
        payload = {"resting_heart_rate": [rhr(date(2026, 10, 2), 51)]}
        self.assertEqual(self.post(payload, key=None)[0], 401)
        self.assertEqual(self.post(payload, key="wrong")[0], 401)
        self.assertEqual(self.post(payload, content_type="text/plain")[0], 415)
        status, body = self.post(payload)
        self.assertEqual((status, body["resting_hr"]), (200, 1))
        with urllib.request.urlopen(self.base + "/api/series") as r:
            d = json.load(r)
        self.assertEqual(d["rhr"][d["days"].index("2026-10-02")], 51)
        self.assertEqual(d["phone"]["last_rhr_day"], "2026-10-02")

    def test_key_in_the_url(self):
        payload = {"resting_heart_rate": [rhr(date(2026, 10, 2), 51)]}
        self.assertEqual(self.post(payload, key=None, query="?key=wrong")[0], 401)
        with self.assertLogs("heartwatch.web", "WARNING") as logs:
            self.post(payload, key=None, query="?key=wrong")
        self.assertNotIn("wrong", "".join(logs.output).replace("wrong or missing", ""))
        self.assertEqual(self.post(payload, key=None, query=f"?key={self.KEY}")[0], 200)

    def test_bad_payload(self):
        self.assertEqual(self.post({"resting_heart_rate": [{"time": "not a time", "bpm": 5}]})[0], 400)

    def test_off_without_a_key(self):
        self.cfg.healthconnect_key = ""
        self.assertEqual(self.post({"resting_heart_rate": []})[0], 503)


if __name__ == "__main__":
    unittest.main()
