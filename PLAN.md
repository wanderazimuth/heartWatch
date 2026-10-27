# HeartWatch build plan

HeartWatch replaces the paid Wahoo plots with two views, Fitness & Training Capacity and
Training Trends, computed from heart rate alone and served from a Raspberry Pi on the home network.

## How it fits together

```
Wahoo app ──.FIT──▶ Dropbox ──(Pi polls daily, or Update button)──▶ importer ──▶ SQLite ──▶ dashboard :8081
Wahoo export + Google Takeout ──(one-time backfill)─────────▲
RingConn ─▶ Health Connect ─▶ HC Webhook app ─▶ POST /api/healthconnect (daily resting HR, sleep)
RingConn app export (zip, by hand) ─▶ Imports page upload ─▶ HRV chart
```

Layout:

- Python, with its own `heartwatch` system user on the Pi.
- Config in `/etc/heartwatch/config.toml`: Dropbox token, resting and max heart rate, averaging windows.
- Data in `/var/lib/heartwatch/`: the database and the raw file archive.
- systemd units installed by `deploy/install.sh`; `deploy/push.sh` ships the code from a workstation.
- Dashboard on port 8081.

## What gets stored

Raw data plus a little derived data, never finished chart values.

- **Raw file archive**: every .FIT file (and the Takeout extracts), copied to the NAS nightly. This is the
  real database.
- **One row per workout**: start, end, type (run / strength / other), source, source file, and a
  type override set from the Imports page.
- **Heart-rate histogram per workout**: seconds spent at each bpm. Load for any resting or max heart
  rate is recomputed from it in milliseconds, without re-reading files.
- **One row per night of HRV**, once the ring part exists.

Daily load, long-term load, short-term load, fitness score, training capacity and fitness state are
computed when a page loads. Two years of days is trivial work, so there is no cache to go stale.

## The math (`load.py`)

| Step | Definition | Default |
|---|---|---|
| Heart-rate reserve | (HR − resting) / (max − resting) | from the data (`profile.py`), else config |
| Zones | 50/60/70/80/90/100% of reserve; with a threshold test, zones 2–5 at 85/90/95/100% of LTHR | zone 2 = 60–70% of reserve |
| Load | Banister TRIMP per second, split into aerobic (zone 2 and below), threshold (zones 3–4), anaerobic (zone 5) | ~60 per hour in zone 2 |
| Long-term load | EWMA per dimension | 60 / 35 / 18 days |
| Short-term load | EWMA of aerobic + 1.4 × threshold + 2 × anaerobic | 7 days |
| Fitness score | EWMA of total long-term load | 21 days |
| HRV adjustment | 7-day HRV z-score vs 30-day baseline, scaled, capped | ±12 (0 until ring data exists) |
| Training capacity | long-term − short-term + HRV adjustment | |
| Fitness state | rules from the mockup (Overtraining risk below −20, etc.) | |

Every number in the right-hand column comes from config.

## Data sources

- **Wahoo export**: the .FIT files of past workouts.
- **Google Takeout** of Google Fit, for history before the export:
  - `Fit/All Data/raw_com.google.heart_rate.bpm_com.wahoofitness.fitness_*.json`: Wahoo heart rate at
    1-second resolution.
  - `Fit/Activities/*.tcx`: sessions (mostly phone-detected walks) with type and times but **no
    heart rate**. Useful only to label the type of a heart-rate session.
  - Ring heart rate may be in there too, but no HRV: Google Fit has no HRV data type.

## Phases

**0. Setup checks**
- Wahoo export and Google Takeout downloaded.
- Wahoo app: Settings > Authorized apps > Dropbox, auto-share on.
- Strength sessions saved as a strength workout type.
- Wahoo's zones set to the heart-rate-reserve numbers so the live display matches HeartWatch.
- A read-only Dropbox app (steps in README) for HeartWatch (full-Dropbox scope: Wahoo writes to its own app folder).

**1. Core, run on Windows first (done)**
Repo skeleton, FIT reading, histogram, `load.py`, tests against real files.
`py -m heartwatch import some.fit` and `py -m heartwatch summary` show today's numbers.
Goal: decide whether the math looks right on real runs and dumbbell sessions before building more.
Found: the TICKR X keeps its own recording that wraps each app recording, so overlapping recordings are
de-duplicated per second; strap-settling glitches are filtered; max HR is best taken from the data.

**2. Imports (done)**
- Wahoo export / single .FIT import.
- Takeout backfill: split the 1-second Wahoo heart-rate stream into sessions (a gap over 10 minutes
  starts a new one), and label each from the Google Fit activity that overlaps it.
