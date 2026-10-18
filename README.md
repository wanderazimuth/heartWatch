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
| `heartwatch-backup.timer` | the NAS copy at 02:30, enabled once `backup_target` is set |

Commands on the Pi run as the service user:

```
sudo -u heartwatch /opt/heartwatch/.venv/bin/heartwatch --config /etc/heartwatch/config.toml summary
```

After editing `/etc/heartwatch/config.toml`, `sudo systemctl restart heartwatch-web` (and rerun
`install.sh` if you set `backup_target`).

### NAS backup

Each night `heartwatch backup` copies the archive (every .fit and Takeout file) and a snapshot of the
database to `backup_target` with rsync over ssh. Nothing is ever deleted on the NAS. The archive is
what matters: the database can be rebuilt from it, except for types you fixed and saved settings,
which the snapshot keeps.

1. On the NAS, make a shared folder (say `Backup/heartwatch`) and an account that can ssh in and
   write to it. The NAS needs SSH enabled and rsync installed.
2. Add the public key `install.sh` printed (`/var/lib/heartwatch/.ssh/id_ed25519.pub`) to that
   account's `~/.ssh/authorized_keys`.
3. Set `backup_target = "user@nas.local:/volume1/Backup/heartwatch"`, rerun `install.sh`, then
   `sudo systemctl start heartwatch-backup` and check `journalctl -u heartwatch-backup`.

## Tests

```
.venv\Scripts\python -m unittest discover -s tests
```

The tests build their own small FIT files (`tests/fitwriter.py`), so no real workout files are
needed. Real files carry GPS tracks, and `.gitignore` keeps them, the database and `config.toml`
out of git.
