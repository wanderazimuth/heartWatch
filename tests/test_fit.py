import io
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from heartwatch import db, fit, store
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)


class ReadTest(unittest.TestCase):
    def test_run(self):
        rec = fit.read(io.BytesIO(write(START, [100] * 30 + [100, 105, 110])))
        self.assertEqual(rec.sport, "run")
        self.assertEqual(rec.start, START)
        self.assertEqual(rec.hr, [(30.0, 100), (31.0, 105), (32.0, 110)])

    def test_strength(self):
        rec = fit.read(io.BytesIO(write(START, [110] * 40, "training", "strength_training")))
        self.assertEqual(rec.sport, "strength")
        self.assertEqual(rec.fit_sport, "training/strength_training")

    def test_unknown_sport_is_other(self):
        self.assertEqual(fit.read(io.BytesIO(write(START, [110] * 40, "generic"))).sport, "other")

    def test_no_heart_rate_fails(self):
        with self.assertRaises(ValueError):
            fit.read(io.BytesIO(write(START, [])))
        with self.assertRaises(ValueError):
            fit.read(io.BytesIO(write(START, [120] * 20)))  # all inside the settling period


class CleanTest(unittest.TestCase):
    def test_settling_period_is_dropped(self):
        # A strap settling in: 38 -> 197 in a few seconds, as seen in real files.
        hr = [(t, b) for t, b in enumerate([38, 85, 158, 197, 197, 120] + [70] * 40)]
        self.assertEqual(max(b for _, b in fit.clean(hr)), 70)

    def test_single_spike_is_dropped_and_the_next_sample_kept(self):
        hr = [(30 + t, b) for t, b in enumerate([140, 141, 190, 142, 143])]
        self.assertEqual([b for _, b in fit.clean(hr)], [140, 141, 142, 143])

    def test_real_change_after_a_dropout_is_kept(self):
        hr = [(30, 120), (31, 121), (60, 160), (61, 161)]
        self.assertEqual([b for _, b in fit.clean(hr)], [120, 121, 160, 161])

    def test_read_cleans(self):
        rec = fit.read(io.BytesIO(write(START, [197] * 5 + [120] * 40)))
        self.assertEqual({b for _, b in rec.hr}, {120})
        self.assertEqual(rec.hr[0][0], 30)


class HistogramTest(unittest.TestCase):
    def test_each_sample_holds_until_the_next(self):
        self.assertEqual(fit.histogram([(0, 120), (1, 120), (3, 130), (4, 130)]), {120: 3.0, 130: 1.0})

    def test_gaps_are_capped(self):
        # A 10-minute dropout counts as 5 seconds, not 10 minutes.
        self.assertEqual(fit.histogram([(0, 120), (600, 125), (601, 125)]), {120: 5.0, 125: 1.0})


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=":memory:", archive_dir=self.tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_import_archives_and_skips_duplicates(self):
        data = write(START, [120] * 91)
        self.assertEqual(store.import_fit(self.conn, self.cfg, "a.fit", data).status, "added")
        self.assertEqual(store.import_fit(self.conn, self.cfg, "renamed.fit", data).status, "duplicate")
        self.assertEqual((Path(self.tmp.name) / "2026" / "a.fit").read_bytes(), data)
        [w] = store.workouts(self.conn)
        self.assertEqual(w.hist, {120: 60.0})
        self.assertEqual(w.sport, "run")

    def test_bad_file_is_reported_not_raised(self):
        result = store.import_fit(self.conn, self.cfg, "bad.fit", b"not a fit file")
        self.assertTrue(result.status.startswith("failed"))
        self.assertEqual(store.workouts(self.conn), [])

    def test_override_wins(self):
        store.import_fit(self.conn, self.cfg, "a.fit", write(START, [120] * 40))
        self.conn.execute("UPDATE workouts SET sport_override = 'strength'")
        self.assertEqual(store.workouts(self.conn)[0].sport, "strength")

    def test_zip_and_folder(self):
        folder = Path(self.tmp.name) / "in"
        folder.mkdir()
        (folder / "one.fit").write_bytes(write(START, [120] * 40))
        (folder / "notes.txt").write_text("ignored")
        with zipfile.ZipFile(folder / "two.zip", "w") as zf:
            zf.writestr("WahooFitness/two.fit", write(START, [130] * 40))
        self.assertEqual(sorted(n for n, _ in store.fit_files([folder])), ["one.fit", "two.fit"])


class OverlapTest(unittest.TestCase):
    """The TICKR X's own recording wraps the app's recording of the same workout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=":memory:", archive_dir=self.tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        # Strap: 10 min warm-up at 100, 20 min at 140, 10 min cool-down at 100.
        self.strap = write(START, [100] * 600 + [140] * 1200 + [100] * 600, "generic")
        # App: the 20-minute run in the middle.
        self.app = write(START + timedelta(minutes=10), [140] * 1200)

    def check(self):
        strap, run = sorted(store.workouts(self.conn), key=lambda w: w.sport)
        self.assertEqual((strap.sport, run.sport), ("other", "run"))
        # The run owns its 20 minutes (less the settling period); the strap keeps only warm-up and cool-down.
        self.assertAlmostEqual(run.minutes, 20 - 0.5, delta=0.1)
        self.assertNotIn(140, strap.hist)
        self.assertAlmostEqual(strap.minutes, 20 - 0.5, delta=0.2)
        # Total time counted once.
        self.assertAlmostEqual(strap.minutes + run.minutes, 40 - 1, delta=0.3)

    def test_strap_first(self):
        store.import_fit(self.conn, self.cfg, "strap.fit", self.strap)
        store.import_fit(self.conn, self.cfg, "app.fit", self.app)
        self.check()

    def test_app_first(self):
        store.import_fit(self.conn, self.cfg, "app.fit", self.app)
        store.import_fit(self.conn, self.cfg, "strap.fit", self.strap)
        self.check()

    def test_rebuild_all_gives_the_same(self):
        store.import_fit(self.conn, self.cfg, "strap.fit", self.strap)
        store.import_fit(self.conn, self.cfg, "app.fit", self.app)
        self.assertEqual(store.rebuild_all(self.conn, self.cfg), 2)
        self.check()


if __name__ == "__main__":
    unittest.main()