- De-duplication: Wahoo files and Takeout can cover the same workouts. A .FIT file wins over a Takeout
  session that overlaps it in time.
- Dropbox poller using Dropbox's change cursor, so each poll fetches only new files. Runs once a day,
  and on demand from the dashboard's Update button (phase 3) or `heartwatch poll`.
- `heartwatch gaps` lists weeks with no workouts.

**3. Dashboard (done)**
Fitness and Training Trends pages ported from the mockups (plain HTML, SVG and JavaScript, like
NetWatch). An Imports page: list of imported workouts, type fix, settings, and the tuning panel.
An Update button that runs a Dropbox poll and redraws the charts.

**4. Deploy (done)**
Install script, dashboard service, daily import timer (11:00), nightly NAS copy of the archive and a
database snapshot (rsync over ssh, never deleting). Deployed with `deploy/push.sh` from a workstation.
Archive paths are stored relative to `archive_dir`, so the archive and database
can move between machines as they are.

**5. Ring data (done)**
RingConn doesn't share HRV with Health Connect (its own docs say so), so readiness comes from the
ring's daily resting heart rate instead, sent from Health Connect by the HC Webhook app on the phone
to `/api/healthconnect` (shared key in a header). Resting heart rate above your normal range lowers
training capacity, below raises it. HRV from manual RingConn exports is charted but not scored.
The normal range leaves out the current week, so a bad week can't lower it.

**6. Events and the weekly plan (events list done; weekly plan next)**
No race-style peaking: every week has one long, hard weekend day, and a trip (canyon, hike,
snowboard, snowshoe) simply takes its place. About 7 hours a week; Sunday is rest.

- **Events table**: `day`, optional `end_day` for trips of more than one day, `title`,
  `kind` (canyon / hike / snowboard / snowshoe / other), `size` (easy under 3 h / long 3–6 h /
  big over 6 h or several days), optional `climb_m`, `pack_lb`, `notes`, and a `recap`
  (kept up / ok / struggled, plus a note) filled in afterwards. A past date means it happened;
  deleting means cancelled.
- **API**: `GET /api/events`, `POST /api/events` to create, `POST /api/events/<id>` to update or
  delete (`{"deleted": true}`). JSON only, like the other POST routes.
- **Plan tab** (phone-first): this week, Monday to Sunday; upcoming events with a countdown;
  an add form (date, name, kind, size, with "more" for the rest); past events with matched workouts
  and the recap. The Fitness tab gets a one-line "Next: … in N days".
- **Weekly template** (in settings, so sessions and minutes can be changed): Mon easy run 45 min,
  Tue uphill ruck/stairs 60, Wed easy run + strength 60, Thu hill repeats or tempo 45,
  Fri easy 30 or off, Sat long day 2.5–4 h, Sun rest.
- **Rules**, written in code so each day can say why:
  - An event takes the long-day slot. If it isn't on Saturday, Saturday becomes easy or rest.
  - Before an event: two easy days for big (no hard Thursday), one for long, none for easy.
  - After a big event: recovery days until resting heart rate is back in its normal range.
  - Snowboard weeks keep Tuesday's uphill session. The Saturday before a spring canyon or hike can
    run a little longer.

Build order: events table, API and the tab without the weekly plan; then the weekly plan from the
template and rules; then matching past events to that day's workouts and Health Connect sessions,
plus the recap. Scoring hikes done without the strap (ring session times + GPX elevation, with an
effort model calibrated by occasional strap hikes) comes after, so an event's actual load can sit
next to its plan.

## Decisions

- **FIT reading**: `fitdecode` in a venv rather than a home-made parser (compressed timestamps and
  developer fields make a maintained library the safer trade).
- **Dropbox access**: the Dropbox API, rather than NAS-side Dropbox sync, to avoid depending on the NAS.
- **Fitness state driver**: open. Start with long-term load as in the mockup, compare with the fitness
  score on real data after phase 1.
- **Dashboard password**: open. It is health data on an unauthenticated LAN page that guests could open.

## Risks

- **Silent import failures**: an expired token or a Wahoo change would make the charts drift into
  Detraining. Guards: show "last workout imported" on the page, and ping a weekly healthchecks.io check
  from the importer when a poll finds a new workout, with a 7-day period, so you're alerted if
  nothing arrives for about 8 days.
- **History gaps**: weeks with no heart rate read as Detraining; that's honest, but the first months
  after a long gap are not comparable with continuous training. `heartwatch gaps` lists them.
- **Strength scoring**: strength sessions spend much of their time below zone 2, so they score well
  under a run's load per hour. That is the heart rate, not a bug.
