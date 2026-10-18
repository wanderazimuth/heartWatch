import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from heartwatch import db, ringconn, store
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


if __name__ == "__main__":
    unittest.main()
