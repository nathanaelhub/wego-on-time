"""Arrival detection on hand-built GPS tracks, where the right answer is known."""

import gzip
from pathlib import Path

from wego_on_time.arrivals import Ping, metres, observe, read_events, trip_events, write
from wego_on_time.schedule import Schedule, Stop, Trip, scheduled_epoch, seconds, service_start

# A straight north-south street; 0.0009 degrees of latitude is about 100 m.
LON = -86.78
A, B, C = 36.1500, 36.1600, 36.1700  # three timepoints 1.1 km apart
T0 = scheduled_epoch("20261109", 8 * 3600)  # 8:00 am, Nov 9 2026


def track(points):
    """[(seconds after T0, lat)] -> pings every report."""
    return [Ping(T0 + t, lat, LON, "v1") for t, lat in points]


def sched(stops):
    s = Schedule("test", {"A": (A, LON, "A"), "B": (B, LON, "B"), "C": (C, LON, "C")}, {"1": "1 Test"},
                 {"t": Trip("t", "1", "north", stops)})
    return s


def test_metres_is_about_right():
    assert abs(metres(A, LON, B, LON) - 1112) < 2


def test_departure_is_when_the_bus_leaves_a_timepoint_not_when_it_arrives():
    # arrives at A at t=0 (on time), waits until t=120, then leaves; reports every 30 s
    pts = [(-60, A - 0.004), (-30, A - 0.0015), (0, A), (30, A), (60, A), (90, A), (120, A), (150, A + 0.003)]
    got = observe(track(pts), A, LON, 0, "dep")
    assert got == (T0 + 135, 6)  # halfway between the last report at the stop and the first one away


def test_arrival_at_the_end_of_the_line_is_when_it_gets_there():
    pts = [(-60, C - 0.004), (-30, C - 0.0015), (0, C), (30, C), (60, C)]
    when, _ = observe(track(pts), C, LON, 0, "arr")
    assert when == T0 - 15  # between the last report short of the stop and the first at it


def test_a_pass_seen_through_a_long_gap_is_not_scored():
    pts = [(-30, A - 0.003), (0, A), (300, A + 0.01)]  # five minutes with no report after the stop
    assert observe(track(pts), A, LON, 0, "dep") is None


def test_a_bus_that_never_came_near_is_not_scored():
    assert observe(track([(0, A - 0.01), (30, A - 0.0095)]), A, LON, 0, "dep") is None


def test_trip_is_matched_in_stop_order_and_delays_are_signed():
    stops = [Stop(1, "A", 8 * 3600, first=True), Stop(5, "B", 8 * 3600 + 300), Stop(9, "C", 8 * 3600 + 600, last=True)]
    # leaves A 1 min late, passes B 3 min late without stopping, reaches C 2 min early
    pts = [(0, A), (30, A), (60, A), (90, A + 0.002), (120, A + 0.004), (420, B - 0.0045), (450, B - 0.0005),
           (480, B + 0.0035), (510, B + 0.006), (540, C - 0.0004), (570, C), (600, C)]
    pts = [(t, lat) for t, lat in pts]
    ev = trip_events(sched(stops), "t", "20261109", track(pts))
    assert [(e.stop_id, e.kind) for e in ev] == [("A", "dep"), ("B", "dep"), ("C", "arr")]
    assert ev[0].delay == 75  # left between 60 and 90 s
    assert 120 < ev[1].delay < 200  # passed B around 3 min late
    assert ev[2].delay == 555 - 600  # reached C between 540 and 570: 45 s early


def test_a_loop_route_passing_a_stop_twice_uses_the_right_visit():
    # the trip starts at A, goes to B, comes back past A on the way to C (as a loop does)
    stops = [Stop(1, "A", 8 * 3600, first=True), Stop(4, "B", 8 * 3600 + 300), Stop(8, "A", 8 * 3600 + 600)]
    pts = [(0, A), (30, A), (60, A + 0.003), (290, B), (310, B), (330, B - 0.003), (580, A + 0.0004), (610, A), (640, A - 0.003)]
    ev = trip_events(sched(stops), "t", "20261109", track(pts))
    assert [e.seq for e in ev] == [1, 4, 8]
    assert ev[0].delay == 45 and ev[2].delay > 0


def test_delays_beyond_an_hour_are_dropped_as_mislabelled():
    stops = [Stop(1, "A", 8 * 3600, first=True), Stop(5, "B", 8 * 3600 + 300)]
    pts = [(7200, A), (7230, A + 0.003), (7500, B - 0.003)]
    assert trip_events(sched(stops), "t", "20261109", track(pts)) == []


