"""wego collect | process | site"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from . import arrivals, collect, report, schedule

ROOT = Path(__file__).resolve().parents[2]
EVENTS = ROOT / "data" / "arrivals"
SCHEDULED = ROOT / "data" / "scheduled_trips.json"  # service date -> trips scheduled that day (with timepoints)


def load_schedule(path: str | None) -> schedule.Schedule:
    if path:
        return schedule.load(Path(path))
    tmp = Path(tempfile.mkdtemp()) / "gtfs.zip"
    return schedule.load(schedule.download(tmp))


def process(positions: Path, gtfs: str | None) -> dict:
    sched = load_schedule(gtfs)
    evts = arrivals.events(sched, positions)
    written = arrivals.write(evts, EVENTS)
    counts = json.loads(SCHEDULED.read_text()) if SCHEDULED.exists() else {}
    for day in {e.service_date for e in evts}:
        counts.setdefault(day, len(sched.scheduled_trips(day)))
    SCHEDULED.write_text(json.dumps(dict(sorted(counts.items())), indent=1) + "\n")
    meta = ROOT / "data" / "schedule_version.txt"
    meta.write_text(sched.version + "\n")
    return {"events": len(evts), "files": [p.name for p in written], "schedule": sched.version}


def site(out: Path, gtfs: str | None) -> Path:
    sched = load_schedule(gtfs)
    rows = arrivals.read_events(EVENTS)
    counts = json.loads(SCHEDULED.read_text()) if SCHEDULED.exists() else {}
    days = sorted({r["service_date"] for r in rows})
    seen = len({(r["service_date"], r["trip_id"]) for r in rows})
    planned = sum(counts.get(d, 0) for d in days)
    cov = {"trips_seen": seen, "trips_scheduled": planned, "share": seen / planned if planned else None}
    rep = report.build_report(rows, sched.routes, sched.stops, cov)
    return report.write_site(rep, out, ROOT / "site" / "style.css")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wego")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="record vehicle positions")
    c.add_argument("--hours", type=float, default=5.25)
    c.add_argument("--out", type=Path, default=Path("positions.csv"))
    p = sub.add_parser("process", help="turn positions into timepoint events in data/arrivals")
    p.add_argument("positions", type=Path)
    p.add_argument("--gtfs", help="a local GTFS zip instead of downloading WeGo's")
    s = sub.add_parser("site", help="build the static site")
    s.add_argument("--out", type=Path, default=ROOT / "_site")
    s.add_argument("--gtfs")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        print(collect.collect(a.out, a.hours, log=lambda m: print(m, flush=True)))
    elif a.cmd == "process":
        print(process(a.positions, a.gtfs))
    else:
        print(site(a.out, a.gtfs))


if __name__ == "__main__":
    main()
