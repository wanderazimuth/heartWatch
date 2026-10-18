from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class Config:
    db_path: str = "/var/lib/heartwatch/heartwatch.db"
    # Every imported file is copied here. It is the real database: everything
    # else can be rebuilt from it.
    archive_dir: str = "/var/lib/heartwatch/archive"

    # Dropbox app (read-only, full Dropbox) that reads the folder the Wahoo app shares into.
    dropbox_app_key: str = ""
    dropbox_folder: str = "/Apps/WahooFitness"
    # Written by `heartwatch dropbox-login`, run as the service user.
    dropbox_token_file: str = "/var/lib/heartwatch/dropbox_token"
    # Pinged after a poll that found a new workout; a monitor set to a week's
    # period then alerts you when workouts stop arriving.
    heartbeat_url: str = ""

    # Nightly copy of the archive and a database snapshot, by rsync over ssh,
    # e.g. "heartwatch@nas.local:/volume1/Backup/heartwatch". Empty: no copy.
    backup_target: str = ""
    backup_ssh_key: str = "/var/lib/heartwatch/.ssh/id_ed25519"

    # Heart-rate reserve zones (Karvonen).
    resting_hr: int = 60
    max_hr: int = 180

    # Averaging windows, in days. Fitness score is long-term load averaged again.
    fitness_days: float = 21
    aerobic_days: float = 60
    threshold_days: float = 35
    anaerobic_days: float = 18
    fatigue_days: float = 7

    # Hard work tires you more than its load suggests.
    threshold_fatigue: float = 1.4
    anaerobic_fatigue: float = 2.0


def load(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    config = Config(**raw)
    if not 0 < config.resting_hr < config.max_hr:
        raise ValueError("need 0 < resting_hr < max_hr")
    return config
