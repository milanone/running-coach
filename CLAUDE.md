# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
General rules for all of Francesco's projects (stack, code conventions, repository rules, git/GitHub, the
Drive mirror, how he works) are in `..\CLAUDE.md`; this file has the project-specific details.

## Purpose

Analyzer for running sessions exported from intervals.icu (activities recorded with Samsung Health): per-km splits,
efficiency factor, cardiac drift, TSS, altitude, sprint detection, Open-Meteo weather, a local history file and advice
for the next session. A Tkinter GUI (`running-coach-gui.pyw`) on top of a logic module (`parse_run_csv.py`) and the advanced
metrics (`run_metrics.py`). The command-line tool was removed on Francesco's request (2026-10-13): `main()`, `analyze()`, the printing
`recommendations()`, `is_latest` and `format_hms` went with it.

## Running / tests

```bash
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

- `parse_run_csv.py`: the logic (no command line): `load_csv` / `extract_points` (the `time` column may be an ISO
  timestamp or elapsed seconds, with the start taken from the file name), `compute_distances` (the file's `distance` column, else
  haversine GPS with an estimate for the pre-GPS lock-up), `km_splits` (altitude at the start and end of each km, read as saved: no filtering and no ascent total, removed on purpose, see the note under *Run detail tab*),
  `detect_sprint` (last full km faster by `SPRINT_PACE_S` and `SPRINT_HR_BPM` higher than the median), `cardiac_drift`
  (warm-up `WARMUP_S` and sprint excluded), `compute_tss`, `compute_ef`, `fetch_weather` (Open-Meteo, silent on failure),
  `compute_recommendations`, the history I/O (`load_history`, `save_history`, `upsert`, `merge_sessions`) and
  `analyze_file` / `import_sessions` used by the GUI. Personal parameters are constants at the top (`THRESHOLD_HR`,
  `EF_BASELINE`, the `DRIFT_*` thresholds...).
- `run_metrics.py`: the advanced metrics, see the section below.
- `running-coach-gui.pyw`: class `App` (Tkinter): History tab (sortable table, per-km detail), Charts tab
  (hand-drawn on a `tk.Canvas`, with a 3-session moving average), no Next session tab (the advice for the next run is the top block of the Fitness tab: `run_metrics.next_session`, the core's `compute_recommendations` refined with the load ratio and the pace at a fixed heart rate). The import runs in a thread and
  reports through a queue polled by `_poll`; `import_now()` is the synchronous version used by the tests.
  The data folder is remembered in `~/.running_coach_gui.json`.

## Advanced metrics (`run_metrics.py`)

Pure functions (no Tk), imported by the GUI as `rm`; `run_metrics(pts, athlete, speed_ref)` returns the per-run dict, `compute_all(data_dir,
history, athlete)` the table over the history (per-file cache `run_metrics_cache.json` keyed by file mtime, merge file mtime, athlete values,
reference speed and `CACHE_VERSION`: bump it when a formula changes), `summary()` / `summary_lines()` the Fitness tab text, `run_lines()` the
per-run text. The athlete's parameters (`athlete.json`: `hr_max`, `hr_rest`, `mass_kg`, `cadence_factor`, `hr_ref`, `hr_max_estimated`) live in the private data folder
and are Francesco's own; the defaults in the code are generic on purpose (this repo is public: never write his body values here).
The GUI computes them lazily in a thread (`ensure_fitness`, only when the Fitness tab or Charts on an `adv.` metric is selected): the thread gets
plain values and a `threading.Event` to cancel, never `self` (a Tk object released in another thread crashes with "Tcl interpreter is leaked").

What each estimate rests on, and how far it is checked (nothing here is validated against a Garmin or a lab). Sources read on 2026-10-13:
- **Read in full**: Swain and Leutholtz 1997, *Med Sci Sports Exerc* 29:410-4, doi 10.1097/00005768-199703000-00018 (abstract via PubMed): in 63 adults on a
  cycle ergometer %HRR versus %VO2 reserve had slope 1.00 and intercept -0.1, versus 1.12 and -11.6 for %VO2max: the basis of `vo2max_estimate`
  (caveat: cycling, not running). Minetti et al. 2002, *J Appl Physiol* 93:1039-46, doi 10.1152/japplphysiol.01177.2001 (abstract via PubMed): the
  metabolic cost of running is 3.40 J/kg/m on the level, independent of speed, and rises/falls with the slope: the structure of `power_estimate`
  (the 0.31 between metabolic and the 1.04 is mine). Firstbeat white papers (full text): "Automated Fitness Level (VO2max) Estimation with
  Heart Rate and Speed Data" (2014, updated 2017) and "EPOC Based Training Effect Assessment" (2005, updated 2012), both on firstbeat.com.
  Garmin's manual (Training Status): the load-ratio table.
- **From the Firstbeat VO2max paper**: their method also uses the heart rate against the speed in reliable stretches only (it drops stops, steep
  downhills, soft ground, cardiac drift), with an age-based maximum heart rate; it reports a mean absolute error of ~5% against the lab for
  their algorithm over 2690 runs, rising to about 9% (rate set 15 bpm too low) or 7% (15 bpm too high) when the maximum heart rate is wrong,
  and ~5% when the real maximum is known. None of that accuracy transfers to my simplified version; the maximum heart rate matters a lot.
- **From the Firstbeat training-effect paper**: TE is set by the PEAK EPOC reached and the athlete's activity class of the previous month, so the
  same session scores differently for different fitness; bands 1.0-1.9 minor, 2.0-2.9 maintaining, 3.0-3.9 improving, 4.0-4.9 highly improving,
  5.0 overreaching. My `aerobic_te` (TRIMP-based, no activity class) is NOT that method, only the same 0-5 reading; the recovery time is
  invented.
- **From Garmin's manual**: load ratio Low below 0.8, Optimal 0.8 to 1.4, High 1.5 to 1.9, Very high 2.0 or more, shown after two weeks; the status
  labels are only described, with no thresholds (mine are heuristics, `training_status`). The acute (7 days) and chronic (28 days) definitions are
  Firstbeat's; reading chronic as the weekly average of the 28 days is my assumption. Firstbeat's own advice (via Wareable) is an ideal ratio near
  1.0 and injury risk from about 1.5.
- **Secondary sources only (the primary text not read)**: ACSM running equation `0.2 * v + 0.9 * v * grade + 3.5` (m/min): valid above 134 m/min
  (5 mph), or from 80 m/min (3 mph) when truly jogging, so Francesco's ~128 m/min jogging is inside the jogging range. A study of athletes
  (Hippokratia, PubMed 24376318) found it 14.6% above the measured VO2max, but that is predicting a maximal test, not this submaximal use.
  Riegel 1981, *American Scientist* 69(3):285-290: the exponent 1.06 is the popular rounding; a reconstruction of his own regression gives about
  1.077 (to confirm in the paper, whose nku.edu copy my tools could not fetch); said to work best between 1.5 km and the marathon.
  Daniels-Gilbert VDOT: the constants I use match several calculators and reproduce the VDOT 50 row of Daniels' table (5 km 19:56 vs 19:57,
  10 km 41:20 vs 41:21, marathon 3:10:40 vs 3:10:49); the book itself was not read.
- **Mine, not from a source**: the TE scale and the recovery time (`4 * TE^2` hours), the power coefficient 1.04, `LOW_WEEKLY_TSS` = 100 (below it a
  high ratio is "Ramping up"), the performance-condition window (minutes 6-20, median of the previous ten runs), Edwards' zone weights 1-5
  (from memory of a book).
- Francesco runs very slowly (~7.7 km/h, 128 m/min, below the ACSM running threshold of 134 and inside the jogging range): the VO2max has an extra,
  unknown bias (ACSM's 0.2 ml/kg/m is above the 3.4 J/kg/m = 0.16 ml/kg/m Minetti measured in 10 runners, so it may read high), and the VDOT race predictions
  (5 km ~22 min from a VO2max of ~47) are what a trained runner would run, far from his real pace (7:50/km): the summary says so. His maximum heart
  rate was an estimate at first (`hr_max_estimated` set: the VO2max is then also given for +-15 bpm, it moves by about +-8%); the watch later
  showed a real maximum close to it, so the athlete file now holds that value and no flag.
- On Francesco's 35 runs the per-run VO2max estimate scatters between about 40 and 49 (+-10%), the median of five gives ~45; use the trend.
  The acute/chronic ratio with exponential averages (CTL/ATL) swung between 0.4 and 1.5 for a once-a-week runner, so the ratio uses the 7-day
  and 28-day sums instead.

## Clipped heart rate (found 2026-10-14)

In Francesco's CSVs from intervals.icu the heart rate of the hard runs was a flat line at one value (the same in four runs: a ceiling, never
exceeded in 35 runs) while the watch and the Health Sync FIT/TCX/GPX had values up to ~20 bpm higher (checked per second, FIT versus CSV: the same
values below the ceiling, the real ones above). Cause, read with his API key (`GET /athlete/0`, read-only): the `sportSettings` of `Run`
had a `max_hr` equal to that ceiling (the `Ride` one was higher), and intervals.icu cuts a heart rate above the sport's maximum as a spike
(forum.intervals.icu/t/max-hr-setting-and-hr-incorrectly-fixing-hr-spikes-values/679: raise the top of zone 7 / the maximum, then "Update zones or
reprocess original file" on the activity). That setting is his account's: not changed from here (ask first). Two protections in the code:
`parse_run_csv.apply_fit_hr` (the FIT of the same name in `FIT_DIR` replaces the CSV heart rate when it agrees with it below the clip; two records
per second are averaged, the first FIT record is second 0 of the CSV; `fitparse` is optional and imported lazily; GUI box *Health Sync folder*,
`~/.running_coach_gui.json` key `fit_dir`, or `$RUNNING_COACH_FIT`) and `run_metrics._clipped_plateau` (30 s or more at exactly the run's
maximum is dropped from `series()`; a constant heart rate is not treated as clipped, so synthetic runs are unaffected). The metrics cache key
includes the FIT file's mtime (`CACHE_VERSION` 3). Tests use a stub `fitparse` module: no real FIT file is in the repo.

## Run detail tab

`analyze_file` also returns `points` (the per-second dicts: `ts`, `hr`, `speed` (m/s, the `velocity_smooth` column), `cadence`, `alt_raw`
(`altitude`), `alt_fixed` (`fixed_altitude`, intervals.icu's corrected elevation), `lat`/`lon`, `dist_m`). The tab draws them on a `tk.Canvas`
(`RUN_PANELS`: one panel per quantity; the speed panel can be switched to pace, `run_speed`: `pace` = 60 / km/h, None below 3 km/h, axis flipped, range = 1st-99th percentile with the line clamped to it, shared X axis time/distance, a hover readout). Nothing is smoothed or resampled. For a merged outing the
timeline is the concatenated one (pauses removed). Do not publish a screenshot of a real run: the readout shows GPS coordinates; the README
one comes from a synthetic run generated by the screenshot script.

Where intervals.icu's CSV comes from (checked 2026-10-13 on the run of 12/10 against the Health Sync FIT and TCX): it is built from the **FIT**,
not from FIT + TCX: lat/lng/altitude identical per second, heart rate/cadence/speed identical in 90-96% of the seconds (1 s alignment), 1 Hz
resampled, `velocity_smooth` = the FIT `speed`, `distance` = integral of that speed, `fixed_altitude` its own correction (checked 2026-10-13 on Francesco's out-and-back road, ~976 m with a constant ~0.65% slope, 58 m to 52 m: `altitude` is linear in the position along the road, R2 0.98-0.99 per leg, same slope both ways; `fixed_altitude` wiggles by +-2 m, R2 0.07-0.59, so it is NOT used: `extract_points` prefers `altitude`). The TCX has no speed,
so its distance can only come from the GPS (2.6% shorter) and its lap total is unreliable.

## Map tab

`draw_map` (interactive: `map_press`/`map_drag` pan, `map_zoom` on the wheel keeps the world pixel under the cursor fixed, `map_hover`, `map_fit`; the view is `map_view` = (zoom, centre x, centre y in world pixels), `None` = fit the track, reset on every new selection) puts the GPS track (the `lat`/`lon` of the points kept for the Run detail tab) over OpenStreetMap tiles on a `tk.Canvas`; no new
dependency: the PNG tiles are loaded with `tk.PhotoImage` (Tk 8.6 reads PNG), the Web-Mercator maths is in the core (`world_px`, `fit_zoom`,
`fetch_tile`, tested without Tk). The first zoom is the highest one at which the bounding box fits the canvas. The track is coloured by `velocity_smooth` in 12 buckets between the 5th and 95th percentile
(`_draw_speed_track`, consecutive segments of one colour are drawn as one canvas line, to keep the redraw small: it runs on every mouse move while dragging). Tiles are fetched one at a time by ONE
persistent worker thread (`_tile_worker`, fed by `tile_req`, answers polled by `_tile_poll` through `tile_queue`; it skips tiles no longer in `map_wanted`, so a fast drag
does not download what was already scrolled away) and cached in `~/.running_coach_tiles` (outside the repos, the data
folder and the Drive mirror). Nothing is downloaded unless the Map tab is the selected one. The tile policy requires an identifying `User-Agent`,
a cache and the visible attribution "(c) OpenStreetMap contributors" (drawn on the canvas): keep all three. Checked live on 2026-10-13 on the run of
12/10 (15 tiles in ~3 s, track on the right streets). Do not publish a screenshot of a real run's map (it shows where Francesco runs): the README one is a
synthetic loop made by the screenshot script. The map, like the weather, talks to the network; the tests mock `fetch_tile`.

## intervals.icu download

`download_new_runs()` lists the activities (`/athlete/0/activities`, `0` = the athlete of the key, or `$INTERVALS_ATHLETE_ID`),
keeps `Run` / `TrailRun` / `VirtualRun`, and saves `/activity/<id>/streams.csv` as `YYYY.MM.DD HH.MM-RUNNING.csv` (local start)
unless the file exists. The streams are byte-identical to the manual export (checked on the 16 runs already in the data folder).
Why this and not the Health Sync TCX/FIT files in Drive: their lap distance is unreliable (doubled, halved, or wrong) and early runs
have a bad GPS start; intervals.icu already "cooks" the distance. Gotchas: a `User-Agent` header is mandatory (Cloudflare answers
403 / error 1010 to the default Python one); the key comes from `$INTERVALS_API_KEY` or `intervals_api_key.txt` (gitignored, and
excluded from the Drive mirror in `mirror_to_drive.bat`; never print it). Tests mock `intervals_get`; no test touches the network.
Verified live on 2026-10-13: 37 runs listed since April, 16 identical to the existing files, 21 new (June-October).

## Merged outings

GUI: select two or more rows in the History table (extended selection), right-click, *Merge runs* / *Unmerge runs*
(`merge_selected`, `unmerge_selected`, backed by `merge_runs()` / `unmerge_run()` in the core, which also write the JSON below).
The popup menu was checked on the real GUI; the tests call the same methods but not `tk_popup` (it blocks).

`merged_sessions.json` in the data folder (`merged_members()`) maps a primary session to the ones recorded after a pause; `analyze_file`
joins their points with `concat_runs()`: later parts are shifted in time (the pause must not count, `km_splits` works on timestamp
differences) and their distance continues from the previous total. `new_run_files` skips the members and recomputes the primary
while any member row is still in the history; `load_folder_history` drops member rows. The file lives in the private data repo. First case:
the three sessions of 2026-08-18 (3.07 + 1.90 + 1.05 km, pauses of 12 and 1.5 min), decided by Francesco as one outing: 6.02 km in 52:00.
The rule for what to merge is manual on purpose (no automatic "same day, short pause" heuristic: it could join a run and a walk).

## History file format

Columns (`HISTORY_FIELDS`): `date` (dd/mm/yyyy), `time`, `dist_km`, `duration_hms`, `pace_minkm`, `avg_hr_bpm`,
`ef_m_beat`, `drift_pct`, `tss`, `temp_c`, `humidity_pct`, `wind_kmh`.
Sessions are keyed by (ISO date, time), dates are accepted as dd/mm/yyyy or yyyy-mm-dd, and a re-import keeps the weather already recorded.

**Legs removed (2026-10-13):** the subjective "legs" rating (`legs`, asked after each run, +1 rest day / shorter run / slower
pace when sore) was dropped from the history, the CLI prompt, the GUI and `compute_recommendations(history, session)`; the advice
now rests on TSS, drift and EF trend only. Old history files with a `legs` / `gambe` column still load (the column is ignored
and disappears on the next save).

**Translation (2026-10-08):** the columns used to be Italian (`data, orario, dist_km, durata_hms, passo_minkm,
fc_media_bpm, ef_m_batt, drift_pct, tss, temp_c, umidita_pct, vento_kmh, gambe`). `LEGACY_FIELDS` / `migrate_legacy_row()` read such files, which are rewritten
with the English names on the next save; `test_legacy_italian_history_is_read_and_migrated` covers it. The private
`running_coach_data/running_history.csv` has **not** been touched: it migrates the first time the app saves it.

## Verified / not verified

- The logic is tested on synthetic runs only; the formulas (EF, drift, TSS with `THRESHOLD_HR` 170) come from the
  original script and were not re-derived. The real history (16 sessions) was read and re-saved in a temporary copy.
- The sprint rule, the advice thresholds and the pace/rest arithmetic are heuristics, not validated training science.
- Weather sends the start coordinates to Open-Meteo unless the GUI box is off (or `FETCH_WEATHER` is False).

## PlotStyleKit

Does not apply: this project draws its charts on a Tkinter canvas and deliberately uses no third-party package. Moving the
charts to matplotlib with PlotStyleKit (consistent style, figure save/edit) would be a design change; ask Francesco first.

## Editing conventions (project)
- Follow `..\CLAUDE.md`: everything in English, surgical edits. The project was translated from Italian in one pass
  (console output, GUI, column names, tests); `howto.txt` is a personal, gitignored note and stays in
  Italian.
- Never commit anything from the data folder.
