import unittest
from datetime import date, timedelta

from heartwatch import db, week

MON = date(2026, 10, 26)
TUE = MON + timedelta(days=1)
TMPL = week.DEFAULT_TEMPLATE


def ev(day, size="big", kind="canyon", title="Canyon", end_day=None, id=1):
    return {"id": id, "day": day, "end_day": end_day, "title": title, "kind": kind, "size": size}


def plan(events, today=TUE, high=None, start=MON):
    return {d.day.weekday(): d for d in week.week(start, events, TMPL, today, high or {})}


class WeekTest(unittest.TestCase):
    def test_no_events_is_the_template(self):
        days = plan([])
        self.assertEqual([(d.session, d.minutes) for d in days.values()], [(t["session"], t["minutes"]) for t in TMPL])
        self.assertTrue(all(not d.why for d in days.values()))

    def test_friday_canyon(self):
        days = plan([ev("2026-10-30")])
        self.assertEqual((days[2].session, days[2].minutes), ("easy", 45))  # Wednesday, two days before
        self.assertEqual((days[3].session, days[3].minutes), ("short", 30))  # Thursday, the day before
        self.assertIn("Fresh legs", days[3].why[0])
        self.assertEqual((days[4].session, days[4].event["title"]), ("event", "Canyon"))
        # The canyon is the week's long day, and Saturday is right after it.
        self.assertEqual((days[5].session, days[5].why), ("rest", ["Canyon is this week's long day."]))
        self.assertEqual(days[1].session, "uphill")

    def test_midweek_long_hike(self):
        days = plan([ev("2026-10-28", size="long", kind="hike", title="Hike")])
        self.assertEqual(days[1].session, "easy")  # the day before
        self.assertEqual(days[3].session, "easy")  # the day after
        self.assertIn("Easy after Hike", days[3].why[0])
        self.assertEqual((days[5].session, days[5].minutes), ("easy", 45))  # not beside it, so an easy day

    def test_easy_event_changes_only_the_long_day(self):
        days = plan([ev("2026-10-31", size="easy", kind="snowboard", title="Snowboard")])
        self.assertEqual(days[3].session, "hard")
        self.assertEqual(days[5].session, "event")
        self.assertIn("doesn't train climbing", days[1].why[0])

    def test_trip_of_several_days(self):
        days = plan([ev("2026-10-31", end_day="2026-11-01")])
        self.assertEqual((days[5].session, days[6].session), ("event", "event"))
        nxt = plan([ev("2026-10-31", end_day="2026-11-01")], start=MON + timedelta(days=7))
        self.assertEqual(nxt[0].session, "recovery")
        self.assertEqual(nxt[1].session, "easy")  # Tuesday: easy unless resting heart rate is back to normal

    def test_event_early_next_week_eases_this_weekend(self):
        days = plan([ev("2026-11-02")])
        self.assertEqual(days[5].session, "easy")  # two days before; not this week's event, so no long-day rule
        self.assertIn("in 2 days", days[5].why[0])

    def test_recovery_follows_resting_heart_rate(self):
        # A big Saturday last week; resting heart rate is still up on Tuesday.
        events = [ev("2026-10-24")]
        days = plan(events, high={TUE: (60.0, 50.0, 52.0)})
        self.assertEqual(days[0].session, "easy")  # Monday was fine
        self.assertEqual(days[1].session, "recovery")
        self.assertIn("resting heart rate 60, above your normal 50–52", days[1].why[0])
        self.assertEqual(plan(events)[1].session, "uphill")

    def test_high_rhr_today_is_a_note(self):
        days = plan([], today=MON + timedelta(days=3), high={MON + timedelta(days=3): (60.0, 50.0, 52.0)})
        self.assertEqual(days[3].session, "hard")
        self.assertIn("go easy if you feel flat", days[3].why[0])

    def test_building_toward_a_big_canyon(self):
        days = plan([ev("2026-11-07")])
        self.assertEqual(days[5].minutes, 150 + week.BUILD_MINUTES)
        self.assertEqual(plan([ev("2026-11-07", kind="snowshoe")])[5].minutes, 150)

    def test_high_rhr_from_readings(self):
        rhr = {MON - timedelta(days=i): 50.0 + i % 3 for i in range(1, 45)}
        rhr[TUE] = 60.0
        high = week.high_rhr(rhr, MON, TUE)
        self.assertEqual(list(high), [TUE])
        self.assertEqual(high[TUE][0], 60.0)


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_save_and_reset(self):
        days = [dict(t) for t in TMPL]
        days[4] = {"session": "rest", "minutes": 30}
        self.assertEqual(week.save_template(self.conn, days)[4], {"session": "rest", "minutes": 0})
        self.assertEqual(week.template(self.conn)[4]["session"], "rest")
        self.assertEqual(week.save_template(self.conn, None), TMPL)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM meta WHERE key = 'week_template'").fetchone())

    def test_refused(self):
        for bad in (TMPL[:6], [{"session": "swim", "minutes": 30}] * 7, [{"session": "easy", "minutes": 999}] * 7,
                    [{"session": "easy", "minutes": "45"}] * 7, "easy"):
            with self.assertRaises(ValueError, msg=bad):
                week.validate_template(bad)


if __name__ == "__main__":
    unittest.main()
