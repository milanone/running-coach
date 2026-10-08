# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
General rules for all of Francesco's projects (stack, code conventions, repository rules, git/GitHub, the
Drive mirror, how he works) are in `..\CLAUDE.md`; this file has the project-specific details.

## Purpose

Analyzer for running sessions exported from intervals.icu (activities recorded with Samsung Health): per-km splits,
efficiency factor, cardiac drift, TSS, elevation, sprint detection, Open-Meteo weather, a local history file and advice
for the next session. A command-line tool (`parse_run_csv.py`) and a Tkinter GUI (`running-coach-gui.pyw`) share the same
logic.

## Running / tests

```bash
py parse_run_csv.py [--data FOLDER] [--no-weather] [--all] [file.csv ...]
pythonw running-coach-gui.pyw
py -m unittest discover -s tests -v
```

The tests use synthetic runs from `tests/synth.py` (intervals.icu-style CSV with the Samsung quirks: doubled distance, GPS
lock-up at the start, optional final-km sprint) and need no real data; the GUI smoke tests need a screen. Real data
lives in the **private** repo `milanone/running-coach-data` (cloned as `..\running_coach_data`): CSVs named
`YYYY.MM.DD HH.MM-RUNNING.csv` and `running_history.csv`. Never copy real activity files or the history into this
repo (the `.gitignore` excludes them). The two `screenshot*.png` come from the real GUI on synthetic runs; the data
folder shown in the first one is a placeholder typed into the entry after loading.

## Architecture

- `parse_run_csv.py`: the logic (and the CLI in `main()`): `load_csv` / `extract_points` (the `time` column may be an ISO
  timestamp or elapsed seconds, with the start taken from the file name), `compute_distances` (the file's `distance` column, else
  haversine GPS with an estimate for the pre-GPS lock-up), `elevation_gain` (hysteresis `NOISE_ELEV_M`), `km_splits`,
  `detect_sprint` (last full km faster by `SPRINT_PACE_S` and `SPRINT_HR_BPM` higher than the median), `cardiac_drift`
  (warm-up `WARMUP_S` and sprint excluded), `compute_tss`, `compute_ef`, `fetch_weather` (Open-Meteo, silent on failure),
  `compute_recommendations`, the history I/O (`load_history`, `save_history`, `upsert`, `merge_sessions`) and
  `analyze_file` / `import_sessions` used by the GUI. Personal parameters are constants at the top (`THRESHOLD_HR`,
  `EF_BASELINE`, the `DRIFT_*` thresholds...).
- `running-coach-gui.pyw`: class `App` (Tkinter): History tab (sortable table, per-km detail, legs editor), Charts tab
  (hand-drawn on a `tk.Canvas`, with a 3-session moving average), Next session tab. The import runs in a thread and
  reports through a queue polled by `_poll`; `import_now()` is the synchronous version used by the tests.
  The data folder is remembered in `~/.running_coach_gui.json`.

## History file format

Columns (`HISTORY_FIELDS`): `date` (dd/mm/yyyy), `time`, `dist_km`, `duration_hms`, `pace_minkm`, `avg_hr_bpm`,
`ef_m_beat`, `drift_pct`, `tss`, `temp_c`, `humidity_pct`, `wind_kmh`, `legs` (`fresh` / `ok` / `heavy` / `sore`).
Sessions are keyed by (ISO date, time), dates are accepted as dd/mm/yyyy or yyyy-mm-dd, and a re-import keeps the weather and
legs already recorded.

**Translation (2026-10-08):** the columns used to be Italian (`data, orario, dist_km, durata_hms, passo_minkm,
fc_media_bpm, ef_m_batt, drift_pct, tss, temp_c, umidita_pct, vento_kmh, gambe`) and `legs` was
`fresche/ok/pesanti/dolenti`. `LEGACY_FIELDS` / `LEGACY_LEGS` / `migrate_legacy_row()` read such files, which are rewritten
with the English names on the next save; `test_legacy_italian_history_is_read_and_migrated` covers it. The private
`running_coach_data/running_history.csv` has **not** been touched: it migrates the first time the app saves it.

## Verified / not verified

- The logic is tested on synthetic runs only; the formulas (EF, drift, TSS with `THRESHOLD_HR` 170) come from the
  original script and were not re-derived. The real history (16 sessions) was read and re-saved in a temporary copy.
- The sprint rule, the advice thresholds and the pace/rest arithmetic are heuristics, not validated training science.
- Weather sends the start coordinates to Open-Meteo unless `--no-weather` or the GUI box is off.

## PlotStyleKit

Does not apply: this project draws its charts on a Tkinter canvas and deliberately uses no third-party package. Moving the
charts to matplotlib with PlotStyleKit (consistent style, figure save/edit) would be a design change; ask Francesco first.

## Editing conventions (project)
- Follow `..\CLAUDE.md`: everything in English, surgical edits. The project was translated from Italian in one pass
  (console output, GUI, column names, `legs` values, tests); `howto.txt` is a personal, gitignored note and stays in
  Italian.
- Never commit anything from the data folder.
