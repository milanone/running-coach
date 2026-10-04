# running-coach

Command-line analyzers for running sessions. They read an activity export, compute a few
training-load metrics, and keep a local history so each new session can be compared with the
previous ones.

| Script | Input | Notes |
|---|---|---|
| `parse_run_csv.py` | CSV exported from [intervals.icu](https://intervals.icu) | Main tool: per-km table, session metrics, weather, sprint detection, recommendations |
| `parse_run_fit.py` | `.fit` activity file | Simpler, earlier tool: per-km table and a traffic-light health status |

Console output and the history column names are in Italian.

## Metrics

- **Efficiency Factor (EF)** — metres covered per heartbeat; higher means better aerobic efficiency.
- **Cardiac drift** — how much heart rate rises relative to pace over the session (%); a high
  value points to fatigue, heat or dehydration.
- **TSS** — a training-stress score from duration and heart rate relative to threshold.
- **Elevation** — ascent/descent with a GPS-noise filter, used to judge whether a high drift is
  explained by the slope.

## parse_run_csv.py

```
python parse_run_csv.py                  # process every "YYYY.MM.DD HH.MM-RUNNING.csv" in the folder
python parse_run_csv.py "file.csv"       # process a single file
```

1. Export the activity as CSV from intervals.icu.
2. Rename it to `YYYY.MM.DD HH.MM-RUNNING.csv` (date and start time of the run) and put it
   next to the script.
3. Run the script. It asks how your legs feel (fresh / ok / heavy / sore) and uses the answer
   in the recommendations for the next session.

It prints a per-km breakdown, EF, cardiac drift, TSS, automatic sprint detection (final km)
and recommendations, then appends or updates the session in `running_history.csv`.

**Weather lookup.** For each session it queries the
[Open-Meteo](https://open-meteo.com) historical API with the **start coordinates** of the
activity to record temperature, humidity and wind. If the request fails the script continues
without weather data. Remove the call to `fetch_weather()` if you do not want coordinates sent
to an external service.

## parse_run_fit.py

```
pip install fitparse
python parse_run_fit.py "activity.fit"
```

Prints a per-km table with pace, heart rate and elevation change, session metrics, and a
traffic-light status (green / yellow / orange / red) that turns red after three consecutive
sessions with cardiac drift above 15%.

**Separate history file.** This script writes its own simple log (`timestamp,drift,EF` per line)
to `running_history.csv`, a different format from the one `parse_run_csv.py` produces.
Use the two scripts in different folders, or change `LOG_FILE` in one of them, otherwise they
will not read each other's history correctly.

## Configuration

Personal parameters are constants at the top of each script. Edit them to match your own values:

| Constant | Meaning |
|---|---|
| `FC_MAX`, `FC_REST`, `LTHR` (`.fit` script) | Maximum, resting and threshold heart rate (bpm) |
| `THRESHOLD_HR`, `EF_BASELINE` (CSV script) | Threshold heart rate and baseline efficiency |
| `SPRINT_PACE_S`, `SPRINT_HR_BPM` (CSV script) | How much faster / higher than the median flags a sprint |

## Privacy

This repository contains code only. `.gitignore` excludes `*.csv`, `*.fit`, `*.tcx`, `*.gpx`
and `running_history.csv`, so activity exports, GPS tracks and your history never get committed.
Keep your own data files out of version control.

## Requirements

- Python 3
- `parse_run_csv.py`: standard library only
- `parse_run_fit.py`: [`fitparse`](https://pypi.org/project/fitparse/)
