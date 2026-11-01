"""Write minimal FIT files for tests, so no real (GPS-carrying) workout files are needed."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SPORTS = {"generic": 0, "running": 1, "training": 10, "walking": 11, "hiking": 17}
SUB_SPORTS = {"generic": 0, "treadmill": 1, "strength_training": 20}

CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc(data: bytes, value: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = CRC_TABLE[value & 0xF]
            value = ((value >> 4) & 0x0FFF) ^ tmp ^ CRC_TABLE[nibble]
    return value


def fit_time(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


def definition(local: int, global_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    out = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
    return out + b"".join(struct.pack("<BBB", *f) for f in fields)


LAP_TRIGGERS = {"manual": 0, "session_end": 7}


def write(start: datetime, bpms: list[int], sport: str = "running", sub_sport: str = "generic",
          step: int = 1, distances: list[float] | None = None,
          laps: list[tuple[int, int, str]] = (), speeds: list[float] | None = None,
          altitudes: list[float] | None = None) -> bytes:
    """A FIT activity: file_id, a session, and one record per bpm, step seconds apart.

    distances (metres, one per record), speeds (m/s, one per record), altitudes
    (metres, one per record) and laps ((start, end) seconds from start, and a
    trigger) are added when given."""
    body = definition(0, 0, [(0, 1, 0x00)]) + struct.pack("<BB", 0, 4)  # file_id: type = activity
    body += definition(1, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (6, 1, 0x00)])
    t0 = fit_time(start)
    body += struct.pack("<BIIBB", 1, t0 + step * len(bpms), t0, SPORTS[sport], SUB_SPORTS[sub_sport])
    # Record fields: (field number, size, base type, struct code, values encoded as stored).
    extra = []
    if distances is not None:
        extra.append((5, 4, 0x86, "I", [round(d * 100) for d in distances]))
    if speeds is not None:
        extra.append((6, 2, 0x84, "H", [round(v * 1000) for v in speeds]))
    if altitudes is not None:
        extra.append((2, 2, 0x84, "H", [round((a + 500) * 5) for a in altitudes]))
    body += definition(2, 20, [(253, 4, 0x86), (3, 1, 0x02)] + [f[:3] for f in extra])
    fmt = "<BIB" + "".join(f[3] for f in extra)
    for i, bpm in enumerate(bpms):
        body += struct.pack(fmt, 2, t0 + i * step, bpm, *(f[4][i] for f in extra))
    if laps:
        body += definition(3, 19, [(253, 4, 0x86), (2, 4, 0x86), (24, 1, 0x00)])
        for a, b, trigger in laps:
            body += struct.pack("<BIIB", 3, t0 + b, t0 + a, LAP_TRIGGERS[trigger])
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", crc(header))
    data = header + body
    return data + struct.pack("<H", crc(data))
