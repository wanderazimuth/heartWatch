import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from heartwatch import climb, db, events, store, web
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)


def hike(*legs, speed=1.0):
    """A hike from legs of (seconds, metres climbed over them, bpm), from 1600 m."""
    bpms, alts, a = [], [], 1600.0
    for seconds, gain, bpm in legs:
        for _ in range(seconds):
            a += gain / seconds
            alts.append(a)
            bpms.append(bpm)
    return write(START, bpms, "hiking", altitudes=alts, speeds=[speed] * len(bpms))


class FindTest(unittest.TestCase):
    def test_a_climb(self):
        # 10 minutes flat, 200 m in 40 minutes, then down again.
        [c] = climb.find(io.BytesIO(hike((600, 0, 100), (2400, 200, 150), (1800, -200, 110))))
        self.assertAlmostEqual(c.start - START.timestamp(), 600, delta=20)
        self.assertAlmostEqual(c.seconds, 2400, delta=60)  # the ends trimmed in by TRIM
        self.assertAlmostEqual(c.gain, 200, delta=5)
        self.assertAlmostEqual(c.rate, 300, delta=10)
        self.assertAlmostEqual(c.hr, 150, delta=1)
        self.assertEqual(c.peak, 150)
        # 95 beats above resting a minute, for 40 minutes, over 200 m.
        self.assertAlmostEqual(climb.beats_per_100m(c.hr, c.seconds, c.gain, 55), 95 * 40 / 2, delta=40)

    def test_small_dips_and_short_stops_dont_end_it(self):
        legs = ((1200, 100, 150), (90, -8, 130), (90, 0, 120), (1200, 100, 150))
        [c] = climb.find(io.BytesIO(hike(*legs)))
        self.assertAlmostEqual(c.gain, 192, delta=6)  # net: the dip is given back

    def test_a_long_stop_or_a_real_descent_does(self):
        self.assertEqual(len(climb.find(io.BytesIO(hike((1200, 100, 150), (600, 0, 100), (1200, 100, 150))))), 2)
        self.assertEqual(len(climb.find(io.BytesIO(hike((1200, 100, 150), (300, -40, 120), (1200, 100, 150))))), 2)

    def test_not_climbs(self):
        self.assertEqual(climb.find(io.BytesIO(hike((1200, 40, 140)))), [])  # too little gain
        self.assertEqual(climb.find(io.BytesIO(hike((300, 100, 150)))), [])  # too short
        self.assertEqual(climb.find(io.BytesIO(hike((900, 500, 100)))), [])  # a ski lift: 2000 m/h
        self.assertEqual(climb.find(io.BytesIO(hike((1200, 100, 100), speed=12))), [])  # a car up a pass
        self.assertEqual(climb.find(io.BytesIO(write(START, [150] * 1800))), [])  # no altitude at all

    def test_a_car_ride_at_the_end_is_left_out(self):
        data = hike((1200, 0, 100), (1200, 150, 100))
        self.assertEqual(len(climb.find(io.BytesIO(data))), 1)
        self.assertEqual(climb.find(io.BytesIO(data), until=START.timestamp() + 1200), [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=str(Path(tmp.name) / "hw.db"), archive_dir=tmp.name, resting_hr=55)
        self.conn = db.connect(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def test_found_on_import_and_kept_through_a_rescan(self):
        store.import_fit(self.conn, self.cfg, "hike.fit", hike((600, 0, 100), (2400, 200, 150), (1800, -200, 110)))
        [c] = climb.climbs_json(self.conn, self.cfg)
        self.assertAlmostEqual(c["gain"], 200, delta=5)
        self.assertEqual((c["rate"], c["hr"], c["pack_lb"], c["not_climb"]), (300, 150, None, False))
        self.assertTrue(climb.update(self.conn, c["id"], {"pack_lb": 20, "not_climb": True}))
        self.assertEqual(climb.scan(self.conn, self.cfg), 1)
        [c] = climb.climbs_json(self.conn, self.cfg)
        self.assertEqual((c["pack_lb"], c["pack_from_event"], c["not_climb"]), (20, False, True))
        self.assertEqual(len(web.series_json(self.conn, self.cfg)["climbs"]), 1)

    def test_the_pack_comes_from_that_days_trip(self):
        store.import_fit(self.conn, self.cfg, "hike.fit", hike((2400, 200, 150)))
        day = datetime.fromtimestamp(START.timestamp()).date().isoformat()
        events.create(self.conn, {"day": day, "title": "Canyon", "kind": "canyon", "size": "long", "pack_lb": 15})
        [c] = climb.climbs_json(self.conn, self.cfg)
        self.assertEqual((c["pack_lb"], c["pack_from_event"]), (15, True))
        climb.update(self.conn, c["id"], {"pack_lb": 0})  # carried nothing on this one after all
        self.assertEqual(climb.climbs_json(self.conn, self.cfg)[0]["pack_lb"], 0)

    def test_bad_updates(self):
        store.import_fit(self.conn, self.cfg, "hike.fit", hike((2400, 200, 150)))
        cid = climb.climbs_json(self.conn, self.cfg)[0]["id"]
        for bad in ({"pack_lb": "15"}, {"pack_lb": 200}, {"pack_lb": True}, {"not_climb": 1}, {}):
            with self.assertRaises(ValueError):
                climb.update(self.conn, cid, bad)
        self.assertFalse(climb.update(self.conn, 999, {"pack_lb": None}))


if __name__ == "__main__":
    unittest.main()
