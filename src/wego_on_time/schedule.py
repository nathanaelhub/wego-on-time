"""WeGo's published schedule (static GTFS): the timepoints each trip is scheduled to hit, and when.

On-time performance is measured at timepoints (stop_times.timepoint = 1), the
stops with real scheduled times. Times at other stops are interpolated by the
scheduling software and aren't promises, so they aren't scored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GTFS_URL = "https://www.wegotransit.com/GoogleExport/google_transit.zip"
TZ = ZoneInfo("America/Chicago")
UA = "wego-on-time (+https://github.com/nathanaelhub/wego-on-time)"


@dataclass
class Stop:
    seq: int
    stop_id: str
    sched: int  # seconds after the service day's reference time (GTFS: noon minus 12h)
    first: bool = False
    last: bool = False


@dataclass
class Trip:
    trip_id: str
    route: str
    headsign: str
    stops: list[Stop] = field(default_factory=list)
    service_id: str = ""


@dataclass
class Schedule:
    version: str
    stops: dict[str, tuple[float, float, str]]  # stop_id -> (lat, lon, name)
    routes: dict[str, str]  # route_id -> "52 Nolensville Pike"
    trips: dict[str, Trip]
    calendar: dict[str, tuple[set[int], str, str]] = field(default_factory=dict)  # service_id -> (weekdays, start, end)
    exceptions: dict[tuple[str, str], int] = field(default_factory=dict)  # (service_id, yyyymmdd) -> 1 added / 2 removed

    def runs_on(self, service_id: str, day: str) -> bool:
        ex = self.exceptions.get((service_id, day))
        if ex:
            return ex == 1
        cal = self.calendar.get(service_id)
        if not cal:
            return False
        weekdays, start, end = cal
        return start <= day <= end and datetime.strptime(day, "%Y%m%d").weekday() in weekdays

    def scheduled_trips(self, day: str) -> list[Trip]:
        return [t for t in self.trips.values() if t.stops and self.runs_on(t.service_id, day)]


def nice(name: str) -> str:
    """'12TH AVENUE SOUTH' -> '12th Avenue South'."""
    return " ".join(w.lower() if w[:1].isdigit() else w.capitalize() for w in name.strip().split())


def download(dest: Path, url: str = GTFS_URL) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        dest.write_bytes(r.read())
    return dest


def seconds(hms: str) -> int:
    """'25:10:00' or ' 5:22:00' -> seconds; GTFS times run past 24:00 for late trips."""
    h, m, s = (int(x) for x in hms.strip().split(":"))
    return h * 3600 + m * 60 + s


def service_start(day: date) -> datetime:
    """GTFS times count from noon minus 12 hours, which is midnight except on DST change days."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=TZ) - timedelta(hours=12)


def scheduled_epoch(start_date: str, sched: int) -> int:
    day = datetime.strptime(start_date, "%Y%m%d").date()
    return int((service_start(day) + timedelta(seconds=sched)).timestamp())


def load(zip_path: Path) -> Schedule:
    raw = zip_path.read_bytes()
    z = zipfile.ZipFile(io.BytesIO(raw))

    def table(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    stops = {r["stop_id"].strip(): (float(r["stop_lat"]), float(r["stop_lon"]), r["stop_name"].strip())
             for r in table("stops.txt")}
    routes = {r["route_id"].strip(): f'{r["route_short_name"].strip()} {nice(r["route_long_name"])}'
              for r in table("routes.txt")}
    trips = {r["trip_id"].strip(): Trip(r["trip_id"].strip(), r["route_id"].strip(), r["trip_headsign"].strip(),
                                        service_id=r["service_id"].strip())
             for r in table("trips.txt")}
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    calendar = {r["service_id"].strip(): ({i for i, d in enumerate(days) if r[d].strip() == "1"},
                                          r["start_date"].strip(), r["end_date"].strip())
                for r in table("calendar.txt")}
    exceptions = {(r["service_id"].strip(), r["date"].strip()): int(r["exception_type"])
                  for r in table("calendar_dates.txt")} if "calendar_dates.txt" in z.namelist() else {}
    last_seq: dict[str, int] = {}
    for r in table("stop_times.txt"):
        tid, seq = r["trip_id"].strip(), int(r["stop_sequence"])
        last_seq[tid] = max(seq, last_seq.get(tid, 0))
        if r.get("timepoint", "").strip() == "1" and tid in trips:
            t = r["departure_time"].strip() or r["arrival_time"].strip()
            trips[tid].stops.append(Stop(seq, r["stop_id"].strip(), seconds(t)))
    for tid, t in trips.items():
        t.stops.sort(key=lambda s: s.seq)
        if t.stops:
            t.stops[0].first = t.stops[0].seq == 1
            t.stops[-1].last = t.stops[-1].seq == last_seq.get(tid)
    return Schedule(hashlib.sha256(raw).hexdigest()[:12], stops, routes, trips, calendar, exceptions)
