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
  day / week / month for all workouts, runs or dumbbells.
- **Imports**: every workout with its type (change it when the file got it wrong), and the settings
  (resting and max heart rate, averaging windows). Settings saved here are kept in the database and
  override `config.toml`; "Reset to config.toml" drops them.

**Update** (top right) fetches new workouts from Dropbox right away instead of waiting for the daily
poll. The dashboard has no login: anyone on the network who can reach the port can see your data and
change types and settings.

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
them to HeartWatch on a schedule, straight over your home Wi-Fi.

1. Set `healthconnect_key` in the config to a long random string (for example
   `python3 -c "import secrets; print(secrets.token_urlsafe(18))"`) and restart `heartwatch-web`.
2. Install HC Webhook on the phone. In Health Connect grant it only **Resting heart rate** and
   **Sleep**; it asks for every kind of health data, and needs none of the rest.
3. Add a webhook: URL `http://<pi address>:8081/api/healthconnect?key=<key from step 1>`, format
   JSON. Data types: resting heart rate and sleep. (An `X-HeartWatch-Key` header works too, but
   HC Webhook leaves custom headers off its manual syncs, so the key belongs in the URL.)
4. Sync mode: **Scheduled**, at times you're usually home (say 08:00 and 21:00). Exclude the app
   from battery optimisation so Android doesn't stop it.
5. Once, run a manual sync with a time range of the last 60 days, so HeartWatch has a baseline
   from the start. **Sync Now** and the app's Webhook Log show whether the Pi accepted it.

HeartWatch keeps the ring's own records if other apps write the same type, archives the resting
heart rate and sleep from every payload, and turns the header's "Ring data" date red when nothing
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
