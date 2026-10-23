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
  haversine GPS with an estimate for the pre-GPS lock-up), `km_splits` (altitude at the start and end of each km, read as saved: no filtering and no ascent total, removed on purpose, see the note under *Chart view*),
  `detect_sprint` (last full km faster by `SPRINT_PACE_S` and `SPRINT_HR_BPM` higher than the median), `cardiac_drift`
  (warm-up `WARMUP_S` and sprint excluded), `compute_tss`, `compute_ef`, `fetch_weather` (Open-Meteo, silent on failure),
  `compute_recommendations`, the history I/O (`load_history`, `save_history`, `upsert`, `merge_sessions`) and
  `analyze_file` / `import_sessions` used by the GUI. Personal parameters are constants at the top (`THRESHOLD_HR`,
  `EF_BASELINE`, the `DRIFT_*` thresholds...).
- `run_metrics.py`: the advanced metrics, see the section below.
- `running-coach-gui.pyw`: class `App` (Tkinter): two tabs, **Today** (the cards, the best efforts, *Trends* and *Where you stand*) and **Runs** (one list of
  sessions and, for the selected run, *Summary*, *Chart*, *Map* and *Gauges*), see *Layout of the window* below. The advice for the next run is the first card
  of Today: `run_metrics.next_session`, the core's `compute_recommendations` refined with the load ratio and the pace at a fixed heart rate. The import runs in a thread and
  reports through a queue polled by `_poll`; `import_now()` is the synchronous version used by the tests.
  The data folder is remembered in `~/.running_coach_gui.json`.

## Advanced metrics (`run_metrics.py`)

Pure functions (no Tk), imported by the GUI as `rm`; `run_metrics(pts, athlete, speed_ref)` returns the per-run dict, `compute_all(data_dir,
history, athlete)` the table over the history (per-file cache `run_metrics_cache.json` keyed by the file's mtime, the mtimes of the recordings merged into it and of its FIT file, the athlete values
(not age and sex) and `CACHE_VERSION`: bump it when a formula changes. NOT keyed by the reference speed or the merge file: they change with every new
run or merge, which once made the whole history be read again at every tab open; the heart rate at the reference speed is worked out in
`compute_all` from `hr_speed_table`, kept with each run). A cold start (no file, a new `CACHE_VERSION`) was 22 s for 37 runs, 85% of it in
`extract_points` trying six date formats on every second of a CSV whose time is elapsed seconds (`NUMBER` now skips that) and 0.4 s per run in
reading a FIT (`FIT_MIN_AT_MAX`: only for a CSV that looks clipped); it is ~1.6 s now, serial. A pool of processes was tried (1.2 s) and dropped: it
saves little and spawning children from a `.pyw` on Windows is fragile. The Today tab has a *Recompute all metrics* button (deletes the file), `summary()` the figures behind the Today tab, `dashboard(adv, summ)` and `run_panel(m, row, details)` that same tab as plain data (the cards: next session,
training status, VO2max, race predictions, the best efforts; the summary of a run: facts from its history row, metric tiles, zone minutes, repetitions, notes, and it works with `m` = None while the metrics
are being computed), tested without Tk;
`summary_lines()` / `run_lines()` are the older text forms, still there. The athlete's parameters (`athlete.json`: `hr_max`, `hr_rest`, `mass_kg`, `cadence_factor`, `hr_ref`, `hr_max_estimated`, `age`, `sex`, `height_cm`) live in the private data folder
and are Francesco's own; the defaults in the code are generic on purpose (this repo is public: never write his body values here).
The GUI computes them in a thread whenever the history changes (`start_fitness`; it was lazy, per tab, until the list and the metrics became one, because the
list has metric columns and the file of results makes it quick): the thread gets
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

## Layout of the window (redone 2026-10-23, "plan A")

The window opens at `WINDOW_FRACTION` (0.8) of the screen in both directions, centred, never below `MIN_WINDOW` (900x600) nor beyond the screen
(`window_geometry(screen_w, screen_h)`, a pure function, tested).

