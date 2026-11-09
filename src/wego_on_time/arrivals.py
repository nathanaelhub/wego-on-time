"""Turn GPS reports into observed times at timepoints, and compare them with the schedule.

For each trip, the reports are walked in time order and the timepoints in
stop order, so a route that passes the same corner twice is matched to the
right visit. At each timepoint the bus is "at the stop" while it's within
NEAR metres; within that pass:

- at a terminal where the trip ends, the time it ARRIVES (first report at the stop);
- everywhere else, the time it DEPARTS (last report at the stop, plus half the
  gap to the next report), because a bus that's early is supposed to wait at a
  timepoint, and riders care about when it leaves.

A pass seen only through a gap of more than MAX_GAP seconds between reports is
not scored: the bus could have been anywhere in between.
"""

from __future__ import annotations

import csv
import gzip
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .schedule import Schedule, Stop, scheduled_epoch

NEAR = 100.0  # metres: within this the bus is passing the stop
DWELL = 25.0  # metres beyond its closest point that still count as "at the stop"
MAX_GAP = 120  # seconds between reports
MAX_DELAY = 3600  # a "delay" beyond an hour is a mislabelled trip, not lateness


@dataclass
class Ping:
    ts: int
    lat: float
    lon: float
    vehicle: str = ""


@dataclass
class Event:
    service_date: str
    route: str
    trip_id: str
    seq: int
    stop_id: str
    kind: str  # "dep" or "arr"
    sched: int  # epoch seconds
    observed: int
    delay: int  # observed - scheduled, seconds; positive = late
    vehicle: str


def metres(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def observe(pings: list[Ping], stop_lat: float, stop_lon: float, start: int, kind: str) -> tuple[int, int] | None:
    """(observed epoch, index of the last report in the pass), or None if the bus
    wasn't seen passing the stop from report `start` on."""
    d = [metres(p.lat, p.lon, stop_lat, stop_lon) for p in pings]
    j = next((i for i in range(start, len(pings)) if d[i] < NEAR), None)
    if j is None:
        return None
    k = j
    while k + 1 < len(pings) and d[k + 1] < NEAR:
        k += 1
    at = [i for i in range(j, k + 1) if d[i] <= min(d[j:k + 1]) + DWELL]
    if kind == "arr":
        i = at[0]
        if i > 0 and pings[i].ts - pings[i - 1].ts > MAX_GAP:
            return None
        before = pings[i - 1].ts if i > 0 else pings[i].ts
        return round((before + pings[i].ts) / 2) if i > 0 else pings[i].ts, k
    i = at[-1]
    if i + 1 >= len(pings) or pings[i + 1].ts - pings[i].ts > MAX_GAP:
        return None  # never saw it leave
    return round((pings[i].ts + pings[i + 1].ts) / 2), k


def trip_events(sched: Schedule, trip_id: str, start_date: str, pings: list[Ping]) -> list[Event]:
    trip = sched.trips.get(trip_id)
    if not trip or not pings:
        return []
    pings = sorted({p.ts: p for p in pings}.values(), key=lambda p: p.ts)
    out, pos = [], 0
    for s in trip.stops:
        if s.stop_id not in sched.stops:
            continue
        lat, lon, _ = sched.stops[s.stop_id]
        kind = "arr" if s.last and not s.first else "dep"
        seen = observe(pings, lat, lon, pos, kind)
        if seen is None:
            continue
        when, pos = seen
        due = scheduled_epoch(start_date, s.sched)
        if abs(when - due) > MAX_DELAY:
            continue
        out.append(Event(start_date, trip.route, trip_id, s.seq, s.stop_id, kind, due, when, when - due,
                         pings[pos].vehicle))
    return out


def read_positions(path: Path) -> dict[tuple[str, str], list[Ping]]:
    by_trip: dict[tuple[str, str], list[Ping]] = defaultdict(list)
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    by_trip[(r["trip_id"], r["start_date"])].append(
                        Ping(int(r["ts"]), float(r["lat"]), float(r["lon"]), r["vehicle"]))
                except (KeyError, TypeError, ValueError):
                    continue  # a row cut short when a shift was stopped
    except EOFError:
        pass  # a file still being written, or a shift stopped mid-write: keep what was read
    return by_trip


def events(sched: Schedule, positions: Path) -> list[Event]:
    out = []
    for (trip_id, start_date), pings in read_positions(positions).items():
        out += trip_events(sched, trip_id, start_date, pings)
    return out


FIELDS = list(Event.__dataclass_fields__)


def write(evts: list[Event], folder: Path) -> list[Path]:
    """Merge events into one gzipped CSV per service date; the first observation of a
    (trip, stop) wins, so re-processing a shift never double-counts."""
    by_day: dict[str, list[Event]] = defaultdict(list)
    for e in evts:
        by_day[e.service_date].append(e)
    written = []
    folder.mkdir(parents=True, exist_ok=True)
    for day, new in by_day.items():
        path = folder / f"{day[:4]}-{day[4:6]}-{day[6:]}.csv.gz"
        rows = {}
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    rows[(r["trip_id"], r["seq"])] = r
        for e in new:
            rows.setdefault((e.trip_id, str(e.seq)), {k: str(v) for k, v in asdict(e).items()})
        ordered = sorted(rows.values(), key=lambda r: (int(r["sched"]), r["trip_id"], int(r["seq"])))
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(ordered)
        written.append(path)
    return written


def read_events(folder: Path) -> list[dict]:
    out = []
    for path in sorted(folder.glob("*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                for k in ("seq", "sched", "observed", "delay"):
                    r[k] = int(r[k])
                out.append(r)
    return out


__all__ = ["Event", "Ping", "Stop", "events", "observe", "read_events", "trip_events", "write"]
