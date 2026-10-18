import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from heartwatch import db, store, takeout
from heartwatch.config import Config

from fitwriter import write

TZ = timezone(timedelta(hours=-6))
START = datetime(2024, 3, 4, 7, 15, tzinfo=TZ)
T0 = int(START.timestamp())


def make_zip(folder: Path, readings: list[tuple[int, int]], activity_names: list[str]) -> Path:
    path = folder / "takeout.zip"
    points = [{"fitValue": [{"value": {"fpVal": float(b)}}], "startTimeNanos": str(t * 10**9)} for t, b in readings]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(takeout.HR_PREFIX + "HeartRate.json", json.dumps({"Data Points": points}))
        zf.writestr("Takeout/Fit/All Data/raw_com.google.heart_rate.bpm_com.gdjztech.ringconn_health_platform.json",
                    json.dumps({"Data Points": [{"fitValue": [{"value": {"fpVal": 60.0}}], "startTimeNanos": str(T0 * 10**9)}]}))
        for name in activity_names:
            zf.writestr(takeout.ACTIVITY_DIR + name, "<TrainingCenterDatabase/>")
    return path


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_activity_names(self):
        z = make_zip(Path(self.tmp.name), [], [
            "2024-03-04T07_15_39-06_00_PT30M3S_Strength training.tcx",
            "2024-03-06T18_02_44.204-06_00_PT46M18.572S_Running.tcx",
            "2024-03-05T07_15_22-06_00_PT1H2M_Walking,Running.tcx",
            "2024-03-07T07_00_00-06_00_PT0S_.tcx",
        ])
        acts = takeout.activities([z])
        self.assertEqual([a.sport for a in acts], ["strength", "run", "run"])
        self.assertEqual(acts[0].start, datetime(2024, 3, 4, 13, 15, 39, tzinfo=timezone.utc).timestamp())
        self.assertAlmostEqual(acts[0].end - acts[0].start, 30 * 60 + 3)
        self.assertAlmostEqual(acts[1].end - acts[1].start, 62 * 60)  # sorted by date

    def test_only_wahoo_heart_rate_is_read(self):
        z = make_zip(Path(self.tmp.name), [(T0 + 1, 120), (T0 + 2, 121)], [])
        self.assertEqual(takeout.heart_rate([z]), [(T0 + 1, 120), (T0 + 2, 121)])


class SessionTest(unittest.TestCase):
    def test_split_on_gaps_and_drop_short_ones(self):
        morning = [(T0 + i, 120) for i in range(1200)]
        blip = [(T0 + 5000 + i, 90) for i in range(60)]  # a minute of strap fiddling
        evening = [(T0 + 40000 + i, 130) for i in range(900)]
        got = takeout.sessions(morning + blip + evening)
        self.assertEqual([(s.start, s.end) for s in got], [(T0, T0 + 1199), (T0 + 40000, T0 + 40899)])

    def test_typed_activity_beats_a_longer_walk(self):
        s = takeout.Session([(T0 + i, 120) for i in range(1800)])
        walk = takeout.Activity(T0 - 3600, T0 + 7200, ["Walking"])
        lift = takeout.Activity(T0 + 300, T0 + 1500, ["Strength training"])
        takeout.label(s, [walk, lift])
        self.assertEqual((s.sport, s.label), ("strength", "Strength training"))

    def test_unlabelled_without_overlap(self):
        s = takeout.Session([(T0 + i, 120) for i in range(600)])
        takeout.label(s, [takeout.Activity(T0 + 9000, T0 + 9600, ["Running"])])
        self.assertEqual(s.sport, "other")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(archive_dir=self.tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def session(self, start=T0, minutes=20, bpm=130, sport="run"):
        return takeout.Session([(start + i, bpm) for i in range(minutes * 60)], sport, "Running")

    def test_import_and_reimport(self):
        self.assertEqual(store.import_takeout(self.conn, self.cfg, self.session()).status, "added")
        self.assertEqual(store.import_takeout(self.conn, self.cfg, self.session()).status, "duplicate")
        [w] = store.workouts(self.conn)
        self.assertEqual(w.sport, "run")
        self.assertAlmostEqual(w.minutes, 20 - 0.5, delta=0.05)

    def test_session_already_in_a_fit_file_is_skipped(self):
        store.import_fit(self.conn, self.cfg, "run.fit", write(START, [130] * 1200))
        self.assertEqual(store.import_takeout(self.conn, self.cfg, self.session()).status, "duplicate")

    def test_fit_imported_later_takes_the_seconds(self):
        store.import_takeout(self.conn, self.cfg, self.session(sport="strength"))
        store.import_fit(self.conn, self.cfg, "run.fit", write(START + timedelta(minutes=5), [150] * 600))
        by_source = {w.sport: w for w in store.workouts(self.conn)}
        self.assertNotIn(130, by_source["run"].hist)
        self.assertAlmostEqual(by_source["run"].minutes, 9.5, delta=0.05)
        self.assertAlmostEqual(by_source["strength"].minutes, 20 - 0.5 - 10, delta=0.1)


if __name__ == "__main__":
    unittest.main()