Before there were a History list, a Fitness list (nearly the same rows), a gauges drop-down and four tabs that did not share a selection, and the Fitness
cards did not change with the run. Now: **Today** = `_build_today_tab` (header with `fit_message` and *Recompute all metrics*, the strip of four cards
(`_card`, `_fill_dashboard` from `rm.dashboard`), the best efforts, then a notebook *Trends* (`_build_chart_tab`, `draw_chart`) and *Where you stand*). **Runs** =
`_build_runs_tab`: a `PanedWindow` with the list (`self.tree`, extended selection, one iid `date|time` per run) and `self.run_tabs` (Notebook: `summary_tab`,
`run_tab` = Chart, `map_tab`, `gauge_tab`). ONE selection drives them all: `show_detail` (bound to the list) reads the CSV, fills the facts and the km table,
and calls `show_run_summary` (`rm.run_panel(metrics, row, run_extra)`), `draw_gauges`, `draw_run`, `draw_map`; hidden canvases do nothing until they have a size, and the map
draws only when `_run_view_visible(map_tab)` (the tile policy). The list: `RUN_COLUMNS` (history fields, metrics from `compute_all` through `METRIC_CELLS`, and
`note`), `DEFAULT_COLUMNS` shown, the *Columns* menu (`toggle_column`, `displaycolumns`), filters (`filtered_history`: `KIND_FILTERS`, `PERIOD_FILTERS`),
sorting on any column, rows tagged `merged` / `intervals`. The metrics arrive from the thread after the list is drawn: `refresh_cells` fills their columns
in place (no new selection event, so no reload of the run); a sort by a metric column rebuilds the list instead. Config keys `columns` and `last_run` bring
back the columns and the open run. `show_run_tab(tab)` / `show_today_tab(tab)` are how tests and code open a view. A cancelled metrics thread can still hold
a CSV open, so `fit_threads` keeps all of them and `_close` joins them (a temp directory could not be deleted on Windows otherwise). The first attempt put the cards in
a vertical `PanedWindow` and they were squashed to nothing: plain `pack` instead. The Summary tab used to have a text block with the facts of the run (date, km,
duration, HR, EF, drift, TSS, weather, altitude, notes) above a formatted panel that repeated most of it, plus a km table beside it: the facts are now tiles of the
panel (`run_panel(m, row, details)`; `details` = altitude and notes from `_show_detail`, which only reads the CSV and fills `run_kms` and `run_extra`), the km table is
part of the panel, and `_show_fit_panel` has two blocks that sit side by side when `_layout_panel` finds room (measured with `winfo_reqwidth`) and one under the
other when not. The gauge cards (`_gauge_cards`) take as many columns as fit, up to three (each needs ~250 px), the dial and the text scale with the card (`_draw_gauge(cv, g, w, h)`),
and `_gauges_resized` draws them again, once the window has stopped changing width, for the new width. Checked on the real window at 1000x700 and 1200x800. `apply_fitness` is also where `fit_view` (what the cards show) is set.
Open and not done here: the best efforts of an interval workout over 3 km include the recoveries, and the Riegel prediction can start from one.

## Gauges tab