def test_gtfs_times_past_midnight_and_on_dst_days():
    assert seconds(" 5:22:00") == 19320 and seconds("25:10:00") == 90600
    # Nov 1 2026 is the fall-back day: noon minus 12h is 1:00 am CDT... i.e. 00:00 + 1h shift
    from datetime import date
    assert service_start(date(2026, 11, 1)).hour == 0
    assert scheduled_epoch("20261102", 25 * 3600) - scheduled_epoch("20261102", 0) == 25 * 3600


def test_write_merges_and_never_double_counts(tmp_path: Path):
    stops = [Stop(1, "A", 8 * 3600, first=True), Stop(5, "B", 8 * 3600 + 300)]
    ev = trip_events(sched(stops), "t", "20261109", track([(0, A), (30, A + 0.003), (300, B), (330, B + 0.003)]))[:1]
    write(ev, tmp_path)
    write(ev, tmp_path)  # the same shift processed twice
    rows = read_events(tmp_path)
    assert len(rows) == 1 and rows[0]["delay"] == 15
    with gzip.open(tmp_path / "2026-11-09.csv.gz", "rt") as f:
        assert f.readline().startswith("service_date,route,trip_id")


# Patterns found in WeGo's real feed on Nov 9 2026, where the first version of the
# matcher scored a departure that hadn't happened.
M = 0.000009  # about one metre of latitude
ORIGIN = [Stop(1, "A", 8 * 3600, first=True), Stop(5, "B", 8 * 3600 + 300)]


def test_a_bus_moving_to_a_layover_spot_near_the_stop_has_not_left():
    # 9 m from the bay, then waits 83 m away for 20 minutes, then leaves for B
    pts = [(-1500, A + 9 * M), (-1470, A + 38 * M)] + [(t, A + 83 * M) for t in range(-1440, 1, 30)] + \
          [(30, A + 400 * M), (300, B), (330, B + 400 * M)]
    ev = trip_events(sched(ORIGIN), "t", "20261109", track(pts))
    assert ev[0].delay == 15  # left between t=0 and t=30, not 25 minutes early


def test_gps_wobbling_across_the_radius_is_one_visit():
    pts = [(-120, A + 46 * M), (-90, A + 119 * M), (-60, A + 46 * M), (-30, A + 131 * M), (0, A + 46 * M),
           (30, A + 500 * M), (300, B), (330, B + 400 * M)]
    ev = trip_events(sched(ORIGIN), "t", "20261109", track(pts))
    assert ev[0].delay == 15


def test_a_trip_start_uses_the_last_visit_before_the_next_timepoint():
    # swings past A 28 minutes early (end of its previous trip), goes away, comes back and leaves on time
    pts = [(-1700, A + 25 * M), (-1670, A + 360 * M), (-1640, A + 600 * M), (-60, A), (-30, A), (0, A),
           (30, A + 400 * M), (300, B), (330, B + 400 * M)]
    ev = trip_events(sched(ORIGIN), "t", "20261109", track(pts))
    assert [e.stop_id for e in ev] == ["A", "B"] and ev[0].delay == 15


def test_a_trip_start_is_not_scored_unless_the_bus_reaches_the_next_timepoint():
    pts = [(-60, A), (0, A), (30, A + 400 * M), (60, A + 800 * M)]  # never seen at B
    assert trip_events(sched(ORIGIN), "t", "20261109", track(pts)) == []


def test_timepoints_passed_on_the_way_to_the_start_do_not_count():
    # an express bus labelled with its trip early drives empty past B on its way to A,
    # waits at A, and the shift ends before it leaves: nothing about this trip is known yet
    stops = [Stop(1, "A", 8 * 3600, first=True), Stop(3, "B", 8 * 3600 + 600), Stop(7, "C", 8 * 3600 + 3600, last=True)]
    pts = [(-3000, B - 0.004), (-2970, B), (-2940, B - 0.004), (-2800, A + 0.002)] + [(t, A) for t in range(-2700, 1, 30)]
    assert trip_events(sched(stops), "t", "20261109", track(pts)) == []
    # the same bus, seen leaving A and reaching B: only the real trip is scored
    pts += [(30, A + 0.004), (630, B), (660, B + 0.004)]
    ev = trip_events(sched(stops), "t", "20261109", track(pts))
    assert [(e.stop_id, e.delay) for e in ev] == [("A", 15), ("B", 45)]
