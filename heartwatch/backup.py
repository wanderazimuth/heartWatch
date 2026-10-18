"""Nightly copy of the archive and a database snapshot to another machine."""
from __future__ import annotations

import logging
import shlex
import sqlite3
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("heartwatch")

# Where install.sh mounts backup_share (systemd mounts it for the backup and unmounts after).
MOUNT = "/mnt/heartwatch"


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


def mounted(path: str, mounts: str = "/proc/mounts") -> bool:
    """Is a network share mounted at path? An empty mount point would fill the SD card instead."""
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return False
    return any(len(f) > 2 and f[1] == path and f[2] in ("cifs", "smb3", "nfs", "nfs4") for f in map(str.split, lines))


def rsync_commands(cfg: Config, db_copy: Path, target: str) -> list[list[str]]:
    target = target.rstrip("/")
    remote = []
    if ":" in target:
        ssh = f"ssh -i {shlex.quote(cfg.backup_ssh_key)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        remote = ["-e", ssh]
    # No --delete: the archive only ever grows, so a file lost on the Pi stays on the NAS.
    return [["rsync", "-rt", "--partial", *remote, str(Path(cfg.archive_dir)) + "/", target + "/archive/"],
            ["rsync", "-t", *remote, str(db_copy), target + "/heartwatch.db"]]


def run(cfg: Config, mount: str = MOUNT) -> None:
    if cfg.backup_share:
        if not mounted(mount):
            raise SystemExit(f"{cfg.backup_share} is not mounted at {mount}; not copying onto the SD card.")
        target = mount
    elif cfg.backup_target:
        target = cfg.backup_target
    else:
        raise SystemExit("Set backup_share or backup_target in the config first (see README).")
    db_copy = snapshot(cfg)
    for cmd in rsync_commands(cfg, db_copy, target):
        subprocess.run(cmd, check=True, timeout=3600)
    log.info("backup copied to %s", cfg.backup_share or cfg.backup_target)
