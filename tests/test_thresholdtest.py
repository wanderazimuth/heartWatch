import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from heartwatch import db, store, thresholdtest, web
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 3, 4, 14, 0, tzinfo=timezone.utc)
WARM = 600  # seconds


def run(test=1800, hr=160, speed=3.3, laps=True):
    """A 10-minute warm-up, a test lap with heart rate climbing for 10 minutes then holding, a cool-down."""
    bpms, dist, d = [], [], 0.0
    for t in range(WARM + test + 300):
        if t < WARM:
            s, bpm = 2.5, 125
        elif t < WARM + test:
            s, into = speed, t - WARM
            bpm = hr - 20 + 20 * into // 600 if into < 600 else hr
            if WARM + test - 10 <= t:
                bpm = hr + 8  # the finish
        else:
            s, bpm = 1.5, 130
        d += s
        bpms.append(bpm)
        dist.append(d)
    lap_list = [(0, WARM, "manual"), (WARM, WARM + test, "manual"), (WARM + test, len(bpms) - 1, "session_end")]
    return write(START, bpms, distances=dist, laps=lap_list if laps else [(0, len(bpms) - 1, "session_end")])


class FindTest(unittest.TestCase):
    def test_a_threshold_test(self):
        t = thresholdtest.find(io.BytesIO(run()))
        self.assertEqual((t.start, t.seconds), (int(START.timestamp()) + WARM, 1800))
        # The last 20 minutes: 160 held, and 10 seconds of the finish at 168.
        self.assertAlmostEqual(t.hr, 160, delta=0.2)
        self.assertEqual(t.peak, 168)
        self.assertAlmostEqual(t.pace, 1000 / 3.3, delta=1)
        self.assertAlmostEqual(t.distance, 3.3 * 1800, delta=4)

    def test_laps_too_short_or_long_or_missing(self):
        self.assertIsNone(thresholdtest.find(io.BytesIO(run(test=20 * 60))))  # a pace test's length
        self.assertIsNone(thresholdtest.find(io.BytesIO(run(test=45 * 60))))
        self.assertIsNone(thresholdtest.find(io.BytesIO(run(laps=False))))

    def test_without_gps(self):
        bpms = [125] * WARM + [160] * 1800 + [130] * 300
        t = thresholdtest.find(io.BytesIO(write(START, bpms, laps=[(0, WARM, "manual"), (WARM, WARM + 1800, "manual")])))
        self.assertEqual(round(t.hr), 160)
        self.assertIsNone(t.pace)


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Fixed resting and max, so "hard enough" is easy to follow: 80% of the reserve is 156.
        self.cfg = Config(db_path=str(Path(tmp.name) / "hw.db"), archive_dir=tmp.name,
                          resting_hr=60, max_hr=180, auto_resting_hr=False, auto_max_hr=False)
        self.conn = db.connect(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def test_a_hard_lap_sets_the_zones(self):
        store.import_fit(self.conn, self.cfg, "test.fit", run())
        d = web.series_json(self.conn, self.cfg)
        [t] = d["threshold_tests"]
        self.assertEqual((t["lthr"], t["minutes"], t["not_test"]), (160, 30, False))
        # A threshold test's lap is steady enough to pass for a pace test; it only counts as one.
        self.assertEqual(d["pace_tests"], [])
        self.assertEqual((d["settings"]["lthr"], d["derived"]["lthr"]["date"]), (160, "2026-03-04"))
        self.assertEqual(d["zones"][1], (136, 144))  # zone 2 from 85% of 160

    def test_an_easy_lap_is_a_pace_test_instead(self):
        store.import_fit(self.conn, self.cfg, "easy.fit", run(hr=140, speed=2.6))
        d = web.series_json(self.conn, self.cfg)
        self.assertEqual(d["threshold_tests"], [])
        self.assertEqual(len(d["pace_tests"]), 1)
        self.assertEqual(d["settings"]["lthr"], 0)  # zones stay on the reserve

    def test_the_latest_test_not_marked_wins(self):
        store.import_fit(self.conn, self.cfg, "first.fit", run(hr=158))
        # A week later, a test with no archived file behind it.
        t = thresholdtest.Test(int(START.timestamp()) + 86400 * 7, 1800, 163.0, 170)
        with self.conn:
            self.conn.execute("INSERT INTO workouts (id, start, end, sport, source, source_ref, imported)"
                              " VALUES (99, ?, ?, 'run', 'fit', 'later', 0)", (t.start, t.start + 1800))
        thresholdtest.save(self.conn, 99, t)
        self.assertEqual(web.series_json(self.conn, self.cfg)["settings"]["lthr"], 163)
        with self.conn:
            self.conn.execute("UPDATE threshold_tests SET not_test = 1 WHERE workout_id = 99")
        self.assertEqual(web.series_json(self.conn, self.cfg)["settings"]["lthr"], 158)
        # A rescan finds the archived test again, and drops the one whose file is gone.
        self.assertEqual(thresholdtest.scan(self.conn, self.cfg), 1)

    def test_zones_switched_off(self):
        store.import_fit(self.conn, self.cfg, "test.fit", run())
        cfg = Config(**{**self.cfg.__dict__, "lthr_zones": False})
        d = web.series_json(self.conn, cfg)
        self.assertEqual(d["settings"]["lthr"], 0)
        self.assertEqual(d["zones"][1], (132, 144))  # back on the reserve
        self.assertEqual(d["derived"]["lthr"]["bpm"], 160)  # still shown


if __name__ == "__main__":
    unittest.main()
