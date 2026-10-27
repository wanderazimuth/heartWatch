import unittest

from heartwatch import db, events


class ValidateTest(unittest.TestCase):
    def test_date_and_title_are_enough(self):
        ev = events.validate({"day": "2026-10-30", "title": "  Canyon  "})
        self.assertEqual((ev["title"], ev["kind"], ev["size"], ev["end_day"]), ("Canyon", "other", "long", None))

    def test_one_day_trip_has_no_end_day(self):
        self.assertIsNone(events.validate({"day": "2026-10-30", "end_day": "2026-10-30", "title": "x"})["end_day"])

    def test_numbers(self):
        ev = events.validate({"day": "2026-10-30", "title": "x", "climb_m": 600.4, "pack_lb": 29.96})
        self.assertEqual((ev["climb_m"], ev["pack_lb"]), (600, 30.0))
        self.assertIsNone(events.validate({"day": "2026-10-30", "title": "x", "climb_m": ""})["climb_m"])
        for bad in ({"climb_m": -5}, {"climb_m": True}, {"climb_m": "600"}, {"pack_lb": 200}):
            with self.assertRaises(ValueError, msg=bad):
                events.validate({"day": "2026-10-30", "title": "x", **bad})

    def test_refused(self):
        for bad in ({"day": "Oct 30", "title": "x"}, {"day": "2026-10-30", "title": "   "},
                    {"day": "2026-10-30", "title": "x", "kind": "ski"}, {"day": "2026-10-30", "title": "x", "recap": "great"},
                    {"day": "2026-10-30", "title": "x", "colour": "red"}, {"day": "2026-10-30", "title": "x" * 101}):
            with self.assertRaises(ValueError, msg=bad):
                events.validate(bad)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_create_update_delete(self):
        ev = events.create(self.conn, {"day": "2026-10-30", "title": "Canyon", "size": "big"})
        self.assertEqual(events.update(self.conn, ev["id"], {"recap": "struggled", "recap_note": "Slow on the climb out"})["size"], "big")
        self.assertEqual(events.all_events(self.conn)[0]["recap"], "struggled")
        self.assertIsNone(events.update(self.conn, ev["id"] + 1, {"title": "y"}))
        # A bad change leaves the event as it was.
        with self.assertRaises(ValueError):
            events.update(self.conn, ev["id"], {"end_day": "2026-10-01"})
        self.assertIsNone(events.all_events(self.conn)[0]["end_day"])
        self.assertTrue(events.delete(self.conn, ev["id"]))
        self.assertFalse(events.delete(self.conn, ev["id"]))
        self.assertEqual(events.all_events(self.conn), [])


if __name__ == "__main__":
    unittest.main()
