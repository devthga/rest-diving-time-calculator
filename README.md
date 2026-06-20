# rest-diving-time-calculator

Estimate how long a diver *could have stayed* at 5, 10 and 15 metres, based on
the air-consumption profile recorded in a [MacDive](https://www.mac-dive.com/)
**UDDF** export.

Given a dive log that contains tank-pressure samples, the script:

1. derives the diver's real air-consumption rate (RMV) from the recorded
   depth / time / tank-pressure profile, and
2. projects how long that same diver could have remained at a constant depth on
   the gas they carried.

Only the Python standard library is required (Python 3.10+).

## Usage

Pass the UDDF export as an **argument to the script** — don't run the `.uddf`
file directly (`python your_export.uddf` will fail with a `SyntaxError`, because
the data file is XML, not Python):

```bash
python3 rest_diving_time.py path/to/export.uddf
```

Try it with the bundled example:

```bash
python3 rest_diving_time.py examples/sample_macdive.uddf
```

Options:

| Option       | Default   | Description                                            |
|--------------|-----------|--------------------------------------------------------|
| `--depths`   | `5,10,15` | Comma-separated target depths in metres.               |
| `--reserve`  | `50`      | Reserve pressure (bar) to keep in the tank.            |
| `--html`     | –         | Write a styled, self-contained HTML report to a path.  |

Example with custom depths and a 40 bar reserve:

```bash
python3 rest_diving_time.py export.uddf --depths 3,6,12,20 --reserve 40
```

### HTML report

For easy viewing in a browser, write the prognosis to a standalone HTML page
(no external assets, just open it):

```bash
python3 rest_diving_time.py examples/sample_macdive.uddf --html report.html
```

## Example output

```
Dive dive1
  samples              : 9
  duration             : 25m 00s
  max depth            : 20.0 m
  average depth        : 14.4 m
  tank volume          : 12.0 L
  start pressure       : 200 bar
  end pressure         : 33 bar
  gas used (sampled)   : 167 bar
  RMV (surface)        : 32.8 L/min
  usable gas           : 1800 L (from 200 bar, 50 bar reserve)
  rest-time prognosis (constant depth, 50 bar reserve):
          5 m :      36m 33s (49.2 L/min)
         10 m :      27m 25s (65.7 L/min)
         15 m :      21m 56s (82.1 L/min)
```

## How it works

UDDF stores everything in SI units, which the script converts internally:

* depth in metres, dive time in seconds,
* tank pressure in **pascal** (200 bar = `20000000`),
* tank volume in **cubic metres** (a 12 L cylinder = `0.012`).

**RMV (Respiratory Minute Volume)** is the diver's breathing rate normalised to
the surface, so it does not depend on depth. It is computed from every interval
in which the tank pressure dropped:

```
RMV = sum( dP_tank * tank_volume ) / sum( dt * P_ambient )
```

where `dP_tank` is the pressure drop over an interval (bar), `tank_volume` is in
litres, `dt` is the interval length (minutes) and
`P_ambient = 1 + depth / 10` is the absolute ambient pressure (bar). Intervals
where the pressure rises (sensor noise or gas switches) are ignored.

**Rest-time prognosis** at a target depth `d`:

```
gas rate at depth = RMV * (1 + d / 10)          [surface L/min]
usable gas        = (start_pressure - reserve) * tank_volume   [surface L]
time at depth     = usable gas / gas rate at depth
```

## Limitations

* The prognosis assumes a constant depth and a steady breathing rate; it does
  **not** model decompression / no-stop limits, so always cross-check against
  your dive computer or tables.
* A dive needs a tank volume **and** tank-pressure samples for an RMV to be
  computed. Dives without air-integration data are reported but skipped for the
  prognosis.
```
