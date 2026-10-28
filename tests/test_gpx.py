import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from heartwatch import db, gpx, store
from heartwatch.config import Config

from fitwriter import write


def make_gpx(start: datetime, readings: list[tuple[int, int | None]], name: str = "Running 3/5/12 8:50 am",
             local_as_utc: bool = False) -> bytes:
    """A Runkeeper-style GPX. local_as_utc writes local time with a Z, as Runkeeper's old files do."""
    def stamp(t: datetime) -> str:
        t = t.astimezone() if local_as_utc else t.astimezone(timezone.utc)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    pts = "".join(
        f'<trkpt lat="40.6" lon="-112.3"><ele>1308.0</ele><time>{stamp(start + timedelta(seconds=s))}</time>'
        + (f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{bpm}</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>"
           if bpm else "") + "</trkpt>\n"
        for s, bpm in readings)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Runkeeper"'
            ' xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
            f"<trk><name><![CDATA[{name}]]></name><trkseg>\n{pts}</trkseg></trk></gpx>").encode()


START = datetime(2012, 3, 5, 8, 50, 59).astimezone()  # local, like Runkeeper's file names
NAME = "2012-03-05-085059.gpx"
EVERY_6S = [(s, 150) for s in range(0, 1500, 6)]  # 25 minutes, a reading every 6 seconds


class ReadTest(unittest.TestCase):
    def test_local_time_written_as_utc(self):
        rec = gpx.read(NAME, make_gpx(START, EVERY_6S, local_as_utc=True))
        self.assertEqual(rec.start, START)
        self.assertEqual(rec.sport, "run")
        self.assertEqual(rec.fit_sport, "Running 3/5/12 8:50 am")

    def test_real_utc(self):
        rec = gpx.read(NAME, make_gpx(START, EVERY_6S))
        self.assertEqual(rec.start, START)

    def test_without_a_runkeeper_name_the_times_are_trusted(self):
        rec = gpx.read("morning.gpx", make_gpx(START, EVERY_6S, name="Hiking"))
        self.assertEqual((rec.start, rec.sport), (START, "hike"))

    def test_sparse_readings_fill_short_gaps_only(self):
        # 6-second readings, then a 2-minute pause, then more.
        rec = gpx.read(NAME, make_gpx(START, EVERY_6S + [(s, 140) for s in range(1620, 1800, 6)]))
        times = [t for t, _ in rec.hr]
        self.assertEqual(times[:3], [30, 31, 32])  # the strap's settling half minute is dropped, as in .fit files
        self.assertFalse(any(1494 < t < 1620 for t in times))
        self.assertEqual(len(times), (1494 - 30 + 1) + (1794 - 1620 + 1))

    def test_no_heart_rate(self):
        with self.assertRaisesRegex(ValueError, "heart-rate"):
            gpx.read(NAME, make_gpx(START, [(s, None) for s in range(0, 600, 5)]))


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=str(Path(self.tmp.name) / "hw.db"), archive_dir=self.tmp.name)
        self.conn = db.connect(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def test_import_and_read_back(self):
        data = make_gpx(START, EVERY_6S, local_as_utc=True)
        self.assertEqual(store.import_file(self.conn, self.cfg, NAME, data).status, "added")
        self.assertEqual(store.import_file(self.conn, self.cfg, NAME, data).status, "duplicate")
        (w,) = store.workouts(self.conn)
        self.assertEqual((w.start, w.sport), (START.replace(tzinfo=None), "run"))
        self.assertAlmostEqual(w.minutes, (1494 - 30) / 60, places=1)  # gaps filled, so the whole run counts
        row = self.conn.execute("SELECT * FROM workouts").fetchone()
        self.assertEqual(row["archive_path"], f"gpx/2012/{NAME}")
        self.assertEqual(len(store.samples(self.cfg, row)), 1494 - 30 + 1)

    def test_no_heart_rate_is_skipped(self):
        got = store.import_file(self.conn, self.cfg, NAME, make_gpx(START, [(s, None) for s in range(0, 600, 5)]))
        self.assertEqual(got.status, "no heart rate")
        self.assertEqual(store.workouts(self.conn), [])

    def test_same_run_already_recorded(self):
        store.import_fit(self.conn, self.cfg, "run.fit", write(START.astimezone(timezone.utc), [150] * 1500))
        got = store.import_file(self.conn, self.cfg, NAME, make_gpx(START, EVERY_6S))
        self.assertEqual(got.status, "duplicate")

    def test_zip_of_gpx_files(self):
        path = Path(self.tmp.name) / "runkeeper.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(NAME, make_gpx(START, EVERY_6S))
            zf.writestr("cardioActivities.csv", "Activity Id,Date\n")
        self.assertEqual([n for n, _ in store.fit_files([path])], [NAME])


if __name__ == "__main__":
    unittest.main()
