#!/usr/bin/env python3
"""Estimate remaining bottom time at fixed depths from a MacDive UDDF export.

MacDive (and most desktop dive logs) can export dives in UDDF, the Universal
Dive Data Format.  UDDF is XML and stores everything in SI units:

    * depth          metres            (<depth>)
    * dive time      seconds           (<divetime>)
    * tank pressure  pascal            (<tankpressure>, <tankpressurebegin> ...)
    * tank volume    cubic metres      (<tankvolume>)

This script reads the recorded depth / time / tank-pressure samples of each
dive, derives the diver's air-consumption rate (RMV, surface-normalised litres
per minute) and then projects how long that same diver could have stayed at a
constant 5, 10 and 15 metres on the gas they carried.

It only uses the Python standard library.
"""

from __future__ import annotations

import argparse
import datetime
import html
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# --- physical constants ----------------------------------------------------

PASCAL_PER_BAR = 100_000.0          # 1 bar = 100000 Pa
LITRES_PER_CUBIC_METRE = 1000.0     # 1 m3 = 1000 L
METRES_PER_BAR = 10.0               # ~10 m of sea water adds 1 bar of pressure


def ambient_pressure_bar(depth_m: float) -> float:
    """Absolute ambient pressure (in bar) at a given depth in sea water."""
    return 1.0 + depth_m / METRES_PER_BAR


# --- data model ------------------------------------------------------------


@dataclass
class Waypoint:
    time_s: float                    # elapsed dive time, seconds
    depth_m: float                   # depth, metres
    tank_pressure_bar: float | None  # tank pressure, bar (None if not logged)


@dataclass
class Dive:
    number: str
    waypoints: list[Waypoint]
    tank_volume_l: float | None      # tank water capacity, litres
    pressure_begin_bar: float | None
    pressure_end_bar: float | None
    when: str | None = None          # dive date/time (UDDF <datetime>)
    divenumber: int | None = None    # logged dive number (UDDF <divenumber>)


# --- UDDF parsing ----------------------------------------------------------


def _localname(tag: str) -> str:
    """Strip an XML namespace from a tag, e.g. '{ns}depth' -> 'depth'."""
    return tag.rsplit("}", 1)[-1]


def _find(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem.iter():
        if _localname(child.tag) == name:
            return child
    return None


def _findall_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem.iter() if _localname(c.tag) == name]


