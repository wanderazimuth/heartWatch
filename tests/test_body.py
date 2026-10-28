import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from heartwatch import body, db
from heartwatch.config import Config

T0 = int(datetime(2016, 5, 2, 7, 0).timestamp())


def fit_points(readings):
    return json.dumps({"Data Points": [{"fitValue": [{"value": {"fpVal": v}}], "startTimeNanos": str(t * 10**9)}
                                       for t, v in readings]})


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = Config(db_path=str(self.dir / "hw.db"), archive_dir=str(self.dir / "archive"))
        self.conn = db.connect(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def test_takeout_keeps_weighed_in_apps_only(self):
        z = self.dir / "takeout.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(body.TAKEOUT_PREFIX + "weight_monitoryourweight.bustan.net_.json", fit_points([(T0, 80.25), (T0 + 86400, 80.0)]))
            zf.writestr(body.TAKEOUT_PREFIX + "weight_com.google.android.apps.fitness_user_input.json", fit_points([(T0 + 3600, 81.0)]))
            # Profile weights copied into every workout, and Google's merge of everything: not weigh-ins.
            zf.writestr(body.TAKEOUT_PREFIX + "weight_com.wahoofitness.fitness_health_platform.json", fit_points([(T0, 86.2)]))
            zf.writestr("Takeout/Fit/All Data/derived_com.google.weight_com.google.android.gms_merge_weight.json", fit_points([(T0, 86.2)]))
        self.assertEqual(body.import_takeout(self.conn, self.cfg, [z]), 3)
        self.assertEqual(body.import_takeout(self.conn, self.cfg, [z]), 0)  # already there
        rows = self.conn.execute("SELECT source, weight_g FROM body ORDER BY taken").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("monitoryourweight", 80250), ("googlefit", 81000), ("monitoryourweight", 80000)])
        self.assertTrue((self.dir / "archive" / "body" / "monitoryourweight.json").exists())

    def test_runkeeper_measurements(self):
        z = self.dir / "runkeeper.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("measurements.csv", "Date,Type,Value\n2011-12-02 00:00:00,weight,75.7499\n"
                        "2012-01-16 00:00:00,weight,77.4736\n2011-12-02 00:00:00,fat_percent,22.0\n")
        self.assertEqual(body.import_runkeeper(self.conn, self.cfg, z), 2)
        self.assertEqual(body.daily(self.conn)[date(2011, 12, 2)], (75.75, 22.0))
        self.assertEqual(body.daily(self.conn)[date(2012, 1, 16)], (77.474, None))

    def test_a_zip_without_measurements(self):
        z = self.dir / "other.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("run.gpx", "<gpx/>")
        self.assertEqual(body.import_runkeeper(self.conn, self.cfg, z), 0)

    def test_rebuild_reads_the_archive_again(self):
        body.keep(self.cfg, "myfitnesspal", [(T0, 80000, None)])
        self.assertEqual(body.reload(self.conn, self.cfg), 1)
        self.assertEqual(body.reload(self.conn, self.cfg), 0)


class SeriesTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_columns_in_pounds_with_a_two_week_average(self):
        start = date(2016, 5, 1)
        at = lambda days: int(datetime.combine(start + timedelta(days), datetime.min.time()).timestamp()) + 7 * 3600
        body.store(self.conn, "aria", [(at(1), 90000, 25.0)], person="1495412")
        body.store(self.conn, "aria", [(at(3), 88000, None)], person="1495412")
        body.store(self.conn, "aria", [(at(3), 60000, None)], person="guest")  # someone else
        got = body.series_json(self.conn, Config(aria_units="lb"), start, start + timedelta(20))
        self.assertEqual(got["weight"][:4], [None, 198.4, None, 194.0])
        self.assertEqual(got["weight_trend"][:4], [None, 198.4, 198.4, 196.2])
        self.assertIsNone(got["weight_trend"][17])  # over two weeks since the last weigh-in
        self.assertEqual(got["fat"][1], 25.0)
        self.assertEqual(got["last_weight"], {"day": "2016-05-04", "weight": 194.0, "fat": None})
        self.assertEqual(body.series_json(self.conn, Config(aria_units="kg"), start, start)["weight_unit"], "kg")

    def test_nothing_yet(self):
        got = body.series_json(self.conn, Config(), date(2026, 1, 1), date(2026, 1, 3))
        self.assertEqual((got["weight"], got["last_weight"]), ([None] * 3, None))


if __name__ == "__main__":
    unittest.main()
