from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from . import config as config_mod
from . import backup, db, dropbox, healthconnect, load, pacetest, settings, store, takeout, web
from .store import MIN_ACTIVE_MINUTES, is_workout

log = logging.getLogger("heartwatch")


def cmd_import(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    counts: dict[str, int] = {}
    for name, data in store.fit_files([Path(p) for p in args.paths]):
        result = store.import_fit(conn, cfg, name, data)
        key = result.status.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        if result.recording:
            r = result.recording
            print(f"added      {r.start.astimezone():%Y-%m-%d %H:%M}  {r.sport:<8} {name}")
        elif key == "failed":
            print(f"FAILED     {name}: {result.status[len('failed: '):]}")
    print(", ".join(f"{n} {k}" for k, n in counts.items()) or "no .fit files found")


def fmt_change(change: float) -> str:
    return f"{'up' if change >= 0 else 'down'} {abs(round(change * 100))}% from 4 weeks ago"


def cmd_summary(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    ws = store.workouts(conn)
    if not ws:
        print("No workouts imported yet.")
        return
    today = date.today()
    rhr = load.readiness(healthconnect.resting_hr(conn), cfg, ws[0].start.date(), today)
    days = load.series(store.daily_loads(ws, cfg), cfg, ws[0].start.date(), today, {b.day: b.adjust for b in rhr})
    d, h = days[-1], rhr[-1]
    lt = d.long_term
    print(f"Today, {today:%a %b %d %Y}: {d.state}")
    print(f"  Fitness score      {d.fitness:5.0f}   ({fmt_change((d.fitness - days[-29].fitness) / days[-29].fitness if len(days) > 28 and days[-29].fitness else 0)})")
    if h.adjust:
        note = f"resting HR {h.adjust:+.0f}: 7-day {h.week:.0f} bpm, normal {h.base - h.sd:.0f}-{h.base + h.sd:.0f}"
    elif h.baseline_days < load.MIN_BASELINE:
        note = f"no resting-HR adjustment until {load.MIN_BASELINE} days of ring data; {h.baseline_days} so far"
    elif h.week is None:
        note = f"no resting-HR adjustment: under {load.MIN_WEEK} nights of ring data this week"
    else:
        note = "resting HR normal, no adjustment"
    print(f"  Training capacity  {d.capacity:+5.0f}   ({note})")
    print(f"  Long-term load     {d.ltl:5.0f}   = aerobic {lt.aerobic:.0f} + threshold {lt.threshold:.0f}"
          f" + anaerobic {lt.anaerobic:.0f}, {fmt_change(d.change)}")
    print(f"  Short-term load    {d.short_term:5.0f}")

    bounds = load.zone_bounds(cfg)
    print("\nZones: " + "  ".join(f"z{i} {lo}-{hi}" for i, (lo, hi) in enumerate(bounds, 1)))
    z2_lo, z2_hi = bounds[1]

    print(f"\nLast {args.weeks} weeks        workouts   min   aerobic  threshold  anaerobic  zone-2 min   state at week end")
    by_day = {x.day: x for x in days}
    monday = today - timedelta(days=today.weekday())
    for k in range(args.weeks - 1, -1, -1):
        wk = monday - timedelta(weeks=k)
        in_week = [w for w in ws if wk <= w.start.date() < wk + timedelta(days=7)]
        sessions = sum(is_workout(w, cfg) for w in in_week)
        total = load.Load()
        for w in in_week:
            total += load.workout_load(w.hist, cfg)
        z2 = sum(s for w in in_week for b, s in w.hist.items() if z2_lo <= b < z2_hi) / 60
        end = by_day.get(min(wk + timedelta(days=6), today))
        print(f"  week of {wk:%b %d}  {sessions:8d} {sum(w.active_minutes(cfg) for w in in_week):5.0f}"
              f" {total.aerobic:9.0f} {total.threshold:10.0f} {total.anaerobic:10.0f} {z2:11.0f}   {end.state if end else ''}")

    top = max(ws, key=lambda w: w.peak_hr)
    if top.peak_hr > cfg.max_hr:
        print(f"\nHighest heart rate recorded: {top.peak_hr} on {top.start:%Y-%m-%d}, above max_hr = {cfg.max_hr}."
              f" Consider raising max_hr to {top.peak_hr}; that moves every zone up a little.")


def cmd_takeout(cfg: config_mod.Config, args) -> None:
    zips = [Path(p) for p in args.zips]
    samples = takeout.heart_rate(zips)
    if not samples:
        print("No Wahoo heart rate found in these zips.")
        return
    sessions = takeout.sessions(samples, takeout.activities(zips))
    print(f"{len(samples)} heart-rate readings, {len(sessions)} sessions")
    conn = db.connect(cfg.db_path)
    counts: dict[str, int] = {}
    for session in sessions:
        result = store.import_takeout(conn, cfg, session)
        key = result.status.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        if key == "added" and args.verbose:
            print(f"added      {result.name}  ({session.sport})")
    print(", ".join(f"{n} {k}" for k, n in counts.items()))


def cmd_ringconn(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    for path in args.paths:
        p = Path(path)
        try:
            days = store.import_ringconn(conn, cfg, p.name, p.read_bytes())
        except ValueError as e:
            print(f"FAILED     {p.name}: {e}")
            continue
        print(f"{len(days):3d} days   {days[0].day} to {days[-1].day}  {p.name}")


def dropbox_client(cfg: config_mod.Config) -> dropbox.Client:
    if not cfg.dropbox_app_key:
        raise SystemExit("Set dropbox_app_key in the config first (see README).")
    try:
        token = Path(cfg.dropbox_token_file).read_text().strip()
    except FileNotFoundError:
        raise SystemExit(f"No Dropbox token at {cfg.dropbox_token_file}: run `heartwatch dropbox-login` first.")
    return dropbox.Client(cfg.dropbox_app_key, token)


def cmd_dropbox_login(cfg: config_mod.Config, args) -> None:
    if not cfg.dropbox_app_key:
        raise SystemExit("Set dropbox_app_key in the config first (see README).")
    url, verifier = dropbox.login_url(cfg.dropbox_app_key)
    print("1. Open this link, sign in to Dropbox and allow HeartWatch:\n\n   " + url)
    code = input("\n2. Paste the code Dropbox shows you: ")
    token = dropbox.Client(cfg.dropbox_app_key).finish_login(code, verifier)
    dropbox.save_token(cfg.dropbox_token_file, token)
    print(f"Saved. The token is in {cfg.dropbox_token_file}, readable only by its owner.")


def cmd_dropbox_ls(cfg: config_mod.Config, args) -> None:
    """List a Dropbox folder, to find where the Wahoo app puts its files."""
    page = dropbox_client(cfg).rpc("files/list_folder", {"path": args.path})
    for e in page["entries"]:
        print(("folder  " if e[".tag"] == "folder" else "        ") + e["path_display"])
    if page["has_more"]:
        print("  ... and more")


def cmd_poll(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    result = dropbox.poll(conn, cfg, dropbox_client(cfg))
    for name in result.added:
        print(f"added      {name}")
    for line in result.failed:
        print(f"FAILED     {line}")
    log.info("dropbox poll: %d .fit files seen, %d new, %d failed", result.seen, len(result.added), len(result.failed))


def cmd_gaps(cfg: config_mod.Config, args) -> None:
    """Stretches of at least --weeks weeks with no workout, so you can see what the backfill missed."""
    days = sorted({w.start.date() for w in store.workouts(db.connect(cfg.db_path)) if is_workout(w, cfg)})
    if not days:
        print("No workouts imported yet.")
        return
    found = False
    for a, b in zip(days + [date.today()], days[1:] + [date.today()]):
        if (b - a).days >= 7 * args.weeks:
            found = True
            print(f"{a + timedelta(days=1):%Y-%m-%d} to {b - timedelta(days=1):%Y-%m-%d}  ({(b - a).days // 7} weeks)")
    print(f"First workout {days[0]}, {len(days)} days with a workout." + ("" if found else " No gaps."))


def cmd_backup(cfg: config_mod.Config, args) -> None:
    backup.run(cfg)


def cmd_web(cfg: config_mod.Config, args) -> None:
    web.serve(cfg, args.host, args.port)


def cmd_rebuild(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    print(f"rebuilt {store.rebuild_all(conn, cfg)} workouts")
    print(f"reloaded {healthconnect.reload(conn, cfg)} Health Connect payloads")
    print(f"found {pacetest.scan(conn, cfg)} pace tests")


def cmd_workouts(cfg: config_mod.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    ws = [w for w in store.workouts(conn) if args.all or is_workout(w, cfg)][-args.last:]
    print("date              type      min  avg  max  load  aerobic thresh anaer  time in zones 1-5 (min)")
    for w in ws:
        l = load.workout_load(w.hist, cfg)
        zones = [0.0] * 6
        for b, s in w.hist.items():
            zones[load.zone(b, cfg)] += s / 60
        print(f"{w.start:%Y-%m-%d %H:%M}  {w.sport:<8} {w.minutes:4.0f} {w.avg_hr:4.0f} {w.peak_hr:4d} {l.total:5.0f}"
              f" {l.aerobic:8.0f} {l.threshold:6.0f} {l.anaerobic:5.0f}  " + " ".join(f"{z:4.0f}" for z in zones[1:]))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="heartwatch", description="Training load and fitness from heart rate")
    parser.add_argument("--config", help="path to config.toml (defaults built in)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="import .fit files, folders of them, or zips")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("summary", help="today's numbers and recent weeks")
    p.add_argument("--weeks", type=int, default=8)
    p = sub.add_parser("workouts", help="list recent workouts with their load")
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--all", action="store_true", help=f"include strap leftovers under {MIN_ACTIVE_MINUTES} active minutes")
    p = sub.add_parser("takeout", help="backfill Wahoo heart rate from Google Takeout zips")
    p.add_argument("zips", nargs="+")
    p = sub.add_parser("ringconn", help="import RingConn data exports (zip or Vital Signs CSV)")
    p.add_argument("paths", nargs="+")
    sub.add_parser("dropbox-login", help="connect HeartWatch to your Dropbox (once)")
    p = sub.add_parser("dropbox-ls", help="list a Dropbox folder")
    p.add_argument("path", nargs="?", default="")
    sub.add_parser("poll", help="import new workouts from Dropbox")
    p = sub.add_parser("gaps", help="list stretches with no workouts")
    p.add_argument("--weeks", type=int, default=2)
    sub.add_parser("rebuild", help="recompute workouts, pace tests and phone data from the archived files")
    sub.add_parser("backup", help="copy the archive and a database snapshot to backup_target")
    p = sub.add_parser("web", help="serve the dashboard")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(args.config)
    if args.cmd in ("summary", "workouts", "gaps"):
        # Settings saved from the dashboard win over config.toml.
        conn = db.connect(cfg.db_path)
        cfg = settings.effective(cfg, conn)
        conn.close()
    commands = {"import": cmd_import, "summary": cmd_summary, "workouts": cmd_workouts, "takeout": cmd_takeout, "ringconn": cmd_ringconn,
                "dropbox-login": cmd_dropbox_login, "dropbox-ls": cmd_dropbox_ls, "poll": cmd_poll,
                "gaps": cmd_gaps, "rebuild": cmd_rebuild, "backup": cmd_backup, "web": cmd_web}
    commands[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