`run_metrics.gauges(m, session, summ)` (pure, tested without Tk) returns one dict per value with a reference range: load ratio and form
(as of today, from `summ["load"]`), drift and average heart rate as % of the maximum (from the history row), training effect, performance
condition and heart-rate recovery (from the run's metrics) and the VO2max (`vo2_gauge`: the median of the last five runs against the Cooper Institute's percentiles by decade of age and sex, `VO2MAX_NORMS`: Garmin manuals, two read and identical, the unit is not printed there; 20-79 only; the estimate's own spread is said in the card). Each has its `bands` (from, to, tone, label, advice), the scale `lo`..`hi`, the
verdict of the band the value falls in, `why` and `source`, and `level`: `sourced` (a source was read: Garmin manual load-ratio table,
Firstbeat training-effect bands; heart-rate recovery: Cole 1999 (12 bpm, with a cool-down walk) and Jou et al., Eur J Prev Cardiol 2025;32:32-42, doi 10.1093/eurjpc/zwae308, found 2026-10-22 on
Consensus and PubMed and then read IN FULL (Francesco put the PDF in the folder; it is gitignored, never commit it): Table 1, quantile regression on 2242 healthy
people, HRR1 5th percentile = `36.4 + 0.26*age - 0.0051*age^2 - 0.19*resting HR`, 50th percentile = `69.1 - 0.0033*age^2 - 0.21*resting HR - 0.34*BMI` (no
age term; the first reading of this row through a summariser had garbled it), unisex; `hrr_limits` returns the walking limit (12) and the lying-down one (the
5th percentile, 18 without an age). IMPORTANT, and I had it backwards at first: the authors say a recovery lying down with no cool-down gives a BIGGER drop than the
other protocols and their equations may be less relevant when the person stays seated or walks; the literature limits are 12 with a cool-down and 18 supine. The
median (`hrr_median`) needs the BMI: `athlete.json` has `height_cm` since 2026-10-22 (entered with the Athlete button), and with the age, the mass and the
height the gauge shows four bands (below the normal range, borderline, normal range, above the median); the median is for lying down, so on foot it is
conservative. Vicente-Campos 2014 (treadmill, active men: mean 15 bpm) and Kaikkonen 2024 (novice runners,
passive: 36 bpm) show how much the protocol moves the number), `secondary` (drift 5/10% from TrainingPeaks articles, form from Friel's
race-day +15 to +25 plus a third-party table, VO2max from the reprinted Cooper tables: the primary texts were not read) or `mine` (the zones as % of the maximum, the performance
condition bands). Do not add a gauge without a range you can name a source for, and keep `level` honest: that is what the card shows. The VO2max needs `age`
and `sex` in `athlete.json` (entered with the *Athlete...* button, `edit_athlete` / `apply_athlete`, validated, saved by `run_metrics.write_athlete`, which
keeps keys it does not know); they are left out of the metrics cache key because they only place the VO2max. The text of the cards is in read-only `tk.Text` widgets (`_selectable`: typing blocked, Ctrl+C and a right-click menu work, the height
comes from a pixel-measured greedy wrap) so that it can be selected and copied; a label cannot. The GUI (`_build_gauges_tab`, `draw_gauges`,
`_draw_gauge`) draws each dial on a `tk.Canvas` with `create_arc(style="arc")`, bands as arcs and a needle; `fit_summ` is `gauge_summ`,
set by `apply_fitness`. The gauges named in `NOW_GAUGES` (load ratio, form, VO2max) are about today and are drawn on the Today tab (*Where you stand*,
`draw_now_gauges`); the others are about the selected run (`draw_gauges`, 2 columns in the Gauges view). The README screenshot is synthetic with the placeholder data path.

## Interval workouts (added 2026-10-22)

Found on Francesco's runs of 2026-10-20 (two recordings of a workout with repetitions, 22 minutes apart): the program had read them as steady
runs, with a drift of -20% and -16% and an efficiency from the average over the recoveries. `parse_run_csv.detect_intervals(pts)` returns the
repetitions or `[]`: the 10-s smoothed moving speed must have two levels (90th percentile at least `INTERVAL_CONTRAST` = 1.4 times the 10th),
fast stretches above the midpoint are merged across gaps under `INTERVAL_MIN_GAP_S` = 20 s and kept from `INTERVAL_MIN_REP_S` = 30 s, and each
pair of neighbours needs a recovery of `INTERVAL_MIN_RECOVERY_S` to `INTERVAL_MAX_RECOVERY_S` (30 s to 10 min) whose mean speed is at most
`INTERVAL_RECOVERY_RATIO` = 0.75 of the slower rep; the reps may take at most `INTERVAL_MAX_SHARE` = 70% of the run. The recovery bounds and the
ratio came from two false positives found by scanning all 40 CSVs (a steady run with a fast finish; one with a fast start and a final sprint): after
them only the two real sessions are flagged. `analyze_file` then sets `kind` = "intervals" (the history column `kind`), leaves `drift_pct` and
`ef_m_beat` empty, skips the sprint rule and takes the TSS from `tss_from_points`. `upsert` must not refill a drift or efficiency left blank on
purpose (nor `kind`), or a reprocess would bring back the old -20%. `compute_recommendations` starts from the last steady run (`base_pace_s`,
distance), adds one rest day and no progression. `run_metrics.run_metrics` skips VO2max, pace at a heart rate, heart rate at a speed, the efficiency
window, the rise time, the split balance and the pace variability for such a run and adds `reps` (`rep_details`: pace, heart rate, drop in 60 s
from the peak, which is the highest 3-s mean from 3 s before the end to 8 s after it because the heart rate lags the effort); `hrr60` is then the
mean of those drops (`hrr60_n` of them), not the drop after the end of the run, which is a cool-down. `ef_reps` (distance per heartbeat over the
repetitions) gives the performance condition against the median of up to ten earlier interval workouts, from the third on (`perf_n` counts them;
with fewer the gauge says what it needs). The gauges say "not for an interval workout" for the drift only and compute the intensity over the
repetitions only. The metrics cache is `CACHE_VERSION` 6. Joining two recordings of one workout is the existing manual *Merge runs*; the program does not do it
by itself.

## Clipped heart rate (found 2026-10-14)

In Francesco's CSVs from intervals.icu the heart rate of the hard runs was a flat line at one value (the same in four runs: a ceiling, never
exceeded in 35 runs) while the watch and the Health Sync FIT/TCX/GPX had values up to ~20 bpm higher (checked per second, FIT versus CSV: the same
values below the ceiling, the real ones above). Cause, read with his API key (`GET /athlete/0`, read-only): the `sportSettings` of `Run`
had a `max_hr` equal to that ceiling (the `Ride` one was higher), and intervals.icu cuts a heart rate above the sport's maximum as a spike
(forum.intervals.icu/t/max-hr-setting-and-hr-incorrectly-fixing-hr-spikes-values/679: raise the top of zone 7 / the maximum, then "Update zones or
reprocess original file" on the activity). That setting is his account's: not changed from here (ask first). Two protections in the code:
`parse_run_csv.apply_fit_hr` (the FIT of the same name in `FIT_DIR`, opened only when the CSV has `FIT_MIN_AT_MAX` = 20 s or more exactly at its maximum, replaces the CSV heart rate when it agrees with it below the clip; two records
per second are averaged, the first FIT record is second 0 of the CSV; `fitparse` is optional and imported lazily; GUI box *Health Sync folder*,
`~/.running_coach_gui.json` key `fit_dir`, or `$RUNNING_COACH_FIT`) and `run_metrics._clipped_plateau` (30 s or more at exactly the run's
maximum is dropped from `series()`; a constant heart rate is not treated as clipped, so synthetic runs are unaffected). The metrics cache key
includes the FIT file's mtime (`CACHE_VERSION` 3). Tests use a stub `fitparse` module: no real FIT file is in the repo.

## Chart view (the second-by-second chart of a run)

`analyze_file` also returns `points` (the per-second dicts: `ts`, `hr`, `speed` (m/s, the `velocity_smooth` column), `cadence`, `alt_raw`
(`altitude`), `alt_fixed` (`fixed_altitude`, intervals.icu's corrected elevation), `lat`/`lon`, `dist_m`). The tab draws them on a `tk.Canvas`
(`RUN_PANELS`: one panel per quantity; the speed panel can be switched to pace, `run_speed`: `pace` = 60 / km/h, None below 3 km/h, axis flipped, range = 1st-99th percentile with the line clamped to it, shared X axis time/distance, a hover readout). Nothing is smoothed or resampled. For a merged outing the
timeline is the concatenated one (pauses removed). Do not publish a screenshot of a real run: the readout shows GPS coordinates; the README
one comes from a synthetic run generated by the screenshot script.

Where intervals.icu's CSV comes from (checked 2026-10-13 on the run of 12/10 against the Health Sync FIT and TCX): it is built from the **FIT**,
not from FIT + TCX: lat/lng/altitude identical per second, heart rate/cadence/speed identical in 90-96% of the seconds (1 s alignment), 1 Hz
resampled, `velocity_smooth` = the FIT `speed`, `distance` = integral of that speed, `fixed_altitude` its own correction (checked 2026-10-13 on Francesco's out-and-back road, ~976 m with a constant ~0.65% slope, 58 m to 52 m: `altitude` is linear in the position along the road, R2 0.98-0.99 per leg, same slope both ways; `fixed_altitude` wiggles by +-2 m, R2 0.07-0.59, so it is NOT used: `extract_points` prefers `altitude`). The TCX has no speed,
so its distance can only come from the GPS (2.6% shorter) and its lap total is unreliable.

## Map view

`draw_map` (interactive: `map_press`/`map_drag` pan, `map_zoom` on the wheel keeps the world pixel under the cursor fixed, `map_hover`, `map_fit`; the view is `map_view` = (zoom, centre x, centre y in world pixels), `None` = fit the track, reset on every new selection) puts the GPS track (the `lat`/`lon` of the points kept for the Run detail tab) over OpenStreetMap tiles on a `tk.Canvas`; no new
dependency: the PNG tiles are loaded with `tk.PhotoImage` (Tk 8.6 reads PNG), the Web-Mercator maths is in the core (`world_px`, `fit_zoom`,
`fetch_tile`, tested without Tk). The first zoom is the highest one at which the bounding box fits the canvas. The track is coloured by `velocity_smooth` in 12 buckets between the 5th and 95th percentile
(`_draw_speed_track`, consecutive segments of one colour are drawn as one canvas line, to keep the redraw small: it runs on every mouse move while dragging). Tiles are fetched one at a time by ONE
persistent worker thread (`_tile_worker`, fed by `tile_req`, answers polled by `_tile_poll` through `tile_queue`; it skips tiles no longer in `map_wanted`, so a fast drag
does not download what was already scrolled away) and cached in `~/.running_coach_tiles` (outside the repos, the data
folder and the Drive mirror). Nothing is downloaded unless the Map view is the one showing. The tile policy requires an identifying `User-Agent`,
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

GUI: select two or more rows in the Runs list (extended selection), right-click, *Merge runs* / *Unmerge runs*
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
`ef_m_beat`, `drift_pct`, `tss`, `temp_c`, `humidity_pct`, `wind_kmh`, `kind` ("intervals" or empty).
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
