import json
import tempfile
import unittest
from datetime import date, datetime, timedelta

from heartwatch import db, healthconnect, movement
from heartwatch.config import Config
from heartwatch.store import Workout

PHONE = "com.android.healthconnect.phone.abc123"
FIT = "com.google.android.apps.fitness"
RING = "com.example.ringconn"
DAY = date(2024, 3, 4)


def steps(origin, start: datetime, seconds: int, count: int) -> dict:
    return {"count": count, "start_time": start.isoformat(), "end_time": (start + timedelta(seconds=seconds)).isoformat(),
            "metadata": {"data_origin": origin}}


def walk(origin, start: datetime, per_minute: list[int]) -> list[dict]:
    """A record per minute, the way the phone writes a walk."""
    return [steps(origin, start + timedelta(minutes=i), 60, n) for i, n in enumerate(per_minute)]


class MovementTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(db_path=":memory:", archive_dir=tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def send(self, records):
        got = healthconnect.receive(self.conn, self.cfg, json.dumps({"steps": records}).encode())
        self.assertEqual(got.steps, len(records))

    def at(self, hour, minute=0):
        return datetime(DAY.year, DAY.month, DAY.day, hour, minute)

    def test_one_source_and_brisk_bursts(self):
        self.send(walk(PHONE, self.at(9), [110, 115, 105, 50])  # a 3-minute brisk walk, then strolling
                  + walk(PHONE, self.at(9, 10), [120, 120])  # a second burst after a pause
                  + walk(FIT, self.at(9), [110, 115, 105, 50])  # the same walking again, from Google Fit
                  + [steps(RING, self.at(9), 1800, 900),  # and the ring's half-hour total
                     steps("com.wahoofitness.fitness", self.at(12), 1800, 999_000)])  # nonsense
        got = movement.daily(self.conn, [])[DAY]
        self.assertEqual((got.steps, got.source, got.bursts), (110 + 115 + 105 + 50 + 240, "phone", 2))
        self.assertAlmostEqual(got.brisk_minutes, 5, delta=0.5)

    def test_a_burst_across_record_edges(self):
        # 110 steps from 9:00:30 to 9:01:30: neither clock minute reaches 100, the minute between them does.
        half = self.at(9) + timedelta(seconds=30)
        self.send([steps(PHONE, half, 30, 55), steps(PHONE, half + timedelta(seconds=30), 30, 55)])
        got = movement.daily(self.conn, [])[DAY]
        self.assertEqual((got.bursts, got.brisk_minutes), (1, 1))

    def test_workouts_and_ring_only_days(self):
        self.send(walk(PHONE, self.at(7), [160] * 30) + [steps(RING, self.at(9) + timedelta(days=1), 1800, 400)])
        run = Workout(1, self.at(7, 2), self.at(7, 28), "run", 160, {150: 26 * 60})
        days = movement.daily(self.conn, [run])
        self.assertEqual((days[DAY].bursts, days[DAY].brisk_minutes), (0, 0))
        self.assertEqual((days[DAY + timedelta(days=1)].steps, days[DAY + timedelta(days=1)].brisk_minutes), (400, None))

    def test_google_fit_when_the_phone_counter_is_missing(self):
        self.send(walk(FIT, self.at(9), [120, 120]))
        self.assertEqual(movement.daily(self.conn, [])[DAY].steps, 240)


if __name__ == "__main__":
    unittest.main()
