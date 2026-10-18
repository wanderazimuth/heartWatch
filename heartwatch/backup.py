"""Nightly copy of the archive and a database snapshot to another machine."""
from __future__ import annotations

import logging
import shlex
import sqlite3
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("heartwatch")


def snapshot(cfg: Config) -> Path:
    """A consistent copy of the database next to the archive, safe to take while the dashboard runs."""
    dest = Path(cfg.archive_dir).parent / "backup" / "heartwatch.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(cfg.db_path)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


def rsync_commands(cfg: Config, db_copy: Path) -> list[list[str]]:
    ssh = f"ssh -i {shlex.quote(cfg.backup_ssh_key)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    # No --delete: the archive only ever grows, so a file lost on the Pi stays on the NAS.
    target = cfg.backup_target.rstrip("/")
    return [["rsync", "-rt", "--partial", "-e", ssh, str(Path(cfg.archive_dir)) + "/", target + "/archive/"],
            ["rsync", "-t", "-e", ssh, str(db_copy), target + "/heartwatch.db"]]


def run(cfg: Config) -> None:
    if not cfg.backup_target:
        raise SystemExit("Set backup_target in the config first (see README).")
    db_copy = snapshot(cfg)
    for cmd in rsync_commands(cfg, db_copy):
        subprocess.run(cmd, check=True, timeout=3600)
    log.info("backup copied to %s", cfg.backup_target)
