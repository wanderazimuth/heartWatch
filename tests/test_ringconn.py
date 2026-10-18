import io
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from heartwatch import db, load, ringconn, store
from heartwatch.config import Config

HEADER = ("Date,Avg. Heart Rate(bpm),Min. Heart Rate(bpm),Max. Heart Rate(bpm),Avg. Spo2(%),Min. Spo2(%),"
          "Max. Spo2(%),Avg. HRV(ms),Min. HRV(ms),Max. HRV(ms)\n")
VITALS = HEADER + "2024-03-04,66,48,120,96%,92%,99%,31,11,80\n2024-03-05,,,,,,,,,\n2024-03-06,70,52,130,95%,90%,98%,27,12,70\n"


def export_zip(vitals: str = VITALS) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Vital Signs-Someone-2024-03-01-2024-03-07.csv", "﻿" + vitals)
        zf.writestr("Sleep-Someone-2024-03-01-2024-03-07.csv", "Start Time,End Time\n")
        zf.writestr("Activity-Someone-2024-03-01-2024-03-07.csv", "Date,Steps\n2024-03-04,9000\n")
    return buf.getvalue()


class ParseTest(unittest.TestCase):
    def test_zip(self):
        days = ringconn.read("export.zip", export_zip())
        # The day the ring wasn't worn is left out, and "96%" is a number.
        self.assertEqual([v.day for v in days], [date(2024, 3, 4), date(2024, 3, 6)])
        self.assertEqual((days[0].hrv, days[0].min_hr, days[0].spo2), (31, 48, 96))

    def test_csv_on_its_own(self):
        self.assertEqual(len(ringconn.read("vitals.csv", VITALS.encode())), 2)

    def test_wrong_files(self):
        with self.assertRaises(ValueError):
            ringconn.read("steps.csv", b"Date,Steps\n2024-03-04,9000\n")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other.csv", "x")
        with self.assertRaises(ValueError):
            ringconn.read("other.zip", buf.getvalue())


class ImportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=":memory:", archive_dir=tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_newer_export_wins_and_files_are_archived(self):
        store.import_ringconn(self.conn, self.cfg, "a.zip", export_zip())
        store.import_ringconn(self.conn, self.cfg, "b.zip", export_zip(HEADER + "2024-03-06,70,50,130,95%,90%,98%,29,12,70\n"))
        self.assertEqual(store.hrv_readings(self.conn), {date(2024, 3, 4): 31, date(2024, 3, 6): 29})
        self.assertEqual(sorted(p.name for p in (Path(self.cfg.archive_dir) / "ringconn").iterdir()), ["a.zip", "b.zip"])


class HrvTest(unittest.TestCase):
    start = date(2024, 1, 1)

    def days(self, values):
        return {self.start + timedelta(days=i): v for i, v in enumerate(values)}

    def test_no_adjustment_while_learning(self):
        # 27 days of data: the 7-day week plus 20 days before it, one short of a baseline.
        hrv = load.hrv_series(self.days([30, 34] * 14), Config(), self.start, self.start + timedelta(days=26))
        self.assertEqual(hrv[-1].baseline_days, 20)
        self.assertEqual(hrv[-1].adjust, 0)
        self.assertIsNotNone(hrv[-1].week)

    def test_low_week_takes_away_and_is_capped(self):
        values = [30, 34] * 12 + [20] * 7
        end = self.start + timedelta(days=len(values) - 1)
        h = load.hrv_series(self.days(values), Config(), self.start, end)[-1]
        self.assertLess(h.week, h.base - h.sd)
        self.assertEqual(h.adjust, -12)
        self.assertEqual(load.hrv_series(self.days(values), Config(hrv_adjust_max=0), self.start, end)[-1].adjust, 0)

    def test_normal_week_is_about_zero(self):
        values = [30, 34] * 20
        h = load.hrv_series(self.days(values), Config(), self.start, self.start + timedelta(days=39))[-1]
        self.assertLess(abs(h.adjust), 2)

    def test_missing_days_mean_no_weekly_average(self):
        values = {self.start + timedelta(days=i): 30.0 for i in range(32)}
        h = load.hrv_series(values, Config(), self.start, self.start + timedelta(days=36))[-1]
        self.assertIsNone(h.week)
        self.assertEqual(h.adjust, 0)


if __name__ == "__main__":
    unittest.main()
