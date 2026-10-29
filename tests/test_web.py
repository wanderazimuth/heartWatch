import gzip
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from heartwatch import body, db, settings, store, web
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The fixed resting and max these tests were worked out with (test_profile.py covers the data-driven ones).
        self.cfg = Config(db_path=str(Path(self.tmp.name) / "hw.db"), archive_dir=self.tmp.name,
                          auto_resting_hr=False, auto_max_hr=False)
        conn = db.connect(self.cfg.db_path)
        # A strap recording wrapping an app run, and a dumbbell session the next day.
        store.import_fit(conn, self.cfg, "strap.fit", write(START, [100] * 600 + [150] * 1200 + [100] * 600, "generic"))
        store.import_fit(conn, self.cfg, "run.fit", write(START + timedelta(minutes=10), [150] * 1200))
        store.import_fit(conn, self.cfg, "lift.fit", write(START + timedelta(days=1), [120] * 2400, "training", "strength_training"))
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def post(self, path, body, content_type="application/json"):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_page(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"HeartWatch", r.read())

    def test_app_files(self):
        # What makes the page installable on the phone: the manifest, its icons and the service worker.
        for path, kind in (("/manifest.webmanifest", "application/manifest+json"), ("/sw.js", "text/javascript"),
                           ("/icon-192.png", "image/png"), ("/icon-512.png", "image/png")):
            with urllib.request.urlopen(self.base + path) as r:
                self.assertEqual(r.headers.get_content_type(), kind)
        icons = self.get("/manifest.webmanifest")["icons"]
        self.assertEqual({i["sizes"] for i in icons}, {"192x192", "512x512"})

    def test_series(self):
        d = self.get("/api/series")
        self.assertEqual(d["days"][0], "2026-10-01")
        self.assertEqual(len(d["days"]), len(d["fitness"]))
        # The run's load is a run's; the strap's leftover warm-up is "other".
        i = d["days"].index("2026-10-01")
        self.assertGreater(d["load"]["run"]["threshold"][i], 0)
        self.assertEqual(d["load"]["other"]["threshold"][i], 0)
        self.assertEqual(d["sessions"]["strength"][i + 1], 1)
        self.assertEqual(d["peak_hr"]["bpm"], 150)

    def test_series_is_compressed_for_the_phone(self):
        req = urllib.request.Request(self.base + "/api/series", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["Content-Encoding"], "gzip")
            self.assertEqual(json.loads(gzip.decompress(r.read())), self.get("/api/series"))
        # Without asking for it (the Aria scale, curl) it comes plain.
        with urllib.request.urlopen(self.base + "/api/series") as r:
            self.assertIsNone(r.headers["Content-Encoding"])

    def test_weigh_ins_before_the_first_workout_start_the_charts(self):
        conn = db.connect(self.cfg.db_path)
        body.store(conn, "runkeeper", [(int((START - timedelta(days=3)).timestamp()), 90000, None)])
        conn.close()
        d = self.get("/api/series")
        self.assertEqual((d["days"][0], d["weight"][0], d["weight_unit"]), ("2026-09-28", 198.4, "lb"))
        self.assertEqual(len(d["weight"]), len(d["days"]))

    def test_series_is_built_again_only_after_a_change(self):
        first = self.get("/api/series")
        built = web._series_cache["data"]
        self.assertEqual(self.get("/api/series"), first)
        self.assertIs(web._series_cache["data"], built)
        # A workout the poller imports (another process, straight into the database).
        conn = db.connect(self.cfg.db_path)
        store.import_fit(conn, self.cfg, "run2.fit", write(START + timedelta(days=2), [150] * 1800))
        conn.close()
        i = first["days"].index("2026-10-03")
        self.assertGreater(self.get("/api/series")["load"]["run"]["threshold"][i], 0)

    def test_preview_does_not_save(self):
        plain = self.get("/api/series")["ltl"][-1]
        faster = self.get("/api/series?aerobic_days=10&threshold_days=10")["ltl"][-1]
        self.assertNotEqual(plain, faster)
        self.assertEqual(self.get("/api/series")["ltl"][-1], plain)

    def test_save_and_reset_settings(self):
        status, body = self.post("/api/settings", {"max_hr": 180, "fitness_days": 30})
        self.assertEqual(status, 200)
        self.assertEqual(body["values"]["max_hr"], 180)
        self.assertEqual(self.get("/api/series")["settings"]["fitness_days"], 30)
        self.post("/api/settings", {})
        self.assertEqual(self.get("/api/settings")["values"]["max_hr"], self.cfg.max_hr)

    def test_bad_settings_are_refused(self):
        self.assertEqual(self.post("/api/settings", {"max_hr": 500})[0], 400)
        self.assertEqual(self.post("/api/settings", {"resting_hr": 90, "max_hr": 125})[0], 200)
        self.assertEqual(self.post("/api/settings", {"colour": 1})[0], 400)

    def test_form_posts_are_refused(self):
        # Other websites can only send simple form posts; those must not change anything.
        self.assertEqual(self.post("/api/settings", {"max_hr": 180}, "text/plain")[0], 415)

    def test_changing_a_type_moves_the_shared_minutes(self):
        ws = self.get("/api/workouts")["workouts"]
        run = next(w for w in ws if w["sport"] == "run")
        strap = next(w for w in ws if w["sport"] == "other")
        self.assertLess(strap["minutes"], 21)
        # Call the run "other": both are untyped now, and the shorter run still keeps its minutes.
        self.assertEqual(self.post(f"/api/workouts/{run['id']}/sport", {"sport": "other"})[0], 200)
        ws = {w["id"]: w for w in self.get("/api/workouts")["workouts"]}
        self.assertEqual(ws[run["id"]]["sport"], "other")
        self.assertTrue(ws[run["id"]]["overridden"])
        # Typing the strap recording as a run makes it win over the now-untyped app recording.
        self.post(f"/api/workouts/{strap['id']}/sport", {"sport": "run"})
        ws = {w["id"]: w for w in self.get("/api/workouts")["workouts"]}
        self.assertGreater(ws[strap["id"]]["minutes"], 39)
        self.assertLess(ws[run["id"]]["minutes"], 1)
        # null puts the file's own type back.
        self.post(f"/api/workouts/{run['id']}/sport", {"sport": None})
        self.assertFalse({w["id"]: w for w in self.get("/api/workouts")["workouts"]}[run["id"]]["overridden"])

    def test_bad_type(self):
        self.assertEqual(self.post("/api/workouts/1/sport", {"sport": "swim"})[0], 400)
        self.assertEqual(self.post("/api/workouts/999/sport", {"sport": "run"})[0], 404)

    def test_threshold_tests_and_switches(self):
        self.assertEqual(self.post("/api/thresholdtests/999", {"not_test": True})[0], 404)
        self.assertEqual(self.post("/api/thresholdtests/1", {"not_test": "yes"})[0], 400)
        status, body = self.post("/api/settings", {"auto_max_hr": True})
        self.assertEqual((status, body["values"]["auto_max_hr"]), (200, True))
        self.assertEqual(body["derived"]["max_hr"]["bpm"], 150)  # the run's 20 minutes at 150
        self.assertEqual(self.get("/api/series")["settings"]["max_hr"], 150)
        self.assertEqual(self.post("/api/settings", {"auto_max_hr": 1})[0], 400)

    def upload(self, data, content_type, name="Data Export.zip"):
        req = urllib.request.Request(self.base + "/api/ringconn", data,
                                     {"Content-Type": content_type, "X-Filename": urllib.parse.quote(name)})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_ringconn_upload(self):
        csv = ("Date,Avg. Heart Rate(bpm),Min. Heart Rate(bpm),Avg. HRV(ms)\n"
               "2026-10-01,66,48,31\n2026-10-02,70,52,27\n")
        self.assertEqual(self.upload(csv.encode(), "text/plain")[0], 415)
        self.assertEqual(self.upload(b"Date,Steps\n2026-10-01,9000\n", "text/csv")[0], 400)
        status, body = self.upload(csv.encode(), "text/csv", "vitals.csv")
        self.assertEqual((status, body["days"]), (200, 2))
        d = self.get("/api/series")
        self.assertEqual(d["hrv"][d["days"].index("2026-10-01")], 31)
        self.assertEqual(d["ring"]["days"], 2)
        self.assertEqual(d["readiness_adjust"][-1], 0)  # HRV only charts; it never adjusts

    def test_poll_without_dropbox(self):
        status, body = self.post("/api/poll", {})
        self.assertEqual(status, 409)
        self.assertIn("Dropbox", body["error"])

    def test_events(self):
        status, ev = self.post("/api/events", {"day": "2026-10-30", "title": "Canyon", "kind": "canyon", "size": "big"})
        self.assertEqual(status, 200)
        self.post("/api/events", {"day": "2026-10-03", "title": "Hike"})
        self.assertEqual([e["title"] for e in self.get("/api/events")["events"]], ["Hike", "Canyon"])
        # Only the fields sent change.
        status, changed = self.post(f"/api/events/{ev['id']}", {"pack_lb": 30, "recap": "kept_up"})
        self.assertEqual(status, 200)
        self.assertEqual((changed["title"], changed["size"], changed["pack_lb"], changed["recap"]), ("Canyon", "big", 30, "kept_up"))
        self.assertEqual(self.post(f"/api/events/{ev['id']}", {"deleted": True}), (200, {"ok": True}))
        self.assertEqual([e["title"] for e in self.get("/api/events")["events"]], ["Hike"])
        self.assertEqual(self.post(f"/api/events/{ev['id']}", {"deleted": True})[0], 404)
        self.assertEqual(self.post(f"/api/events/{ev['id']}", {"title": "Gone"})[0], 404)

    def test_drive_route(self):
        ws = self.get("/api/workouts")["workouts"]
        self.assertTrue(all(w["drive_minutes"] is None for w in ws))
        self.assertEqual(self.post(f"/api/workouts/{ws[0]['id']}/drive", {"keep": True})[0], 404)  # no car ride
        self.assertEqual(self.post(f"/api/workouts/{ws[0]['id']}/drive", {"keep": "yes"})[0], 400)

    def test_week(self):
        d = self.get("/api/week")
        self.assertEqual([len(w["days"]) for w in d["weeks"]], [7, 7])
        self.assertEqual(date.fromisoformat(d["weeks"][0]["start"]).weekday(), 0)
        conn = db.connect(self.cfg.db_path)
        days = web.week_json(conn, self.cfg, date(2026, 10, 2))["weeks"][0]["days"]
        conn.close()
        self.assertEqual([x["done"] for x in days[2:6]], [[], ["run"], ["strength"], []])  # Thu run, Fri dumbbells (today)
        tmpl = [dict(t) for t in d["template"]]
        tmpl[0] = {"session": "rest", "minutes": 0}
        status, saved = self.post("/api/week/template", {"days": tmpl})
        self.assertEqual((status, saved["template"][0]["session"]), (200, "rest"))
        self.assertEqual(self.post("/api/week/template", {"days": tmpl[:3]})[0], 400)
        self.assertEqual(self.post("/api/week/template", {"days": None})[1]["template"], d["default"])

    def test_bad_events_are_refused(self):
        for body in ({"title": "No date"}, {"day": "2026-10-30"}, {"day": "2026-10-30", "title": "x", "size": "huge"},
                     {"day": "2026-10-30", "title": "x", "end_day": "2026-10-29"}):
            status, err = self.post("/api/events", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", err)
        self.assertEqual(self.post("/api/events", {"day": "2026-10-30", "title": "x"}, "text/plain")[0], 415)
        self.assertEqual(self.get("/api/events")["events"], [])


class SettingsTest(unittest.TestCase):
    def test_saved_values_equal_to_config_are_dropped(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cfg = Config()
        settings.save(cfg, conn, {"max_hr": cfg.max_hr, "fitness_days": 30})
        self.assertEqual(settings.saved(conn), {"fitness_days": 30})


if __name__ == "__main__":
    unittest.main()
