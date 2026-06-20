# rest-diving-time-calculator

Estimate the **remaining** ("Rest") dive time at 5, 10 and 15 metres from the
air left in the tank at the end of a dive, based on the consumption profile
recorded in a [MacDive](https://www.mac-dive.com/) **UDDF** export.

Given a dive log that contains tank-pressure samples, the script:

1. measures how fast the diver actually breathed **at each target depth**
   during the dive, and
2. projects how much longer they could have stayed at a constant depth on the
   gas remaining at the end of the dive, drawn down to a reserve pressure.

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

| Option           | Default   | Description                                                       |
|------------------|-----------|------------------------------------------------------------------|
| `--depths`       | `5,10,15` | Comma-separated target depths in metres.                         |
| `--reserve`      | `50`      | Reserve pressure (bar) to keep in the tank.                      |
| `--depth-window` | `2.5`     | Half-width (m) of the band used to measure consumption per depth.|
| `--sort`         | `file`    | Output order of dives: `file` (as exported) or `number`.         |
| `--html`         | –         | Write a styled, self-contained HTML report to a path.            |

Example with custom depths, a 40 bar reserve, sorted by dive number:

```bash
python3 rest_diving_time.py export.uddf --depths 3,6,12,20 --reserve 40 --sort number
```

### HTML report

For easy viewing in a browser, write the prognosis to a standalone HTML page
(no external assets, just open it):

```bash
python3 rest_diving_time.py examples/sample_macdive.uddf --html report.html
```

## Example output

```
Dive 2
  date / time          : 2026-06-20T14:30:00
  samples              : 8
  duration             : 27m 00s
  max depth            : 15.0 m
  average depth        : 10.1 m
  tank volume          : 12.0 L
  start pressure       : 200 bar
  end pressure         : 132 bar
  gas used (sampled)   : 68 bar
  RMV (surface)        : 15.0 L/min
  remaining gas        : 986 L (from 132 bar, 50 bar reserve)
  remaining-time prognosis (constant depth, down to 50 bar):
          5 m :      42m 16s (23.3 L/min, measured)
         10 m :      33m 31s (29.4 L/min, measured)
         15 m :      26m 36s (37.1 L/min, measured)
```

Each prognosis line is tagged **measured** when the rate was taken from time
the diver actually spent near that depth, or **modelled** when they never went
there and the rate had to be derived from the surface RMV.

## How it works

UDDF stores everything in SI units, which the script converts internally:

* depth in metres, dive time in seconds,
* tank pressure in **pascal** (200 bar = `20000000`),
* tank volume in **cubic metres** (a 12 L cylinder = `0.012`).

**RMV (Respiratory Minute Volume / SAC)** is the diver's breathing rate
normalised to the surface. It is reported the way dive logs (including MacDive)
do — total gas consumed over the whole dive at the average depth:

```
RMV = (dP_total * tank_volume) / (dive_time * P_ambient(avg_depth))
```

where `dP_total` is the begin-to-end tank-pressure drop (bar), `tank_volume` is
in litres, `dive_time` is in minutes and `P_ambient = 1 + depth / 10` is the
absolute ambient pressure (bar). Using the *total* dive time (not only the
intervals where pressure dropped) is what keeps the figure in line with
MacDive even when the air-integration log has gaps.

**Remaining-time prognosis** at a target depth `d`:

```
consumption at d  = gas actually used near depth d, per minute   [surface L/min]
                    (falls back to RMV * (1 + d / 10) if never dived there)
remaining gas     = (end_pressure - reserve) * tank_volume       [surface L]
time at depth     = remaining gas / consumption at d
```

The consumption "near depth `d`" is measured from every profile interval whose
mean depth is within `--depth-window` metres of `d`.

## Limitations

* The prognosis assumes a constant depth and a steady breathing rate; it does
  **not** model decompression / no-stop limits, so always cross-check against
  your dive computer or tables.
* Remaining time needs a tank volume **and** an end pressure. If the dive
  already ended at or below the reserve, the remaining time is zero.
* If the dive never visited a target depth, that depth's rate is **modelled**
  from the surface RMV rather than measured.
```
