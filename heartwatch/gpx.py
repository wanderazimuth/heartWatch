"""Read workouts from GPX files with heart rate, the format Runkeeper exports.

Runkeeper names each file after the local start time (2012-03-05-085059.gpx).
Its older files write local time with a Z as if it were UTC; newer ones are
real UTC. The file name is right either way, so when there is one the track
is moved to start there.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import fit

SPORTS = {"running": "run", "hiking": "hike", "walking": "hike"}

# Phone apps logged the strap every few seconds, not every second. A gap up to
# this long is the same heart rate until the next reading; a longer one is a pause.
FILL_GAP = 30

LOCAL_NAME = re.compile(r"(\d{4}-\d\d-\d\d-\d{6})(?:-\d+)?\.gpx$", re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read(name: str, data: bytes) -> fit.Recording:
    root = ET.fromstring(data)
    label = ""
    points: list[tuple[datetime, int]] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("name", "type") and not label and el.text:
            label = el.text.strip()
        elif tag == "trkpt":
            ts = bpm = None
            for child in el.iter():
                ctag = _local(child.tag)
                if ctag == "time" and child.text:
                    ts = datetime.fromisoformat(child.text.strip().replace("Z", "+00:00"))
                elif ctag == "hr" and child.text:
                    bpm = int(float(child.text))
            if ts is not None and bpm:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                points.append((ts, bpm))
    if len(points) < 2:
        raise ValueError("no heart-rate samples")
    points.sort()
    start = points[0][0]
    m = LOCAL_NAME.search(name)
    if m:
        named = datetime.strptime(m[1], "%Y-%m-%d-%H%M%S").astimezone()  # this machine's time zone
        # Whole quarter hours only: the shift fixes a time zone, it doesn't move the workout.
        shift = round((named - start).total_seconds() / 900) * 900
        start = datetime.fromtimestamp(start.timestamp() + shift, timezone.utc)
    t0 = points[0][0]
    hr = fit.clean([((ts - t0).total_seconds(), bpm) for ts, bpm in points])
    if len(hr) < 2:
        raise ValueError("too short to use")
    word = label.split()[0].lower() if label else ""
    return fit.Recording(start, datetime.fromtimestamp(start.timestamp() + hr[-1][0], timezone.utc),
                         SPORTS.get(word, "other"), label, fill(hr))


def fill(hr: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """One reading a second: each reading holds until the next, across gaps up to FILL_GAP."""
    out: list[tuple[float, int]] = []
    for (t1, bpm), (t2, _) in zip(hr, hr[1:]):
        out.append((t1, bpm))
        if t2 - t1 <= FILL_GAP:
            out.extend((t1 + k, bpm) for k in range(1, int(t2 - t1)))
    out.append(hr[-1])
    return out
