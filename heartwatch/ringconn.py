"""Daily vital signs from a RingConn data export.

The RingConn app exports a zip of CSVs per date range. Only the vital-signs
file is read: one row per day with heart rate, SpO2 and HRV. The ring writes
HRV to the export as a daily average (and min and max), not just overnight.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, fields
from datetime import date
from pathlib import PurePath

VITALS_PREFIX = "vital signs"

# Export column -> Vitals field.
COLUMNS = {
    "avg. heart rate(bpm)": "avg_hr",
    "min. heart rate(bpm)": "min_hr",
    "max. heart rate(bpm)": "max_hr",
    "avg. spo2(%)": "spo2",
    "avg. hrv(ms)": "hrv",
    "min. hrv(ms)": "hrv_min",
    "max. hrv(ms)": "hrv_max",
}


@dataclass
class Vitals:
    day: date
    avg_hr: float | None = None
    min_hr: float | None = None
    max_hr: float | None = None
    spo2: float | None = None
    hrv: float | None = None
    hrv_min: float | None = None
    hrv_max: float | None = None

    def values(self) -> dict[str, float | None]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "day"}


def _number(text: str) -> float | None:
    text = text.strip().rstrip("%").strip()
    if not text or text in ("--", "-"):
        return None
    return float(text)


def parse_csv(text: str) -> list[Vitals]:
    rows = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(rows, [])]
    if "date" not in header or not any(h in COLUMNS for h in header):
        raise ValueError("not a RingConn vital signs file")
    out = []
    for row in rows:
        if not row or not row[0].strip():
            continue
        v = Vitals(date.fromisoformat(row[header.index("date")].strip()))
        for i, h in enumerate(header):
            if h in COLUMNS and i < len(row):
                setattr(v, COLUMNS[h], _number(row[i]))
        # Days the ring wasn't worn come out as a date and empty columns.
        if any(x is not None for x in v.values().values()):
            out.append(v)
    return out


def read(name: str, data: bytes) -> list[Vitals]:
    """The days in an export zip, or in its vital-signs CSV on its own."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.namelist() if PurePath(m).name.lower().startswith(VITALS_PREFIX) and m.lower().endswith(".csv")]
            if not members:
                raise ValueError("no Vital Signs CSV in this zip")
            days = {v.day: v for m in sorted(members) for v in parse_csv(zf.read(m).decode("utf-8-sig"))}
        return sorted(days.values(), key=lambda v: v.day)
    return parse_csv(data.decode("utf-8-sig"))
