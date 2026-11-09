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

NEAR = 100.0  # metres: within this the bus is at or passing the stop
LEAVE = 300.0  # metres: it has only left once it's this far away (GPS and layover spots wobble around NEAR)
DWELL = 25.0  # metres beyond its closest point that still count as "arrived" at the end of a line
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


def passes(d: list[float], start: int = 0) -> list[tuple[int, int, bool]]:
    """Visits to a stop from report `start` on: (first index within NEAR, last index
    within NEAR, whether the bus was then seen beyond LEAVE). Wandering out past NEAR
    and back without reaching LEAVE is the same visit."""
    out, i, n = [], start, len(d)
    while i < n:
        if d[i] >= NEAR:
            i += 1
            continue
        j = k = i
        while i < n and d[i] <= LEAVE:
            if d[i] < NEAR:
                k = i
            i += 1
        out.append((j, k, i < n))
    return out


def timed(pings: list[Ping], d: list[float], visit: tuple[int, int, bool], kind: str) -> int | None:
    """When a visit happened: the arrival (first report at the stop, less half the gap
    before it) or the departure (last report within NEAR, plus half the gap after it)."""
    j, k, left = visit
    if kind == "arr":
        at = [i for i in range(j, k + 1) if d[i] <= min(d[j:k + 1]) + DWELL]
        i = at[0]
        if i == 0 or pings[i].ts - pings[i - 1].ts > MAX_GAP:
            return None
        return round((pings[i - 1].ts + pings[i].ts) / 2)
    if not left or k + 1 >= len(pings) or pings[k + 1].ts - pings[k].ts > MAX_GAP:
        return None  # never saw it leave
    return round((pings[k].ts + pings[k + 1].ts) / 2)


def observe(pings: list[Ping], stop_lat: float, stop_lon: float, start: int, kind: str,
            before: int | None = None) -> tuple[int, int] | None:
    """(observed epoch, index of the visit's last report) for the first visit from report
    `start` on; or, with `before`, the LAST visit that ends before report `before`."""
    d = [metres(p.lat, p.lon, stop_lat, stop_lon) for p in pings]
    visits = passes(d, start)
    if before is not None:
        visits = [v for v in visits if v[1] < before][-1:]
    if not visits:
        return None
    when = timed(pings, d, visits[0], kind)
    return None if when is None else (when, visits[0][1])


def start_visit(pings: list[Ping], sched: Schedule, stops: list[Stop]):
    """The visit to the trip's starting stop that the trip really began from.

    A bus is often labelled with its next trip early: arriving at the end of the
    previous one, idling nearby, or driving empty through downtown on the way to
    the start (passing other timepoints as it goes). The trip begins at the last
    visit to its first stop before the bus is next seen at its second timepoint.

    Returns that visit; None when the bus was never at the first stop (the trip
    began before collection did) or the trip has no first stop; False when it was
    at the first stop but never seen at the second timepoint afterwards.
    """
    if not stops[0].first or len(stops) < 2:
        return None
    lat, lon, _ = sched.stops[stops[0].stop_id]
    nlat, nlon, _ = sched.stops[stops[1].stop_id]
    here = passes([metres(p.lat, p.lon, lat, lon) for p in pings])
    if not here:
        return None
    there = passes([metres(p.lat, p.lon, nlat, nlon) for p in pings])
    for v in there:
        before = [h for h in here if h[1] < v[0]]
        if before:
            return before[-1]
    return False


def trip_events(sched: Schedule, trip_id: str, start_date: str, pings: list[Ping]) -> list[Event]:
    trip = sched.trips.get(trip_id)
    if not trip or not pings:
        return []
    pings = sorted({p.ts: p for p in pings}.values(), key=lambda p: p.ts)
    out, pos = [], 0
    stops = [s for s in trip.stops if s.stop_id in sched.stops]
    if not stops:
        return []
    begin = start_visit(pings, sched, stops)
    if begin is False:
        return []  # at its starting stop but never seen moving on: nothing to score yet
    for s in stops:
        lat, lon, _ = sched.stops[s.stop_id]
        kind = "arr" if s.last and not s.first else "dep"
        if s.first:
            if begin is None:
                continue
            d = [metres(p.lat, p.lon, lat, lon) for p in pings]
            when = timed(pings, d, begin, kind)
            seen = None if when is None else (when, begin[1])
            pos = begin[1]  # nothing before the trip really started counts, timed or not
        else:
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
