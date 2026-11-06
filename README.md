# HeartWatch

Wahoo-style Fitness, Training Capacity and Training Trends charts, computed from heart rate alone
(Wahoo TICKR X recordings), without the Wahoo subscription. See [PLAN.md](PLAN.md) for the design.

## Local use (phase 1)

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e .
copy config.example.toml config.toml     # then point db_path and archive_dir at data\
.venv\Scripts\python -m heartwatch --config config.toml import %USERPROFILE%\Downloads\WahooFitness.zip
.venv\Scripts\python -m heartwatch --config config.toml summary
.venv\Scripts\python -m heartwatch --config config.toml workouts --last 20
```

`import` takes .fit files, folders and zips, and skips files it has already seen. Every imported
file is copied into `archive_dir`.

Nothing is cached: change a setting in `config.toml` (zones, averaging windows) and the next
`summary` recomputes the whole history.

## Dashboard

```
.venv\Scripts\python -m heartwatch --config config.toml web --port 8081
```

Then open http://localhost:8081. Three tabs:

- **Fitness**: fitness score (left axis) and training capacity (right axis) on one chart, the fitness
  state strip along the bottom, and long-term and short-term load on demand.
- **Training Trends**: long-term load split into aerobic, threshold and anaerobic, and load per
  day / week / month for all workouts, runs, strength (dumbbell workouts) or hikes (the Wahoo app's Hiking and Walking).
- **Imports**: every workout with its type (change it when the file got it wrong), and the settings
  (resting and max heart rate, averaging windows). Settings saved here are kept in the database and
  override `config.toml`; "Reset to config.toml" drops them.

**Update** (top right) fetches new workouts from Dropbox right away instead of waiting for the daily
poll. The dashboard has no login: anyone on the network who can reach the port can see your data and
change types and settings.

## On the phone

Away from home the Pi is reached over Tailscale, which the
[piServer](https://github.com/wanderazimuth/piServer) repo sets up: HeartWatch is at
`https://raspberrypi.<tailnet>.ts.net`, for your signed-in devices only. piServer also has the same
address answer at home with Tailscale off (a Pi-hole record and Caddy, using Tailscale's
certificate), so one address works everywhere. Open that in Chrome on the phone and choose
**Add to Home screen > Install**: HeartWatch then opens as its own app.

The app keeps the page and the last data it loaded. When the Pi can't be reached (no signal, or
away from home with Tailscale off) it opens with that saved copy, and a notice says when it was saved; it goes back to
live data on its own. Settings, type changes and Update need the Pi. Installing needs HTTPS, so it
works from the tailnet address, not http://heartwatch.lan.

## Heart rates and zones

Resting, max and threshold heart rate come from your own data where there's enough of it, and
each can be switched back to a fixed number on the Imports page (or in `config.toml`):

- **Resting**: the ring's daily resting heart rate, averaged over the last 30 days (14 readings
  needed).
- **Max**: the highest heart rate held for 5 seconds in any workout in the last 12 months, so a
  one-beat strap glitch doesn't count and max can come down with age. Only a hard effort gets
  near your real max, so after months of easy running it reads low.
- **Threshold (LTHR)**: from a threshold test. Warm up, press lap, run 30 minutes alone as hard as
  you can hold evenly, press lap. The average of the last 20 minutes is your lactate threshold
  heart rate. HeartWatch finds the test by its laps (a manual lap of 25-40 minutes averaging over
  80% of your heart-rate reserve; an easier lap is a pace test, and a lap that hard is never one,
  so a test cut short of 25 minutes is neither) and lists it on Trends. The newest
  one sets zones 2-5 at 85, 90, 95 and 100% of it; zone 1 still starts at half the reserve, and load
  is still scored on the reserve. Mark a lap "Not a test" there to leave it out.

Like any setting, these apply to the whole history: a new test rescores past workouts too. After
an upgrade, `heartwatch rebuild` looks for threshold tests in runs imported before.

