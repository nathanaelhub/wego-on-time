"""The summaries and the page, on rows where the answer is known."""

from pathlib import Path

from wego_on_time.collect import collect, rows
from wego_on_time.report import build_report, page, period, status, summarize, wilson
from wego_on_time.schedule import scheduled_epoch


def ev(delay, route="52", stop="S1", day="20261109", hour=8):
    return {"service_date": day, "route": route, "trip_id": f"t{delay}{hour}", "stop_id": stop, "seq": 1,
            "sched": scheduled_epoch(day, hour * 3600), "observed": 0, "delay": delay}


def test_on_time_is_one_minute_early_to_five_late_inclusive():
    assert [status(d) for d in (-61, -60, 0, 300, 301)] == ["early", "on_time", "on_time", "on_time", "late"]


def test_arriving_early_at_the_end_of_the_line_is_not_early():
    assert status(-400, kind="arr") == "on_time"
    assert status(400, kind="arr") == "late"


def test_summary_counts_and_interval():
    s = summarize([ev(d) for d in (-120, 0, 30, 200, 400)])
    assert (s["n"], s["early"], s["on_time"], s["late"]) == (5, 1, 3, 1)
    assert s["on_time_pct"] == 0.6 and s["median_delay"] == 30
    lo, hi = s["ci"]
    assert lo < 0.6 < hi and hi - lo > 0.5  # five departures say very little


def test_wilson_narrows_with_more_data_and_stays_in_bounds():
    assert wilson(0, 0) == (0.0, 1.0)
    a, b = wilson(80, 100), wilson(800, 1000)
    assert b[1] - b[0] < a[1] - a[0]
    assert wilson(10, 10)[1] <= 1.0


def test_periods_put_after_midnight_trips_in_the_night():
    assert period(ev(0, hour=8)) == "Morning rush (7–9am)"
    assert period(ev(0, hour=25)) == "Night (after 9pm)"  # 1am on the next calendar day
    assert period(ev(0, hour=5)) == "Early morning (before 7am)"


def test_routes_with_too_few_departures_are_listed_but_not_ranked():
    rows_ = [ev(0, route="52") for _ in range(120)] + [ev(400, route="3") for _ in range(5)]
    rows_ = [dict(r, trip_id=f"{r['trip_id']}-{i}") for i, r in enumerate(rows_)]
    rep = build_report(rows_, {"52": "52 Nolensville Pike", "3": "3 West End"}, {"S1": (0, 0, "MAIN ST")},
                       {"trips_seen": 125, "trips_scheduled": 250, "share": 0.5})
    shown = [r["route"] for r in rep["routes"] if r["shown"]]
    assert shown == ["52"]
    html = page(rep)
    assert "52 Nolensville Pike" in html and "fewer than 100" in html and "3 West End" in html
    assert "Early results" in html  # one day of data is labelled as such
    assert "125 of 250 scheduled trips" in html


def test_collector_keeps_each_report_once_and_survives_a_failed_poll(tmp_path: Path):
    from google.transit import gtfs_realtime_pb2 as g

    def feed(ts):
        f = g.FeedMessage()
        e = f.entity.add(id="1")
        e.vehicle.trip.trip_id, e.vehicle.trip.start_date, e.vehicle.trip.route_id = "t1", "20261109", "52"
        e.vehicle.vehicle.id, e.vehicle.timestamp = "2201", ts
        e.vehicle.position.latitude, e.vehicle.position.longitude = 36.16, -86.78
        f.entity.add(id="2").vehicle.vehicle.id = "idle"  # no trip, no position: skipped
        return f

    feeds = iter([feed(100), feed(100), RuntimeError("timeout"), feed(130)])

    def fetcher():
        x = next(feeds)
        if isinstance(x, Exception):
            raise x
        return x

    t = [0.0]
    out = tmp_path / "p.csv"
    res = collect(out, hours=4 * 30 / 3600, every=30, log=lambda m: None, fetcher=fetcher,
                  clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + 30))
    assert res == {"polls": 4, "errors": 1, "reports": 2}
    lines = out.read_text().splitlines()
    assert lines[0] == "ts,vehicle,trip_id,start_date,route_id,lat,lon" and len(lines) == 3
    assert len(rows(feed(1))) == 1
