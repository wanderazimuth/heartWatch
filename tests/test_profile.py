import unittest
from datetime import date, datetime, timedelta

from heartwatch import db, profile, settings, store
from heartwatch.config import Config

TODAY = date(2026, 10, 20)


def workout(i: int, day: date, hist: dict[int, float]) -> store.Workout:
    start = datetime(day.year, day.month, day.day, 8)
    return store.Workout(i, start, start + timedelta(hours=1), "run", max(hist), hist)


class HeldPeakTest(unittest.TestCase):
    def test_a_glitch_is_not_a_peak(self):
        # 2 seconds at 190 are a strap glitch; 165 and above adds up to 5 seconds.
        self.assertEqual(profile.held_peak({190: 2, 165: 3, 150: 600}), 165)
        self.assertEqual(profile.held_peak({172: 5, 150: 600}), 172)
        self.assertIsNone(profile.held_peak({150: 4}))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = Config(resting_hr=60, max_hr=180)

    def ring(self, days: int, bpm: float):
        """A resting heart rate from the ring for each of the last `days` days."""
        with self.conn:
            for k in range(days):
                t = int(datetime.combine(TODAY - timedelta(days=k), datetime.min.time()).timestamp())
                self.conn.execute("INSERT INTO health_records (id, type, start, end, value, origin, received)"
                                  " VALUES (?, 'resting_hr', ?, ?, ?, 'com.ringconn', 0)", (f"r{k}", t, t, bpm + k % 2))

    def test_from_the_data(self):
        self.ring(20, 52)  # alternating 52 and 53
        ws = [workout(1, TODAY - timedelta(days=400), {185: 30, 150: 600}),  # over a year ago: aged out
              workout(2, TODAY - timedelta(days=100), {174: 6, 150: 600}),
              workout(3, TODAY - timedelta(days=5), {176: 1, 160: 600})]
        cfg, info = profile.apply(self.cfg, self.conn, ws, TODAY)
        self.assertEqual((cfg.resting_hr, cfg.max_hr), (52, 174))  # 52.5 rounds to even
        self.assertEqual(info["max_hr"]["date"], "2026-07-12")
        self.assertEqual(info["resting_hr"]["days"], 20)

    def test_too_little_data_keeps_the_settings(self):
        self.ring(10, 52)
        cfg, info = profile.apply(self.cfg, self.conn, [], TODAY)
        self.assertEqual((cfg.resting_hr, cfg.max_hr), (60, 180))
        self.assertIsNone(info["resting_hr"]["bpm"])
        self.assertEqual(info["resting_hr"]["days"], 10)

    def test_switched_off(self):
        self.ring(20, 52)
        ws = [workout(1, TODAY, {170: 60})]
        cfg, info = profile.apply(Config(resting_hr=60, max_hr=180, auto_resting_hr=False, auto_max_hr=False),
                                  self.conn, ws, TODAY)
        self.assertEqual((cfg.resting_hr, cfg.max_hr), (60, 180))
        self.assertEqual((info["resting_hr"]["bpm"], info["max_hr"]["bpm"]), (52, 170))  # still shown

    def test_odd_data_falls_back(self):
        self.ring(20, 95)  # a ring reading far too high...
        cfg, _ = profile.apply(self.cfg, self.conn, [workout(1, TODAY, {90: 600})], TODAY)  # ...and a walk as max
        self.assertEqual((cfg.resting_hr, cfg.max_hr), (60, 180))

    def test_a_threshold_that_does_not_fit_is_left_out(self):
        cfg, info = profile.apply(Config(resting_hr=60, max_hr=180, auto_max_hr=False, lthr=172), self.conn, [], TODAY)
        self.assertEqual(cfg.lthr, 172)
        cfg, info = profile.apply(Config(resting_hr=60, max_hr=180, auto_max_hr=False, lthr=135), self.conn, [], TODAY)
        self.assertEqual(cfg.lthr, 0)  # zone 2 would start at 115, below zone 1's 120
        self.assertFalse(info["lthr"]["fits"])


class SettingsTest(unittest.TestCase):
    def test_switches_are_saved(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cfg = Config()
        eff = settings.save(cfg, conn, {"auto_max_hr": False, "lthr_zones": True})
        self.assertFalse(eff.auto_max_hr)
        self.assertEqual(settings.saved(conn), {"auto_max_hr": False})  # True is config.toml's already
        with self.assertRaises(ValueError):
            settings.save(cfg, conn, {"auto_max_hr": 0})


if __name__ == "__main__":
    unittest.main()