## Climbs

Any recording with altitude (the Wahoo app takes it from the phone's barometer, so it works in
canyons where GPS doesn't) is searched for climbs: at least 8 minutes and 60 m of steady climbing,
through short stops and small dips. A stop of 5 minutes or more, or a 15 m descent, ends one.
Anything climbing faster than 1500 m/h or moving at car speed is a lift or a ride and is left out.
Trends lists each climb's rate and heart rate, and charts the heartbeats above resting it took
per 100 m (or 100 ft) climbed. That number hardly depends on how fast you went, so lower is fitter.
Enter the pack weight on a climb, or it takes the pack from that day's trip on the Plan tab.
The summary compares the latest climb with earlier ones carrying the same pack. For a clean
benchmark, climb the same hill for about 20 minutes with the same pack every 3-4 weeks, holding
about 145 bpm: a fixed heart rate keeps the effort the same, since pushing near threshold costs
more beats per metre. Getting fitter then shows as a faster rate at that heart rate. After an
upgrade, `heartwatch rebuild` finds climbs in workouts imported before.

## Recordings left running in the car

End a hike late, after driving off, and the drive would count as part of it. HeartWatch reads the
recording's GPS speed: when it ends with speed above 25 km/h held for 30 seconds and staying at
driving pace, everything from the last standstill before it (you, at the car) is left out. The
archived file isn't changed. The workout list on Imports says "Car ride left out" with the minutes;
tap it to count them after all. `heartwatch rebuild` looks for drives in workouts imported before.

## Backfill from Google Takeout

The Wahoo app wrote its heart rate to Google Fit for years. Export Google Fit with Google Takeout,
then:

```
.venv\Scripts\python -m heartwatch --config config.toml takeout %USERPROFILE%\Downloads\takeout-*.zip
.venv\Scripts\python -m heartwatch --config config.toml gaps
```

The heart-rate stream is split into sessions wherever it pauses for 10 minutes, and each session is
named after the Google Fit activity that overlaps it. Sessions already covered by a .fit file are
skipped, and a .fit file imported later takes over any seconds it shares with a Takeout session.

## Backfill from Runkeeper (GPX)

`import` also takes .gpx files, such as Runkeeper's account export (a zip of them):

```
.venv\Scripts\python -m heartwatch --config config.toml import %USERPROFILE%\Downloads\runkeeper-data-export-*.zip
```

Only tracks with heart rate are imported; the rest are counted as "no heart rate". The workout type
comes from the track name (Running, Hiking, Walking). Runkeeper's older files write local time
marked as UTC, so the start time is taken from the file name (2012-03-05-085059.gpx), which is
local either way. Phone apps logged the strap every few seconds: each reading holds until the next
across gaps of up to 30 seconds, and a longer gap is a pause. Like everything else, old workouts
are scored with today's resting and max heart rate.

## Dropbox

The Wahoo app can share every workout's .fit file to Dropbox (Settings > Authorized apps > Dropbox).
HeartWatch reads them from there once a day, and on demand with `heartwatch poll`.

1. At https://www.dropbox.com/developers/apps choose **Create app**, **Scoped access**,
   **Full Dropbox** (the Wahoo app writes into its own app folder, which a folder-only app can't
   see), and any name.
2. On the app's **Permissions** tab tick `files.metadata.read` and `files.content.read`, and submit.
   Nothing else: HeartWatch only ever reads.
3. Copy the **App key** from the Settings tab into `dropbox_app_key` in the config. No app secret
   is needed.
4. Run `heartwatch dropbox-login` (on the Pi: `ssh -t pi@raspberrypi.local sudo -u heartwatch
   /opt/heartwatch/.venv/bin/heartwatch --config /etc/heartwatch/config.toml dropbox-login`), open
   the link, allow access and paste back the code. The refresh token is saved to
   `dropbox_token_file`, readable only by its owner.
5. `heartwatch dropbox-ls /Apps` shows where the Wahoo files are; set `dropbox_folder` if it isn't
   `/Apps/WahooFitness`. Then `heartwatch poll`.

## Ring data from the phone (Health Connect)

The RingConn app writes a daily resting heart rate and sleep sessions into Android Health Connect.
[HC Webhook](https://github.com/mcnaveen/health-connect-webhook), an open-source Android app, sends
them to HeartWatch on a schedule.

1. Set `healthconnect_key` in the config to a long random string (for example
   `python3 -c "import secrets; print(secrets.token_urlsafe(18))"`) and restart `heartwatch-web`.
2. Install HC Webhook on the phone. In Health Connect grant it only **Resting heart rate**,
   **Sleep** and **Steps**; it asks for every kind of health data, and needs none of the rest.
3. Add a webhook: URL `https://raspberrypi.<tailnet>.ts.net/api/healthconnect?key=<key from step 1>`
   (the tailnet address from [On the phone](#on-the-phone)), format JSON. That address works at
   home and, with Tailscale connected, away from home too, so the ring data keeps arriving on
   trips, and over HTTPS the key in the URL isn't sent in the clear. On a Pi without piServer's
   setup, `http://<pi address>:8081/...` works on home Wi-Fi only.

   Data types: resting heart rate, sleep and steps, with sleep and steps detail set to **full**
   (without it sleep stages are left off, so broken nights read long, and steps arrive as
   daily totals, too coarse to find brisk walking in). (An `X-HeartWatch-Key` header works too, but
   HC Webhook leaves custom headers off its manual syncs, so the key belongs in the URL.)
4. Sync mode: **Scheduled**, say 08:00 and 21:00 (with the home address only, pick times you're
   usually home). Exclude the app from battery optimisation so Android doesn't stop it.
5. Once, run a manual sync with a time range of the last 60 days, so HeartWatch has a baseline
   from the start. **Sync Now** and the app's Webhook Log show whether the Pi accepted it.

HeartWatch keeps the ring's own records if other apps write the same type, archives the resting
heart rate, sleep and steps from every payload, and turns the header's "Ring data" date red when nothing
has arrived for 3 days. To keep everything the phone sends (for uses beyond HeartWatch), set
`healthconnect_keep_all = true`: whole payloads then go to `archive/healthconnect-all`, and into
the backup with the rest of the archive. Payloads over 20 MB are refused (413 in the app's log);
sync shorter time ranges if full-detail heart rate makes them that big.

**Readiness.** The 7-day average resting heart rate is compared with your normal range: the
average of the 30 days before the current week, plus or minus one standard deviation. Once 21 of
those 30 days have a reading, training capacity moves by up to `readiness_adjust_max` points
(default 12, 0 turns it off): a week two standard deviations above normal takes the full amount
away, two below adds it.

## RingConn exports (HRV, by hand)

RingConn doesn't share HRV with Health Connect, so HRV only comes from the app's manual data export.
Import the zip with **Import an export…** on the Imports page or:

```
.venv\Scripts\python -m heartwatch --config config.toml ringconn "Data Export-*.zip"
```

HeartWatch reads the Vital Signs file (daily heart rate, SpO2 and HRV) and charts HRV the same way
as resting heart rate, but it doesn't change training capacity: the export's HRV is a whole-day
average, which workouts and stressful days pull down too. Exports can overlap; a later export of
the same day replaces it.

## Fitbit Aria scale

The original Fitbit Aria (first generation) can't be set up with today's Fitbit app, but it keeps
uploading to `www.fitbit.com` over plain HTTP. Point that name at the Pi (a Local DNS record in
Pi-hole, and a Caddy block passing `/scale/*` to port 8081; the piServer repo does both) and set
`aria_mac` and the other `aria_` settings in the config. HeartWatch then answers the scale the way
fitbit.com did, using the protocol the [helvetic](https://github.com/micolous/helvetic) project
worked out:

- Every upload is kept under `archive/aria` and its readings go into the database; `heartwatch
  rebuild` reads them again. Only your scale's MAC is accepted, in a message with a valid checksum.
- The reply sets the scale's clock and units and tells it who you are (`aria_name`, height, birth
  year, sex), so it recognises you by weight, within 4 kg of your last reading, and estimates body
  fat. Anyone else is a guest: weight only, kept apart.
- It never offers the scale a firmware update.
- The scale keeps readings it couldn't upload and sends them later, so nothing is lost while the Pi
  is down.

## Weight

The Fitness tab charts weight and body fat: each weigh-in, the 2-week average, and a Weight tile.
Besides the Aria scale, `takeout` reads the weights you weighed into apps that wrote to Google Fit
(Monitor Your Weight, MyFitnessPal, Google Fit's own entries), and `import` reads Runkeeper's
`measurements.csv` (weight and body fat) from its export zip. Profile weights that the Wahoo app and
others copy into every workout are left out. The readings taken from exports are kept under
`archive/body`. Weight is shown, not scored: load and fitness come from heart rate alone.

## On the Pi

From Git Bash on Windows, commit, then:

```
bash deploy/push.sh pi@raspberrypi.local   # copies the committed code to /opt/heartwatch and runs deploy/install.sh
```

`install.sh` creates a `heartwatch` system user, `/etc/heartwatch/config.toml` (from
`config.example.toml`, first time only) and `/var/lib/heartwatch` for the database, archive and
Dropbox token (readable by that user only), and installs:

| unit | what |
|---|---|
| `heartwatch-web` | the dashboard on port 8081 |
| `heartwatch-poll.timer` | the Dropbox import, daily at 11:00 (skips itself until Dropbox is connected) |
| `heartwatch-backup.timer` | the NAS copy at 02:30, enabled once `backup_share` or `backup_target` is set |

Commands on the Pi run as the service user:

```
sudo -u heartwatch /opt/heartwatch/.venv/bin/heartwatch --config /etc/heartwatch/config.toml summary
```

After editing `/etc/heartwatch/config.toml`, `sudo systemctl restart heartwatch-web` (and rerun
`install.sh` if you change `backup_share` or `backup_target`).

### NAS backup

Each night `heartwatch backup` copies the archive (every .fit and Takeout file) and a snapshot of the
database to the NAS with rsync. Nothing is ever deleted on the NAS. The archive is what matters: the
database can be rebuilt from it, except for types you fixed and saved settings, which the snapshot
keeps.

With a shared folder (the simplest, and no admin login on the NAS):

1. On the NAS, create a shared folder (say `heartwatch`) and a regular, non-admin user for the Pi
   with read/write on that folder only. SMB (Windows file sharing) must be on.
2. Set `backup_share = "//<nas address>/heartwatch"` in `/etc/heartwatch/config.toml` and rerun
   `install.sh`. It makes `/etc/heartwatch/nas-credentials` (root only); put the NAS user's name and
   password in it: `sudo nano /etc/heartwatch/nas-credentials`.
3. `sudo systemctl start heartwatch-backup` and check `journalctl -u heartwatch-backup`.

systemd mounts the folder at `/mnt/heartwatch` only while the backup runs. If the mount fails the
backup fails too, rather than copying onto the SD card.

Or over ssh, if your NAS lets a regular user log in with a key: set `backup_target =
"user@<nas address>:/path/to/folder"` and add the key `install.sh` printed
(`/var/lib/heartwatch/.ssh/id_ed25519.pub`) to that user's `~/.ssh/authorized_keys`.

## Tests

```
.venv\Scripts\python -m unittest discover -s tests
```

The tests build their own small FIT files (`tests/fitwriter.py`), so no real workout files are
needed. Real files carry GPS tracks, and `.gitignore` keeps them, the database and `config.toml`
out of git.
