"""Summaries of the observed timepoint events, and the static site that shows them.

On time = no more than EARLY seconds early and no more than LATE seconds late,
the range a Transportation Research Board study found most US agencies use
(1 minute early to 5 minutes late). WeGo's own definition wasn't found
published, so the thresholds are settings and the README says so.
"""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EARLY, LATE = 60, 300
MIN_EVENTS = 100  # fewer than this and a route's percentage isn't shown
TZ = ZoneInfo("America/Chicago")
PERIODS = [(0, 7, "Early morning (before 7am)"), (7, 9, "Morning rush (7–9am)"), (9, 15, "Midday (9am–3pm)"),
           (15, 18, "Evening rush (3–6pm)"), (18, 21, "Evening (6–9pm)"), (21, 30, "Night (after 9pm)")]
REPO = "https://github.com/nathanaelhub/wego-on-time"


def status(delay: int, early: int = EARLY, late: int = LATE, kind: str = "dep") -> str:
    """An early ARRIVAL at the end of the line strands nobody (schedules pad the last
    stretch), so there only lateness counts."""
    if delay > late:
        return "late"
    return "early" if delay < -early and kind != "arr" else "on_time"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion; honest about small samples where a plain k/n isn't."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(rows: list[dict], early: int = EARLY, late: int = LATE) -> dict:
    n = len(rows)
    counts = defaultdict(int)
    for r in rows:
        counts[status(r["delay"], early, late, r.get("kind", "dep"))] += 1
    delays = sorted(r["delay"] for r in rows)
    lo, hi = wilson(counts["on_time"], n)
    return {
        "n": n, "on_time": counts["on_time"], "early": counts["early"], "late": counts["late"],
        "on_time_pct": counts["on_time"] / n if n else None, "ci": [lo, hi],
        "median_delay": statistics.median(delays) if delays else None,
        "p90_delay": delays[min(n - 1, int(0.9 * n))] if delays else None,
    }


def local_hour(r: dict) -> float:
    t = datetime.fromtimestamp(r["sched"], TZ)
    h = t.hour + t.minute / 60
    # trips after midnight belong to the previous service day's night
    return h + 24 if t.strftime("%Y%m%d") > r["service_date"] else h


def period(r: dict) -> str:
    h = local_hour(r)
    return next(label for lo, hi, label in PERIODS if lo <= h < hi)


def weekday(r: dict) -> bool:
    return datetime.strptime(r["service_date"], "%Y%m%d").weekday() < 5


def build_report(rows: list[dict], routes: dict[str, str], stops: dict[str, tuple], coverage: dict) -> dict:
    by_route, by_period, by_stop = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_route[r["route"]].append(r)
        by_period[period(r)].append(r)
        by_stop[r["stop_id"]].append(r)
    route_rows = []
    for rid, rs in by_route.items():
        s = summarize(rs)
        s.update(route=rid, name=routes.get(rid, rid), shown=s["n"] >= MIN_EVENTS,
                 weekday=summarize([r for r in rs if weekday(r)]), weekend=summarize([r for r in rs if not weekday(r)]))
        route_rows.append(s)
    route_rows.sort(key=lambda s: (not s["shown"], s["on_time_pct"] if s["shown"] else 0, s["name"]))
    stop_rows = []
    for sid, rs in by_stop.items():
        if len(rs) >= MIN_EVENTS:
            s = summarize(rs)
            s.update(stop=sid, name=stops.get(sid, (0, 0, sid))[2], routes=sorted({r["route"] for r in rs}, key=_route_key))
            stop_rows.append(s)
    stop_rows.sort(key=lambda s: s["on_time_pct"])
    days = sorted({r["service_date"] for r in rows})
    return {
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "thresholds": {"early_s": EARLY, "late_s": LATE},
        "days": days, "overall": summarize(rows),
        "by_daytype": {"Weekdays": summarize([r for r in rows if weekday(r)]),
                       "Weekends": summarize([r for r in rows if not weekday(r)])},
        "by_period": [{"period": label, **summarize(by_period[label])} for _, _, label in PERIODS if by_period[label]],
        "routes": route_rows, "worst_stops": stop_rows[:15], "coverage": coverage,
    }


def _route_key(r: str):
    return (0, int(r)) if r.isdigit() else (1, r)


# ---- the page --------------------------------------------------------------

def pct(x):
    return "–" if x is None else f"{100 * x:.0f}%"


def mins(s):
    if s is None:
        return "–"
    m = s / 60
    return f"{m:+.1f} min" if abs(m) >= 0.05 else "0 min"


def bar(s: dict) -> str:
    if not s["n"]:
        return ""
    w = {k: 100 * s[k] / s["n"] for k in ("early", "on_time", "late")}
    return (f'<span class="bar" role="img" aria-label="{w["early"]:.0f}% early, {w["on_time"]:.0f}% on time, {w["late"]:.0f}% late">'
            f'<i class="e" style="width:{w["early"]:.1f}%"></i><i class="o" style="width:{w["on_time"]:.1f}%"></i>'
            f'<i class="l" style="width:{w["late"]:.1f}%"></i></span>')


def row(label: str, s: dict, extra: str = "") -> str:
    ci = f'{pct(s["ci"][0])}–{pct(s["ci"][1])}' if s["n"] else "–"
    return (f"<tr><th scope=row>{label}{extra}</th><td class=num>{pct(s['on_time_pct'])}</td><td class=muted>{ci}</td>"
            f"<td>{bar(s)}</td><td class=num>{pct(s['early'] / s['n'] if s['n'] else None)}</td>"
            f"<td class=num>{pct(s['late'] / s['n'] if s['n'] else None)}</td><td class=num>{mins(s['median_delay'])}</td>"
            f"<td class=num>{s['n']:,}</td></tr>")


