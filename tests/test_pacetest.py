import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from heartwatch import db, pacetest, store, web
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 3, 4, 14, 0, tzinfo=timezone.utc)
WARM, TEST = 300, 1200  # seconds


def run(test_speed=1.9, after=200, walk_after=0.0, laps=True, wobble=0.0):
    """A 5-minute warm-up, a 20-minute test lap, then standing still while heart rate falls."""
    bpms, dist, d = [], [], 0.0
    for t in range(WARM + TEST + after):
        if t < WARM:
            speed, bpm = 2.0, 120 + t * 15 // WARM
        elif t < WARM + TEST:
            # Steady pace, heart rate drifting from 140 to 144 between the halves.
            speed = test_speed * (1 + wobble * (1 if (t // 60) % 2 else -1))
            bpm = 140 if t < WARM + TEST // 2 else 144
        else:
            s = t - WARM - TEST
            speed, bpm = walk_after, max(110, 144 - s // 3)  # 20 bpm in the first minute
        d += speed
        bpms.append(bpm)
        dist.append(d)
    lap_list = [(0, WARM, "manual"), (WARM, WARM + TEST, "manual"), (WARM + TEST, len(bpms) - 1, "session_end")]
    return write(START, bpms, distances=dist, laps=lap_list if laps else [(0, len(bpms) - 1, "session_end")])


class FindTest(unittest.TestCase):
    def test_a_test_run(self):
        t = pacetest.find(io.BytesIO(run()))
        self.assertEqual((t.start, t.seconds), (int(START.timestamp()) + WARM, TEST))
        self.assertAlmostEqual(t.pace, 1000 / 1.9, delta=1)
        self.assertAlmostEqual(t.hr, 144, delta=0.5)
        self.assertAlmostEqual(t.decoupling, 144 / 140 - 1, delta=0.005)
        self.assertLess(t.spread, 0.01)
        self.assertAlmostEqual(t.distance, 1.9 * TEST, delta=3)
        self.assertEqual(t.hr_stop - t.hr_after, 20)
        self.assertAlmostEqual(t.beats_per_km, 144 * 1000 / 1.9 / 60, delta=4)

    def test_not_tests(self):
        self.assertIsNone(pacetest.find(io.BytesIO(run(laps=False))))  # an ordinary run
        self.assertIsNone(pacetest.find(io.BytesIO(run(wobble=0.3))))  # intervals, not a steady pace
        self.assertIsNone(pacetest.find(io.BytesIO(write(START, [140] * 1800))))  # no distance at all

    def test_recovery_needs_the_recording_and_standing_still(self):
        self.assertIsNone(pacetest.find(io.BytesIO(run(after=20))).hr_after)  # auto-pause stopped the recording
        self.assertIsNone(pacetest.find(io.BytesIO(run(walk_after=1.2))).hr_after)  # walked it off
        self.assertIsNotNone(pacetest.find(io.BytesIO(run(wobble=0.05))))  # a slightly uneven test still counts


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=str(Path(tmp.name) / "hw.db"), archive_dir=tmp.name)
        self.conn = db.connect(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def test_found_on_import_and_kept_through_a_rescan(self):
        store.import_fit(self.conn, self.cfg, "test.fit", run())
        store.import_fit(self.conn, self.cfg, "easy.fit", run(test_speed=2.1, laps=False))  # an ordinary run
        [t] = pacetest.tests_json(self.conn)
        self.assertEqual((t["minutes"], t["recovery"], t["not_test"]), (20, 20, False))
        self.conn.execute("UPDATE pace_tests SET not_test = 1")
        self.assertEqual(pacetest.scan(self.conn, self.cfg), 1)
        self.assertTrue(pacetest.tests_json(self.conn)[0]["not_test"])
        self.assertEqual(len(web.series_json(self.conn, self.cfg)["pace_tests"]), 1)


if __name__ == "__main__":
    unittest.main()
