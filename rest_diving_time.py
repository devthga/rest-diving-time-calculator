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

    RMV (Respiratory Minute Volume) is the depth-independent breathing rate
    normalised to the surface::

        RMV = sum(dP_tank * tank_volume) / sum(dt * P_ambient)

    where dP_tank is the pressure drop over an interval (bar), tank_volume is
    in litres, dt is the interval duration (minutes) and P_ambient is the mean
    absolute pressure (bar) over the interval.  Intervals where the pressure
    rises (sensor noise, gas switches) are ignored.
    """
    wps = dive.waypoints
    total_gas_bar = 0.0
    weighted_volume_litres = 0.0     # numerator: surface gas consumed (L)
    weighted_ata_minutes = 0.0       # denominator: dt(min) * P_ambient(bar)
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
        if dp <= 0:
            continue
        total_gas_bar += dp
        dt_min = dt_s / 60.0
        p_amb = ambient_pressure_bar(avg_depth)
        if dive.tank_volume_l is not None:
            weighted_volume_litres += dp * dive.tank_volume_l
        weighted_ata_minutes += dt_min * p_amb

    max_depth = max(max_depth, (wps[-1].depth_m if wps else 0.0))
    avg_depth = depth_time_integral / total_time_s if total_time_s else 0.0
    duration_min = total_time_s / 60.0

    rmv = None
    if (
        dive.tank_volume_l is not None
        and weighted_ata_minutes > 0
        and weighted_volume_litres > 0
    ):
        rmv = weighted_volume_litres / weighted_ata_minutes

    return Consumption(
        rmv_l_per_min=rmv,
        gas_used_bar=total_gas_bar,
        duration_min=duration_min,
        avg_depth_m=avg_depth,
        max_depth_m=max_depth,
    )


def available_gas_litres(dive: Dive, reserve_bar: float) -> tuple[float, float] | None:
    """Usable surface gas (litres) and the start pressure (bar) used for it.

    Prefers the explicit tankpressurebegin, otherwise the highest pressure seen
    in the profile.  Returns None if neither pressure nor tank volume is known.
    """
    if dive.tank_volume_l is None:
        return None

    start_bar = dive.pressure_begin_bar
    if start_bar is None:
        sampled = [
            wp.tank_pressure_bar
            for wp in dive.waypoints
            if wp.tank_pressure_bar is not None
        ]
        if sampled:
            start_bar = max(sampled)
    if start_bar is None:
        return None

    usable_bar = max(0.0, start_bar - reserve_bar)
    return usable_bar * dive.tank_volume_l, start_bar


# --- reporting -------------------------------------------------------------


def format_minutes(minutes: float) -> str:
    total_seconds = int(round(minutes * 60))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def report_dive(
    dive: Dive,
    depths: list[float],
    reserve_bar: float,
) -> None:
    cons = analyse_consumption(dive)

    print(f"Dive {dive.number}")
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

    if cons.rmv_l_per_min is None:
        print(
            "  RMV                  : n/a "
            "(needs tank volume + tank-pressure samples)"
        )
        print("  -> cannot compute a rest-time prognosis for this dive.\n")
        return

    print(f"  RMV (surface)        : {cons.rmv_l_per_min:.1f} L/min")

    gas = available_gas_litres(dive, reserve_bar)
    if gas is None:
        print("  -> no usable gas figure available for prognosis.\n")
        return

    usable_litres, start_bar = gas
    print(
        f"  usable gas           : {usable_litres:.0f} L "
        f"(from {start_bar:.0f} bar, {reserve_bar:.0f} bar reserve)"
    )
    print(f"  rest-time prognosis (constant depth, {reserve_bar:.0f} bar reserve):")
    for depth in depths:
        p_amb = ambient_pressure_bar(depth)
        rate_at_depth = cons.rmv_l_per_min * p_amb   # surface L/min at that depth
        if rate_at_depth <= 0:
            continue
        minutes = usable_litres / rate_at_depth
        print(
            f"      {depth:5.0f} m : {format_minutes(minutes):>12} "
            f"({rate_at_depth:.1f} L/min)"
        )
    print()


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

    depths = parse_depths(args.depths)

    print(f"Parsed {len(dives)} dive(s) from {args.uddf}\n")
    for dive in dives:
        report_dive(dive, depths, args.reserve)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