def _float_or_none(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _direct_child_text(elem: ET.Element, name: str) -> str | None:
    """Text of the first *direct* child with the given local name."""
    for child in elem:
        if _localname(child.tag) == name:
            return child.text
    return None


def parse_uddf(path: str) -> list[Dive]:
    """Parse a UDDF file and return its dives."""
    tree = ET.parse(path)
    root = tree.getroot()

    dives: list[Dive] = []
    dive_elems = [e for e in root.iter() if _localname(e.tag) == "dive"]

    for index, dive_elem in enumerate(dive_elems, start=1):
        number = dive_elem.get("id") or str(index)

        # dive date/time (lives under informationbeforedive in UDDF)
        when = None
        dt_elem = _find(dive_elem, "datetime")
        if dt_elem is not None and dt_elem.text:
            when = dt_elem.text.strip()

        # logged dive number (<divenumber>), used for display and sorting
        divenumber = None
        num_elem = _find(dive_elem, "divenumber")
        if num_elem is not None and num_elem.text:
            try:
                divenumber = int(num_elem.text.strip())
            except ValueError:
                divenumber = None

        # waypoints / samples
        waypoints: list[Waypoint] = []
        for wp in _findall_local(dive_elem, "waypoint"):
            time_s = _float_or_none(_direct_child_text(wp, "divetime"))
            depth_m = _float_or_none(_direct_child_text(wp, "depth"))
            pressure_pa = _float_or_none(_direct_child_text(wp, "tankpressure"))
            if time_s is None or depth_m is None:
                continue
            pressure_bar = (
                pressure_pa / PASCAL_PER_BAR if pressure_pa is not None else None
            )
            waypoints.append(Waypoint(time_s, depth_m, pressure_bar))

        # tank metadata (first tank with usable data wins)
        tank_volume_l = None
        pressure_begin_bar = None
        pressure_end_bar = None
        for tank in _findall_local(dive_elem, "tankdata"):
            vol = _float_or_none(_direct_child_text(tank, "tankvolume"))
            begin = _float_or_none(_direct_child_text(tank, "tankpressurebegin"))
            end = _float_or_none(_direct_child_text(tank, "tankpressureend"))
            if vol is not None and tank_volume_l is None:
                tank_volume_l = vol * LITRES_PER_CUBIC_METRE
            if begin is not None and pressure_begin_bar is None:
                pressure_begin_bar = begin / PASCAL_PER_BAR
            if end is not None and pressure_end_bar is None:
                pressure_end_bar = end / PASCAL_PER_BAR

        dives.append(
            Dive(
                number=number,
                waypoints=waypoints,
                tank_volume_l=tank_volume_l,
                pressure_begin_bar=pressure_begin_bar,
                pressure_end_bar=pressure_end_bar,
                when=when,
                divenumber=divenumber,
            )
        )

    return dives


# --- consumption analysis --------------------------------------------------


@dataclass
class Consumption:
    rmv_l_per_min: float | None      # surface-normalised respiratory minute volume
    gas_used_bar: float              # total tank pressure consumed
    duration_min: float              # span over which it was consumed
    avg_depth_m: float               # time-weighted average depth
    max_depth_m: float


def analyse_consumption(dive: Dive) -> Consumption:
    """Derive the diver's air-consumption rate from the recorded profile.

    RMV/SAC (the surface-normalised breathing rate) is computed the way dive
    logs such as MacDive report it: total gas consumed over the whole dive,
    divided by the total dive time and the average ambient pressure::

        RMV = (dP_total * tank_volume) / (dive_time * P_ambient(avg_depth))

    dP_total is the begin-to-end tank-pressure drop (from <tankpressurebegin>
    / <tankpressureend>, falling back to the sampled drop), tank_volume is in
    litres, dive_time is in minutes and P_ambient(avg_depth) = 1 + avg/10.

    Using the *total* time (not only the intervals where pressure dropped)
    matters: air-integration logging gaps would otherwise shrink the
    denominator and overstate the SAC compared with MacDive.
    """
    wps = dive.waypoints
    total_gas_bar = 0.0
    depth_time_integral = 0.0
    total_time_s = 0.0
    max_depth = 0.0

    for prev, cur in zip(wps, wps[1:]):
        dt_s = cur.time_s - prev.time_s
        if dt_s <= 0:
            continue
        avg_depth = (prev.depth_m + cur.depth_m) / 2.0
        depth_time_integral += avg_depth * dt_s
        total_time_s += dt_s
        max_depth = max(max_depth, prev.depth_m, cur.depth_m)

        if prev.tank_pressure_bar is None or cur.tank_pressure_bar is None:
            continue
        dp = prev.tank_pressure_bar - cur.tank_pressure_bar
        if dp > 0:
            total_gas_bar += dp

    max_depth = max(max_depth, (wps[-1].depth_m if wps else 0.0))
    avg_depth = depth_time_integral / total_time_s if total_time_s else 0.0
    duration_min = total_time_s / 60.0

    # Total gas drop: prefer the explicit begin/end tank pressures (what
    # MacDive uses), fall back to the sum of sampled pressure drops.
    gas_drop_bar = total_gas_bar
    if dive.pressure_begin_bar is not None and dive.pressure_end_bar is not None:
        explicit_drop = dive.pressure_begin_bar - dive.pressure_end_bar
        if explicit_drop > 0:
            gas_drop_bar = explicit_drop

    rmv = None
    if (
        dive.tank_volume_l is not None
        and duration_min > 0
        and gas_drop_bar > 0
    ):
        avg_p_amb = ambient_pressure_bar(avg_depth)
        rmv = (gas_drop_bar * dive.tank_volume_l) / (duration_min * avg_p_amb)

    return Consumption(
        rmv_l_per_min=rmv,
        gas_used_bar=total_gas_bar,
        duration_min=duration_min,
        avg_depth_m=avg_depth,
        max_depth_m=max_depth,
    )


def remaining_gas_litres(dive: Dive, reserve_bar: float) -> tuple[float, float] | None:
    """Usable *remaining* surface gas (litres) and the remaining pressure (bar).

    "Rest"/remaining time is computed from the gas the diver still had at the
    end of the dive, drawn down to the reserve pressure::

        usable = (remaining_pressure - reserve) * tank_volume

    The remaining pressure is the explicit tankpressureend, otherwise the
    pressure of the last logged sample.  Returns None if neither the remaining
    pressure nor the tank volume is known.
    """
    if dive.tank_volume_l is None:
        return None

    remaining_bar = dive.pressure_end_bar
    if remaining_bar is None:
        sampled = [
            wp.tank_pressure_bar
            for wp in dive.waypoints
            if wp.tank_pressure_bar is not None
        ]
        if sampled:
            remaining_bar = sampled[-1]
    if remaining_bar is None:
        return None

    usable_bar = max(0.0, remaining_bar - reserve_bar)
    return usable_bar * dive.tank_volume_l, remaining_bar


def consumption_at_depth(
    dive: Dive, target_depth_m: float, window_m: float
) -> float | None:
    """Observed surface gas-consumption rate (L/min) near a given depth.

    Looks at every interval of the recorded profile whose mean depth lies
    within +/- window_m of the target depth and aggregates the gas actually
    consumed there::

        rate = sum(dP_tank * tank_volume) / sum(dt)

    Returns None if the dive never spent measurable time near that depth (or
    has no tank data), so the caller can fall back to a modelled rate.
    """
    if dive.tank_volume_l is None:
        return None

    litres = 0.0
    minutes = 0.0
    for prev, cur in zip(dive.waypoints, dive.waypoints[1:]):
        dt_s = cur.time_s - prev.time_s
        if dt_s <= 0:
            continue
        avg_depth = (prev.depth_m + cur.depth_m) / 2.0
        if abs(avg_depth - target_depth_m) > window_m:
            continue
        if prev.tank_pressure_bar is None or cur.tank_pressure_bar is None:
            continue
        dp = prev.tank_pressure_bar - cur.tank_pressure_bar
        if dp <= 0:
            continue
        litres += dp * dive.tank_volume_l
        minutes += dt_s / 60.0

    if minutes <= 0 or litres <= 0:
        return None
    return litres / minutes


# --- report model ----------------------------------------------------------


@dataclass
class PrognosisRow:
    depth_m: float
    minutes: float
    rate_l_per_min: float
    observed: bool          # True: rate measured at this depth; False: modelled


@dataclass
class DiveReport:
    dive: Dive
    consumption: Consumption
    usable_litres: float | None
    remaining_bar: float | None
    reserve_bar: float
    rows: list[PrognosisRow]
    note: str | None  # set when no prognosis could be produced


def build_dive_report(
    dive: Dive,
    depths: list[float],
    reserve_bar: float,
    window_m: float,
) -> DiveReport:
    cons = analyse_consumption(dive)

    gas = remaining_gas_litres(dive, reserve_bar)
    if gas is None:
        return DiveReport(
            dive, cons, None, None, reserve_bar, [],
            "No remaining-gas figure available "
            "(needs tank volume + end pressure) - no prognosis possible.",
        )

    usable_litres, remaining_bar = gas
    rows: list[PrognosisRow] = []
    for depth in depths:
        # Prefer the consumption the diver actually had near this depth;
        # fall back to the surface RMV scaled to the depth's ambient pressure.
        rate = consumption_at_depth(dive, depth, window_m)
        observed = rate is not None
        if rate is None and cons.rmv_l_per_min is not None:
            rate = cons.rmv_l_per_min * ambient_pressure_bar(depth)
        if rate is None or rate <= 0:
            continue
        rows.append(PrognosisRow(depth, usable_litres / rate, rate, observed))

    if not rows:
        return DiveReport(
            dive, cons, usable_litres, remaining_bar, reserve_bar, [],
            "Not enough consumption data to estimate time at the given depths.",
        )

    return DiveReport(
        dive, cons, usable_litres, remaining_bar, reserve_bar, rows, None
    )


# --- reporting -------------------------------------------------------------


def format_minutes(minutes: float) -> str:
    total_seconds = int(round(minutes * 60))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def dive_label(dive: Dive) -> str:
    """Human-friendly dive identifier: the logged number when available."""
    if dive.divenumber is not None:
        return f"Dive {dive.divenumber}"
    return f"Dive {dive.number}"


def report_dive(report: DiveReport) -> None:
    dive = report.dive
    cons = report.consumption

    print(dive_label(dive))
    if dive.when:
        print(f"  date / time          : {dive.when}")
    print(f"  samples              : {len(dive.waypoints)}")
    print(f"  duration             : {format_minutes(cons.duration_min)}")
    print(f"  max depth            : {cons.max_depth_m:.1f} m")
    print(f"  average depth        : {cons.avg_depth_m:.1f} m")
    if dive.tank_volume_l is not None:
        print(f"  tank volume          : {dive.tank_volume_l:.1f} L")
    if dive.pressure_begin_bar is not None:
        print(f"  start pressure       : {dive.pressure_begin_bar:.0f} bar")
    if dive.pressure_end_bar is not None:
        print(f"  end pressure         : {dive.pressure_end_bar:.0f} bar")
    print(f"  gas used (sampled)   : {cons.gas_used_bar:.0f} bar")
    if cons.rmv_l_per_min is not None:
        print(f"  RMV (surface)        : {cons.rmv_l_per_min:.1f} L/min")

    if report.note is not None:
        print(f"  -> {report.note}\n")
        return

    print(
        f"  remaining gas        : {report.usable_litres:.0f} L "
        f"(from {report.remaining_bar:.0f} bar, "
        f"{report.reserve_bar:.0f} bar reserve)"
    )
    print(
        "  remaining-time prognosis "
        f"(constant depth, down to {report.reserve_bar:.0f} bar):"
    )
    for row in report.rows:
        source = "measured" if row.observed else "modelled"
        print(
            f"      {row.depth_m:5.0f} m : {format_minutes(row.minutes):>12} "
            f"({row.rate_l_per_min:.1f} L/min, {source})"
        )
    print()


# --- HTML rendering --------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value))


def _dive_card_html(report: DiveReport) -> str:
    dive = report.dive
    cons = report.consumption

    facts = [
        ("Samples", f"{len(dive.waypoints)}"),
        ("Duration", format_minutes(cons.duration_min)),
        ("Max depth", f"{cons.max_depth_m:.1f} m"),
        ("Average depth", f"{cons.avg_depth_m:.1f} m"),
    ]
    if dive.tank_volume_l is not None:
        facts.append(("Tank volume", f"{dive.tank_volume_l:.1f} L"))
    if dive.pressure_begin_bar is not None:
        facts.append(("Start pressure", f"{dive.pressure_begin_bar:.0f} bar"))
    if dive.pressure_end_bar is not None:
        facts.append(("End pressure", f"{dive.pressure_end_bar:.0f} bar"))
    facts.append(("Gas used (sampled)", f"{cons.gas_used_bar:.0f} bar"))
    if cons.rmv_l_per_min is not None:
        facts.append(("RMV (surface)", f"{cons.rmv_l_per_min:.1f} L/min"))

    facts_html = "\n".join(
        f'      <div class="fact"><span class="label">{_esc(k)}</span>'
        f'<span class="value">{_esc(v)}</span></div>'
        for k, v in facts
    )

    if report.note is not None:
        body = f'    <p class="note">{_esc(report.note)}</p>'
    else:
        rows_html = "\n".join(
            f"        <tr><td>{r.depth_m:.0f} m</td>"
            f"<td class=\"time\">{_esc(format_minutes(r.minutes))}</td>"
            f"<td class=\"rate\">{r.rate_l_per_min:.1f} L/min</td>"
            f"<td class=\"src\">{'measured' if r.observed else 'modelled'}</td>"
            f"</tr>"
            for r in report.rows
        )
        body = f"""    <p class="gasline">Remaining gas
      <strong>{report.usable_litres:.0f} L</strong>
      (from {report.remaining_bar:.0f} bar, {report.reserve_bar:.0f} bar reserve)</p>
    <table class="prognosis">
      <thead><tr><th>Depth</th><th>Remaining time</th>
        <th>Consumption</th><th>Source</th></tr></thead>
      <tbody>
{rows_html}
      </tbody>
    </table>"""

    subtitle = (
        f'\n    <p class="when">{_esc(dive.when)}</p>' if dive.when else ""
    )
    return f"""  <section class="card">
    <h2>{_esc(dive_label(dive))}</h2>{subtitle}
    <div class="facts">
{facts_html}
    </div>
{body}
  </section>"""


def render_html(reports: list[DiveReport], source: str) -> str:
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "\n".join(_dive_card_html(r) for r in reports)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rest Diving Time – {_esc(source)}</title>
<style>
  :root {{
    --bg: #0e1726; --card: #16223a; --ink: #e6edf6; --muted: #8aa0bd;
    --accent: #38bdf8; --line: #243650;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1rem 4rem; background: var(--bg); color: var(--ink);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  header h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
  header p {{ margin: 0 0 2rem; color: var(--muted); font-size: .9rem; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 1.25rem 1.5rem; margin-bottom: 1.5rem;
  }}
  .card h2 {{ margin: 0 0 .25rem; font-size: 1.25rem; color: var(--accent); }}
  .card .when {{ margin: 0 0 1rem; color: var(--muted); font-size: .85rem; }}
  .facts {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: .5rem .75rem; margin-bottom: 1.25rem;
  }}
  .fact {{ display: flex; flex-direction: column; }}
  .fact .label {{ color: var(--muted); font-size: .75rem;
    text-transform: uppercase; letter-spacing: .04em; }}
  .fact .value {{ font-weight: 600; }}
  .gasline {{ color: var(--muted); margin: 0 0 .75rem; }}
  .gasline strong {{ color: var(--ink); }}
  table.prognosis {{ width: 100%; border-collapse: collapse; }}
  table.prognosis th, table.prognosis td {{
    text-align: left; padding: .6rem .5rem; border-bottom: 1px solid var(--line);
  }}
  table.prognosis th {{ color: var(--muted); font-size: .75rem;
    text-transform: uppercase; letter-spacing: .04em; }}
  table.prognosis td.time {{ font-weight: 700; color: var(--accent);
    font-size: 1.05rem; }}
  table.prognosis td.rate {{ color: var(--muted); }}
  table.prognosis td.src {{ color: var(--muted); font-size: .8rem;
    text-transform: uppercase; letter-spacing: .04em; }}
  table.prognosis tr:last-child td {{ border-bottom: none; }}
  .note {{ color: #fbbf24; margin: 0; }}
  footer {{ color: var(--muted); font-size: .8rem; margin-top: 2rem;
    border-top: 1px solid var(--line); padding-top: 1rem; }}
</style>
</head>
<body>
  <div class="wrap">
  <header>
    <h1>Rest Diving Time Prognosis</h1>
    <p>Source: {_esc(source)} &middot; {len(reports)} dive(s)
       &middot; generated {generated}</p>
  </header>
{cards}
  <footer>
    Estimates assume a constant depth and steady breathing rate. They do
    <strong>not</strong> model no-decompression limits &ndash; always
    cross-check with your dive computer or tables.
  </footer>
  </div>
</body>
</html>"""


def parse_depths(spec: str) -> list[float]:
    depths = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if chunk:
            depths.append(float(chunk))
    return depths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate remaining bottom time at fixed depths from the air "
            "consumption recorded in a MacDive UDDF export."
        )
    )
    parser.add_argument("uddf", help="path to the UDDF export file")
    parser.add_argument(
        "--depths",
        default="5,10,15",
        help="comma-separated target depths in metres (default: 5,10,15)",
    )
    parser.add_argument(
        "--reserve",
        type=float,
        default=50.0,
        help="reserve pressure to keep in the tank, in bar (default: 50)",
    )
    parser.add_argument(
        "--depth-window",
        type=float,
        default=2.5,
        metavar="M",
        help=(
            "half-width (metres) of the depth band used to measure the "
            "consumption near each target depth (default: 2.5)"
        ),
    )
    parser.add_argument(
        "--sort",
        choices=("file", "number"),
        default="file",
        help=(
            "order of dives in the output: 'file' (as exported, default) or "
            "'number' (by logged dive number)"
        ),
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="write a styled HTML report to PATH instead of plain text",
    )
    args = parser.parse_args(argv)

    try:
        dives = parse_uddf(args.uddf)
    except ET.ParseError as exc:
        print(f"error: could not parse UDDF file: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read file: {exc}", file=sys.stderr)
        return 1

    if not dives:
        print("No dives found in the UDDF file.", file=sys.stderr)
        return 1

    if args.sort == "number":
        # Dives without a logged number sort last, keeping export order.
        dives = sorted(
            dives,
            key=lambda d: (d.divenumber is None, d.divenumber or 0),
        )

    depths = parse_depths(args.depths)
    reports = [
        build_dive_report(dive, depths, args.reserve, args.depth_window)
        for dive in dives
    ]

    if args.html:
        try:
            with open(args.html, "w", encoding="utf-8") as fh:
                fh.write(render_html(reports, args.uddf))
        except OSError as exc:
            print(f"error: could not write HTML file: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote HTML report for {len(reports)} dive(s) to {args.html}")
        return 0

    print(f"Parsed {len(reports)} dive(s) from {args.uddf}\n")
    for report in reports:
        report_dive(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