HEAD = ("<thead><tr><th scope=col></th><th scope=col>On time</th><th scope=col>95% range</th><th scope=col></th>"
        "<th scope=col>Early</th><th scope=col>Late</th><th scope=col>Typical delay</th><th scope=col>Departures</th></tr></thead>")


def long_day(d: str) -> str:
    x = date(int(d[:4]), int(d[4:6]), int(d[6:]))
    return f"{x:%B} {x.day}, {x.year}"


def page(rep: dict) -> str:
    e = html.escape
    o, days, cov = rep["overall"], rep["days"], rep["coverage"]
    span = f"{long_day(days[0])} to {long_day(days[-1])}" if days else "no days yet"
    prelim = len(days) < 14
    shown = [r for r in rep["routes"] if r["shown"]]
    hidden = [r for r in rep["routes"] if not r["shown"]]
    routes = "".join(row(e(r["name"]), r) for r in shown)
    periods = "".join(row(e(p["period"]), p) for p in rep["by_period"])
    daytypes = "".join(row(k, v) for k, v in rep["by_daytype"].items() if v["n"])
    stops = "".join(row(e(s["name"].title()), s, f' <span class=muted>· routes {", ".join(s["routes"])}</span>')
                    for s in rep["worst_stops"])
    note = (f'<p class="note"><strong>Early results.</strong> This covers {len(days)} service '
            f'day{"s" if len(days) != 1 else ""}. Percentages settle after a few weeks of collection; '
            f'the 95% ranges show how much each could still move.</p>') if prelim else ""
    hidden_note = (f'<p class=hint>{len(hidden)} route{"s" if len(hidden) != 1 else ""} with fewer than {MIN_EVENTS} '
                   f'observed departures so far aren\'t shown: {e(", ".join(r["name"] for r in hidden))}.</p>') if hidden else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeGo On Time</title>
<meta name="description" content="How often Nashville's WeGo buses leave their timepoints on time, measured from WeGo's own live GPS feed.">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="top"><div class="wrap"><p class="eyebrow">WeGo On Time</p></div></header>
<main class="wrap">
<h1>Are Nashville's buses on time?</h1>
<p class="lede">Measured from WeGo's own live GPS feed: every bus, every 30 seconds, compared with the
published schedule at its timepoints. On time means leaving no more than 1 minute early or 5 minutes late.</p>
{note}
<div class="big"><p><span class="num-big">{pct(o['on_time_pct'])}</span> of {o['n']:,} observed departures were on time
<span class="muted">(95% range {pct(o['ci'][0])}–{pct(o['ci'][1])})</span>. {pct(o['early'] / o['n'] if o['n'] else None)} left early
and {pct(o['late'] / o['n'] if o['n'] else None)} more than 5 minutes late.</p>
<p class="muted">{span} · {cov.get('trips_seen', 0):,} of {cov.get('trips_scheduled', 0):,} scheduled trips observed ({pct(cov.get('share'))}).</p></div>

<h2>By route</h2>
<p class=hint>Worst first. An early bus is as much a problem as a late one: riders who arrive on time miss it.</p>
<div class="scroll"><table>{HEAD}<tbody>{routes}</tbody></table></div>
{hidden_note}

<h2>By time of day</h2>
<div class="scroll"><table>{HEAD}<tbody>{periods}</tbody></table></div>

<h2>Weekdays and weekends</h2>
<div class="scroll"><table>{HEAD}<tbody>{daytypes}</tbody></table></div>

<h2>Least reliable timepoints</h2>
<p class=hint>Timepoints with at least {MIN_EVENTS} observed departures.</p>
<div class="scroll"><table>{HEAD}<tbody>{stops}</tbody></table></div>

<section class="about">
<h2>How it's measured</h2>
<ul>
<li>A collector reads WeGo's public GTFS-Realtime vehicle positions every 30 seconds and keeps each bus's
reports for the trip it's serving.</li>
<li>Only <strong>timepoints</strong> are scored: the stops with real scheduled times (other stops' times are
estimates the schedule interpolates). Departure time is when the bus leaves the stop; at the end of a line,
when it arrives.</li>
<li>At the end of a line only lateness counts: arriving early at the last stop strands no one.</li>
<li>A pass is only scored when reports are no more than 2 minutes apart around it, and a "delay" of more than
an hour is treated as a mislabelled trip, not lateness.</li>
<li>Coverage is the share of trips scheduled on collection days that produced at least one scored timepoint.
Collection runs in shifts, so some trips around shift changes, and any outage of the feed, are missed.</li>
<li>WeGo's own on-time definition wasn't found published; 1 minute early to 5 minutes late is the range a
Transportation Research Board study found most US agencies use. Not affiliated with WeGo.</li>
</ul>
<p class=hint>Updated {e(rep['generated'])}. Data and code: <a href="{REPO}">github.com/nathanaelhub/wego-on-time</a>
· <a href="report.json">report.json</a></p>
</section>
</main>
</body>
</html>
"""


def write_site(rep: dict, out: Path, css: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page(rep), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    (out / "style.css").write_text(css.read_text(encoding="utf-8"), encoding="utf-8")
    return out
