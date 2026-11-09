"""Record where every WeGo bus is, every 30 seconds, from WeGo's public GTFS-Realtime feed.

Each row is one GPS report: when the bus reported it (the vehicle's own
timestamp, not when we asked), which bus, which trip and route it was serving,
and where it was. The same report seen on two polls is kept once.
"""

from __future__ import annotations

import csv
import time
import urllib.request
from pathlib import Path

from google.transit import gtfs_realtime_pb2

BASE = "http://transitdata.nashvillemta.org/TMGTFSRealTimeWebService"  # WeGo serves these over http only
POSITIONS = f"{BASE}/vehicle/vehiclepositions.pb"
UA = "wego-on-time (+https://github.com/nathanaelhub/wego-on-time)"
FIELDS = ["ts", "vehicle", "trip_id", "start_date", "route_id", "lat", "lon"]


def fetch(url: str = POSITIONS, timeout: int = 20) -> gtfs_realtime_pb2.FeedMessage:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(r.read())
    return feed


def rows(feed: gtfs_realtime_pb2.FeedMessage) -> list[dict]:
    """Buses serving a trip, with a position and a timestamp."""
    out = []
    for e in feed.entity:
        v = e.vehicle
        if not (e.HasField("vehicle") and v.trip.trip_id and v.timestamp and v.HasField("position")):
            continue
        out.append({
            "ts": int(v.timestamp), "vehicle": v.vehicle.id, "trip_id": v.trip.trip_id,
            "start_date": v.trip.start_date, "route_id": v.trip.route_id,
            "lat": round(v.position.latitude, 6), "lon": round(v.position.longitude, 6),
        })
    return out


def collect(out: Path, hours: float, every: float = 30.0, log=print, fetcher=fetch, clock=time.time,
            sleep=time.sleep) -> dict:
    """Poll for `hours`, appending new reports to a plain CSV that is flushed after every
    poll, so a shift that is cut off keeps everything it saw. Returns counts."""
    out.parent.mkdir(parents=True, exist_ok=True)
    end = clock() + hours * 3600
    seen: set[tuple[str, int]] = set()
    polls = errors = written = 0
    new_file = not out.exists()
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        while clock() < end:
            started = clock()
            polls += 1
            try:
                for r in rows(fetcher()):
                    key = (r["vehicle"], r["ts"])
                    if key not in seen:
                        seen.add(key)
                        w.writerow(r)
                        written += 1
            except Exception as exc:  # a missed poll is a gap, not a reason to stop the shift
                errors += 1
                log(f"poll {polls} failed: {exc!r}")
            f.flush()
            if polls % 20 == 0:
                log(f"{polls} polls, {written} reports, {errors} errors")
            sleep(max(0.0, every - (clock() - started)))
    return {"polls": polls, "errors": errors, "reports": written}
