"""The original Fitbit Aria scale, talking to HeartWatch instead of fitbit.com.

Pi-hole points www.fitbit.com at the Pi and Caddy passes /scale/* here. The scale
sends one binary upload (protocol version 3, as worked out by the helvetic project:
https://github.com/micolous/helvetic/blob/master/protocol.md) holding its clock and
up to 17 readings, and expects a reply with the time, the display units and the
people it should recognise. Without a valid reply it keeps the readings and tries
again later, so nothing is lost while this isn't working.

All numbers are little-endian. Body fat comes only for a recognised person: the
scale needs their height, age and sex for it, so guests get weight alone.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Config

log = logging.getLogger("heartwatch")

HEADER = struct.Struct("<LL6s16sLLLL")   # version, battery %, MAC, auth code, firmware, 33, scale clock, count
MEASUREMENT = struct.Struct("<LLLLLLLL")  # 2, impedance, grams, time, user id, fat (‰), covariance, fat again
UNITS = {"lb": 0, "stone": 1, "kg": 2}
SEXES = {"male": 0x02, "female": 0x00, "": 0x34}
TOLERANCE_G = 4000  # it's you if you weigh within this of your last reading
CONFIGURED = 0x32
NO_UPDATE = 3  # never offer firmware: there's none to give, and a bad one would brick the scale


@dataclass
class Reading:
    taken: int  # unix seconds, corrected for the scale's clock
    weight_g: int
    fat_pct: float | None
    user_id: int  # 0: a guest


@dataclass
class Upload:
    mac: str  # "001dc9d20f79"
    battery: int
    firmware: int
    readings: list[Reading]


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM: polynomial 0x1021, starting at 0."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def parse(body: bytes, now: float | None = None) -> Upload:
    """Read an upload. Raises ValueError if it isn't one."""
    now = time.time() if now is None else now
    if len(body) < HEADER.size + 2:
        raise ValueError("too short")
    data, (crc,) = body[:-2], struct.unpack("<H", body[-2:])
    if crc16(data) != crc:
        raise ValueError("bad checksum")
    version, battery, mac, _auth, firmware, _, clock, count = HEADER.unpack_from(data)
    if version != 3:
        raise ValueError(f"protocol version {version}, expected 3")
    if len(data) != HEADER.size + count * MEASUREMENT.size:
        raise ValueError(f"{count} readings don't fit in {len(data)} bytes")
    readings = []
    for i in range(count):
        _, _imp, grams, when, user, fat, _cov, _ = MEASUREMENT.unpack_from(data, HEADER.size + i * MEASUREMENT.size)
        # The scale's clock drifts; its readings are timed by it, so take them relative to it.
        readings.append(Reading(int(now - (clock - when)), grams, fat / 1000 if fat and user else None, user))
    return Upload(mac.hex(), battery, firmware, readings)


def reply(cfg: Config, user_id: int, last_g: int | None, now: float | None = None) -> bytes:
    """The scale's settings: the time, the units and the one person it should recognise."""
    now = time.time() if now is None else now
    body = struct.pack("<LBBBL", int(now), UNITS[cfg.aria_units], CONFIGURED, 0x01, 1)
    lo, hi = (max(0, last_g - TOLERANCE_G), last_g + TOLERANCE_G) if last_g else (0, 0)
    age = max(1, date.fromtimestamp(now).year - cfg.aria_birth_year) if cfg.aria_birth_year else 0
    # 77 bytes: id, 16 blank, name, weight range, age, sex, height, then last weight, fat and time, left 0.
    body += struct.pack("<L16x20sLLLBLLLLLL", user_id, cfg.aria_name.upper()[:20].ljust(20).encode(),
                        lo, hi, age, SEXES[cfg.aria_sex], cfg.aria_height_mm, 0, 0, 0, 0, 0)
    body += struct.pack("<LLL", 0, NO_UPDATE, 0)
    # The last two bytes aren't understood; helvetic sends 0x19 plus 0x4d per person.
    return body + struct.pack("<HH", crc16(body), 0x19 + 0x4D)


def save(conn: sqlite3.Connection, cfg: Config, body: bytes, upload: Upload) -> int:
    """Archive the upload and store its readings. Returns how many were new (the scale resends after a failure)."""
    stamp = int(time.time())
    dest = Path(cfg.archive_dir) / "aria" / f"{datetime.now():%Y}" / f"{stamp}.bin"
    n = 1
    while dest.exists():
        dest, n = dest.with_name(f"{stamp}-{n}.bin"), n + 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    if upload.readings:
        dest.write_bytes(body)
    new = store_readings(conn, upload.readings)
    status = {"battery": upload.battery, "firmware": upload.firmware, "at": int(time.time())}
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('aria_status', ?)", (json.dumps(status),))
    return new


def store_readings(conn: sqlite3.Connection, readings: list[Reading]) -> int:
    now = int(time.time())
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO body (taken, weight_g, fat_pct, source, person, imported) VALUES (?, ?, ?, 'aria', ?, ?)",
            [(r.taken, r.weight_g, r.fat_pct, str(r.user_id) if r.user_id else "guest", now) for r in readings])
        return conn.total_changes - before


def me(conn: sqlite3.Connection) -> tuple[int, int | None]:
    """The scale's number for you (whatever it last recognised you as, else 1) and your last weight."""
    row = conn.execute("SELECT person, weight_g FROM body WHERE source = 'aria' AND person != 'guest'"
                       " ORDER BY taken DESC LIMIT 1").fetchone()
    return (int(row["person"]), row["weight_g"]) if row else (1, None)


def reload(conn: sqlite3.Connection, cfg: Config) -> int:
    """Read every archived upload again, for `heartwatch rebuild`. Returns how many readings were new."""
    new = 0
    for path in sorted((Path(cfg.archive_dir) / "aria").rglob("*.bin")):
        try:
            # Timed against when it arrived, which the file name records.
            new += store_readings(conn, parse(path.read_bytes(), now=float(path.stem.split("-")[0])).readings)
        except ValueError as e:
            log.warning("aria upload %s: %s", path.name, e)
    return new
