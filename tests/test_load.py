import math
import unittest
from datetime import date, timedelta

from heartwatch import load
from heartwatch.config import Config
from heartwatch.load import Load

CFG = Config(resting_hr=60, max_hr=180)  # the numbers the zone tests were worked out with


class ZoneTest(unittest.TestCase):
    def test_zone_2_matches_the_plan(self):
        self.assertEqual(load.zone_bounds(CFG)[1], (132, 144))
        self.assertEqual(load.zone(131, CFG), 1)
        self.assertEqual(load.zone(138, CFG), 2)
        self.assertEqual(load.zone(180, CFG), 5)
        self.assertEqual(load.zone(60, CFG), 0)

    def test_reserve_is_clamped(self):
        self.assertEqual(load.reserve(40, CFG), 0.0)
        self.assertEqual(load.reserve(200, CFG), 1.0)


class WorkoutLoadTest(unittest.TestCase):
    def test_trimp_formula(self):
        bpm = 138  # reserve 0.65, zone 2
        r = (bpm - 60) / 120
        got = load.workout_load({bpm: 3600}, CFG)
        self.assertAlmostEqual(got.aerobic, 60 * r * 0.64 * math.exp(1.92 * r))
        self.assertEqual((got.threshold, got.anaerobic), (0, 0))

    def test_split_by_intensity(self):
        got = load.workout_load({138: 60, 160: 60, 175: 60}, CFG)
        self.assertTrue(got.aerobic > 0 and got.threshold > 0 and got.anaerobic > 0)
        # Harder minutes score more.
        self.assertLess(got.aerobic, got.threshold)
        self.assertLess(got.threshold, got.anaerobic)

    def test_resting_heart_rate_scores_nothing(self):
        self.assertEqual(load.workout_load({60: 3600}, CFG).total, 0)


class SeriesTest(unittest.TestCase):
    start = date(2026, 1, 1)

    def run_series(self, daily, days):
        return load.series(daily, CFG, self.start, self.start + timedelta(days=days - 1))

    def test_steady_training_converges(self):
        daily = {self.start + timedelta(days=i): Load(aerobic=50) for i in range(400)}
        last = self.run_series(daily, 400)[-1]
        self.assertAlmostEqual(last.ltl, 50, delta=0.1)
        self.assertAlmostEqual(last.short_term, 50, delta=0.1)
        self.assertAlmostEqual(last.fitness, 50, delta=0.1)
        self.assertAlmostEqual(last.capacity, 0, delta=0.1)
        self.assertEqual(last.state, "Maintaining")

    def test_fitness_lags_long_term_load(self):
        daily = {self.start + timedelta(days=i): Load(aerobic=50) for i in range(60)}
        days = self.run_series(daily, 60)
        self.assertTrue(all(d.fitness <= d.ltl for d in days))
        # And it is smoother: its day-to-day steps are smaller.
        step = lambda xs: max(abs(b - a) for a, b in zip(xs, xs[1:]))
        self.assertLess(step([d.fitness for d in days]), step([d.ltl for d in days]))

    def test_hard_block_is_fatiguing(self):
        daily = {self.start + timedelta(days=i): Load(aerobic=40) for i in range(200)}
        daily.update({self.start + timedelta(days=i): Load(aerobic=60, threshold=40) for i in range(200, 210)})
        d = self.run_series(daily, 210)[-1]
        self.assertLess(d.capacity, 0)
        self.assertIn(d.state, ("Building", "Overtraining risk"))

    def test_stopping_is_detraining(self):
        daily = {self.start + timedelta(days=i): Load(aerobic=50) for i in range(200)}
        d = self.run_series(daily, 240)[-1]
        # Fresh, but six weeks off: fading fitness outranks feeling rested.
        self.assertGreater(d.capacity, 8)
        self.assertEqual(d.state, "Detraining")

    def test_tuning_changes_the_answer(self):
        daily = {self.start: Load(aerobic=100)}
        fast = load.series(daily, Config(aerobic_days=10), self.start, self.start + timedelta(days=30))[-1]
        slow = load.series(daily, Config(aerobic_days=90), self.start, self.start + timedelta(days=30))[-1]
        self.assertLess(fast.ltl, slow.ltl)

    def test_hrv_adjustment_shifts_capacity(self):
        daily = {self.start: Load(aerobic=50)}
        plain = self.run_series(daily, 1)[-1]
        adj = load.series(daily, CFG, self.start, self.start, {self.start: 12})[-1]
        self.assertAlmostEqual(adj.capacity - plain.capacity, 12)


if __name__ == "__main__":
    unittest.main()
