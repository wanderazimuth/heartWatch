import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from heartwatch import db, settings, store, web
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=str(Path(self.tmp.name) / "hw.db"), archive_dir=self.tmp.name)
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

    def test_poll_without_dropbox(self):
        status, body = self.post("/api/poll", {})
        self.assertEqual(status, 409)
        self.assertIn("Dropbox", body["error"])


class SettingsTest(unittest.TestCase):
    def test_saved_values_equal_to_config_are_dropped(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cfg = Config()
        settings.save(cfg, conn, {"max_hr": cfg.max_hr, "fitness_days": 30})
        self.assertEqual(settings.saved(conn), {"fitness_days": 30})


if __name__ == "__main__":
    unittest.main()
