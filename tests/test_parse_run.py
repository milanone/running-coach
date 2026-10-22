import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import parse_run_csv as P
from synth import write_run, write_intervals_run


def load(**kw):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "2026.05.07 07.51-RUNNING.csv")
        write_run(path, **kw)
        pts = P.compute_distances(P.extract_points(P.load_csv(path), P.filename_start(path)))
    return pts, P.km_splits(pts)


class ParseRunTests(unittest.TestCase):
    def test_distance_column_with_gps_lockup(self):
        pts, _ = load(km=6.0)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.05)

    def test_intervals_icu_format_elapsed_seconds_and_fixed_altitude(self):
        pts, splits = load(km=6.0, sprint=True, icu_format=True)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.05)
        self.assertEqual(pts[0]["ts"].strftime("%H:%M"), "07:51")
        self.assertIsNotNone(pts[-1]["alt"])
        self.assertTrue(P.detect_sprint(splits)[0])

    def test_km_splits_report_altitude_as_read(self):
        from datetime import datetime, timedelta
        t0 = datetime(2026, 1, 1)
        pts = [{"ts": t0 + timedelta(seconds=3 * i), "dist_m": i * 10.0, "hr": 140,
                "alt": 58.0 - 0.006 * i * 10} for i in range(201)]            # 2 km, -6 m per km
        sp = P.km_splits(pts)
        self.assertEqual(len(sp), 2)
        self.assertAlmostEqual(sp[0]["alt_start"], 58.0)
        self.assertAlmostEqual(sp[0]["alt_delta"], -6.0)
        self.assertAlmostEqual(sp[1]["alt_delta"], -6.0)
        for p in pts:
            p["alt"] = None
        self.assertIsNone(P.km_splits(pts)[0]["alt_delta"])                    # no altitude column: nothing invented

    def test_distance_column_used_as_is_when_correct(self):
        pts, _ = load(km=6.0, icu_format=True, dist_factor=1)
        self.assertAlmostEqual(pts[-1]["dist_m"], 6000, delta=5)

    def test_gps_fallback_when_no_distance_column(self):
        pts, _ = load(km=6.0, icu_format=True, dist_factor=0)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.1)

    def test_splits(self):
        _, splits = load(km=6.0)
        self.assertGreaterEqual(len(splits), 5)
        self.assertAlmostEqual(splits[0]["pace_s"], 400, delta=3)

    def test_sprint_detected_and_drift_excludes_it(self):
        pts, splits = load(km=6.0, sprint=True)
        is_sprint, idx = P.detect_sprint(splits)
        self.assertTrue(is_sprint)
        self.assertEqual(idx, len(splits) - 1)
        with_sprint = P.cardiac_drift(pts)
        without = P.cardiac_drift(pts, exclude_tail_s=int(splits[idx]["dur_s"]))
        self.assertGreater(with_sprint, without + 2)
        self.assertLess(without, 3)

    def test_drift_ignores_warmup_ramp(self):
        import datetime as dt
        t0 = dt.datetime(2026, 5, 7, 7, 0, 0)
        pts = [{"ts": t0 + dt.timedelta(seconds=i),
                "hr": 110 + 0.05 * i if i < 600 else 140.0} for i in range(2400)]
        self.assertGreater(P.cardiac_drift(pts, warmup_s=0), 5)
        self.assertAlmostEqual(P.cardiac_drift(pts), 0.0, places=1)

    def test_no_sprint_on_steady_run(self):
        _, splits = load(km=6.0)
        self.assertFalse(P.detect_sprint(splits)[0])

    def test_history_upsert_dedups_same_session(self):
        a = {"date": "2026-05-07", "time": "07:00", "dist_km": "4.0"}
        b = {"date": "2026-05-07", "time": "07:00", "dist_km": "4.5"}
        rows = P.upsert(P.upsert([], a), b)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dist_km"], "4.5")

    def test_upsert_keeps_recorded_weather_when_reprocessed(self):
        old = {"date": "2026-05-07", "time": "07:51", "temp_c": "18.0"}
        new = {"date": "2026-05-07", "time": "07:51", "temp_c": ""}
        self.assertEqual(P.upsert([old], new)[0]["temp_c"], "18.0")


    def test_history_mixed_formats_collapse_sort_and_keep_weather(self):
        header = ",".join(P.HISTORY_FIELDS)
        lines = [
            header,
            "01/06/2026,07:05,4.51,00:33:39,07:27,139,0.964,12.99,37,20.8,65,6.2,ok",
            "07/05/2026,07:51,4.80,00:42:00,08:45,135,0.85,5.0,45,18.0,60,5.0,ok",
            "29/04/2026,18:47,4.05,00:33:03,08:09,132,0.923,2.93,33,22.1,37,15.8,",
            "2026-05-07,07:51,4.82,42:11,525.0,135,0.849,1.7,47,,,,fresche",
        ]
        with tempfile.TemporaryDirectory() as d:
            old = P.HISTORY_FILE
            P.HISTORY_FILE = os.path.join(d, "h.csv")
            try:
                with open(P.HISTORY_FILE, "w", newline="") as f:
                    f.write("\n".join(lines))
                rows = P.load_history()
            finally:
                P.HISTORY_FILE = old
        self.assertEqual([r["date"] for r in rows], ["29/04/2026", "07/05/2026", "01/06/2026"])
        may7 = rows[1]
        self.assertEqual(may7["dist_km"], "4.80")
        self.assertEqual(may7["temp_c"], "18.0")
        self.assertEqual(may7["duration_hms"], "00:42:00")
        self.assertEqual(may7["pace_minkm"], "08:45")
        self.assertNotIn("legs", may7)
        self.assertEqual(may7["drift_pct"], "5.0")

    def test_legacy_italian_history_is_read_and_migrated(self):
        # history files written by earlier versions: Italian column names and the dropped `gambe` column
        header = "data,orario,dist_km,durata_hms,passo_minkm,fc_media_bpm,ef_m_batt,drift_pct,tss,temp_c,umidita_pct,vento_kmh,gambe"
        lines = [header,
                 "16/04/2026,19:16,3.02,00:21:12,07:01,151,0.943,-4.92,28,16.6,84,17.1,dolenti",
                 "18/04/2026,17:22,3.68,00:34:29,09:22,129,0.831,1.9,33,20.8,41,20.1,fresche",
                 "21/04/2026,19:08,4.00,00:36:00,09:00,135,0.85,2.0,35,18.0,60,5.0,pesanti"]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.csv")
            with open(path, "w", newline="") as f:
                f.write(chr(10).join(lines))
            rows = P.load_history(path)
            self.assertEqual(rows[0]["humidity_pct"], "84")
            self.assertEqual(rows[0]["avg_hr_bpm"], "151")
            P.save_history(rows, path)                      # saved with the English header
            with open(path) as f:
                self.assertEqual(f.readline().strip(), ",".join(P.HISTORY_FIELDS))
            self.assertEqual([r["humidity_pct"] for r in P.load_history(path)], ["84", "41", "60"])

    def test_pace_and_duration_formats(self):
        self.assertEqual(P.pace_csv(447), "07:27")
        self.assertEqual(P.hms_csv(2019), "00:33:39")
        self.assertEqual(P.parse_pace("07:27"), 447)

    def test_high_drift_adds_rest_day(self):
        hist = [{"date": "2026-05-01", "time": "07:00", "ef_m_beat": "0.90"},
                {"date": "2026-05-03", "time": "07:00", "ef_m_beat": "0.92"}]
        base = {"dist_km": 5.0, "tss": 40, "_pace_s": 450}
        rest_for = lambda drift: P.compute_recommendations(hist, dict(base, drift_pct=drift))["rest"]
        self.assertEqual(rest_for(5.0), 1)
        self.assertEqual(rest_for(6.5), 2)

    def test_import_sessions_session_file_and_recommendations(self):
        with tempfile.TemporaryDirectory() as d:
            write_run(os.path.join(d, "2026.05.07 07.51-RUNNING.csv"), icu_format=True, sprint=True)
            res = P.import_sessions(d, fetch=False)
            self.assertEqual((len(res["sessions"]), res["skipped"], res["errors"]), (1, 0, []))
            history = P.merge_sessions(res["history"], res["sessions"])
            P.save_history(history, P.history_path(d))
            again = P.import_sessions(d, fetch=False)
            self.assertEqual((len(again["sessions"]), again["skipped"]), (0, 1))
            self.assertTrue(P.session_file(d, history[0]).endswith("2026.05.07 07.51-RUNNING.csv"))
            open(os.path.join(d, "2026.05.08 07.00-RUNNING.csv"), "w").write("time\n0\n")
            bad = P.import_sessions(d, fetch=False)
            self.assertEqual(len(bad["errors"]), 1)
        rec = P.compute_recommendations(history, history[0])
        self.assertGreaterEqual(rec["rest"], 1)
        self.assertGreater(rec["dist_km"], 0)

    def test_run_filename_uses_local_start(self):
        self.assertEqual(P.run_filename("2026-10-12T08:28:32"), "2026.10.12 08.28-RUNNING.csv")

    def test_download_new_runs_filters_types_and_skips_existing(self):
        acts = [{"id": "i1", "type": "Run", "start_date_local": "2026-10-07T08:59:57"},
                {"id": "i2", "type": "Walk", "start_date_local": "2026-10-09T12:15:43"},
                {"id": "i3", "type": "Run", "start_date_local": "2026-10-12T08:28:32"},
                {"id": "i4", "type": "Run", "start_date_local": "2026-10-13T08:00:00"}]
        stream = b"time,heartrate\n" + b"0,100\n" * 8
        calls = []

        def fake_get(path, key):
            calls.append(path)
            if "/activities" in path:
                return json.dumps(acts).encode()
            return b"time\n" if "i4" in path else stream

        old = P.intervals_get
        P.intervals_get = fake_get
        try:
            with tempfile.TemporaryDirectory() as d:
                open(os.path.join(d, "2026.10.07 08.59-RUNNING.csv"), "w").close()
                res = P.download_new_runs(d, since="2026-10-01", key="k")
                self.assertEqual(res["downloaded"], ["2026.10.12 08.28-RUNNING.csv"])
                self.assertEqual(res["skipped"], 1)
                self.assertEqual([n for n, _ in res["errors"]], ["2026.10.13 08.00-RUNNING.csv"])
                self.assertFalse(any("i2" in c for c in calls))        # walks are never fetched
                with open(os.path.join(d, res["downloaded"][0]), "rb") as f:
                    self.assertEqual(f.read(), stream)                   # bytes kept as served
        finally:
            P.intervals_get = old

    def test_download_without_key_raises(self):
        old = os.environ.pop("INTERVALS_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                if P.read_api_key(d) is None:                            # no key file next to the program
                    with self.assertRaises(ValueError):
                        P.download_new_runs(d)
        finally:
            if old is not None:
                os.environ["INTERVALS_API_KEY"] = old

    def test_sessions_merged_into_one_outing(self):
        with tempfile.TemporaryDirectory() as d:
            write_run(os.path.join(d, "2026.08.18 16.45-RUNNING.csv"), km=3.0, icu_format=True)
            write_run(os.path.join(d, "2026.08.18 17.21-RUNNING.csv"), km=2.0, icu_format=True)
            solo = P.analyze_file(os.path.join(d, "2026.08.18 16.45-RUNNING.csv"), fetch=False)
            with open(os.path.join(d, P.MERGE_FILE), "w") as f:
                json.dump({"2026.08.18 16.45": ["2026.08.18 17.21"]}, f)
            # a history from before the merge still holds both sessions
            rows = [dict(date="18/08/2026", time="16:45", dist_km="3.0"), dict(date="18/08/2026", time="17:21", dist_km="2.0")]
            P.save_history([P.normalize_row(r) for r in rows], P.history_path(d))
            res = P.import_sessions(d, fetch=False)
            self.assertEqual([s["time"] for s in res["sessions"]], ["16:45"])      # the primary is recomputed
            self.assertEqual([r["time"] for r in res["history"]], ["16:45"])       # the member row is dropped
            s = res["sessions"][0]
            self.assertAlmostEqual(s["dist_km"], 5.0, delta=0.05)
            self.assertAlmostEqual(solo["dur_s"] * 5 / 3, P.safe_float(s["duration_hms"][:2]) * 3600
                                   + P.safe_float(s["duration_hms"][3:5]) * 60 + P.safe_float(s["duration_hms"][6:]), delta=15)
            P.save_history(P.merge_sessions(res["history"], res["sessions"]), P.history_path(d))
            again = P.import_sessions(d, fetch=False)
            self.assertEqual((len(again["sessions"]), len(again["history"])), (0, 1))   # stable on the next import

    def test_merge_runs_on_request_and_unmerge(self):
        with tempfile.TemporaryDirectory() as d:
            for name, km in (("2026.08.18 16.45", 3.0), ("2026.08.18 17.21", 2.0), ("2026.08.18 17.38", 1.0)):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=km, icu_format=True)
            history = P.merge_sessions([], [P.analyze_file(os.path.join(d, f), fetch=False)["session"]
                                            for f in P.find_run_files(d)])
            history[0]["temp_c"] = "28.1"
            self.assertEqual(len(history), 3)
            with self.assertRaises(ValueError):
                P.merge_runs(d, history, history[:1])                      # one run is not a merge
            merged = P.merge_runs(d, history, history[:2])                 # two of the three
            self.assertEqual([r["time"] for r in merged], ["16:45", "17:38"])
            self.assertAlmostEqual(merged[0]["dist_km"], 5.0, delta=0.05)
            self.assertEqual(merged[0]["temp_c"], "28.1")                  # weather already recorded is kept
            self.assertEqual(P.read_merge_groups(d), {"2026.08.18 16.45": ["2026.08.18 17.21"]})
            # merging again a selection that includes the merged run absorbs its members too
            again = P.merge_runs(d, merged, merged)
            self.assertEqual([r["time"] for r in again], ["16:45"])
            self.assertAlmostEqual(again[0]["dist_km"], 6.0, delta=0.08)
            self.assertEqual(P.read_merge_groups(d), {"2026.08.18 16.45": ["2026.08.18 17.21", "2026.08.18 17.38"]})
            # undo: three sessions again, the CSVs were never touched, the file is gone
            undone = P.unmerge_run(d, again, again[0])
            self.assertEqual([r["time"] for r in undone], ["16:45", "17:21", "17:38"])
            self.assertEqual(sorted(P.find_run_files(d)), ["2026.08.18 16.45-RUNNING.csv", "2026.08.18 17.21-RUNNING.csv",
                                                           "2026.08.18 17.38-RUNNING.csv"])
            self.assertFalse(os.path.exists(os.path.join(d, P.MERGE_FILE)))
            self.assertAlmostEqual(float(undone[0]["dist_km"]), 3.0, delta=0.05)
            self.assertEqual(undone[1]["temp_c"], "28.1")
            with self.assertRaises(ValueError):
                P.unmerge_run(d, undone, undone[0])                        # no longer merged

    def test_map_tile_maths_and_cached_download(self):
        import unittest.mock as mock
        self.assertEqual(P.world_px(0, 0, 0), (128.0, 128.0))
        x, y = P.world_px(0, 0, 1)
        self.assertAlmostEqual(x, 256)
        self.assertAlmostEqual(y, 256)
        lats, lons = [45.0, 45.0090], [9.0, 9.0001]                    # a thin 1 km road
        z = P.fit_zoom(lats, lons, 800, 600)
        for zz, fits in ((z, True), (z + 1, False)):
            xa, ya = P.world_px(max(lats), min(lons), zz)
            xb, yb = P.world_px(min(lats), max(lons), zz)
            self.assertEqual(xb - xa <= 720 and yb - ya <= 520, fits)
        self.assertEqual(P.fit_zoom([45.0], [9.0], 800, 600), P.MAX_ZOOM)  # a single point
        with tempfile.TemporaryDirectory() as d:
            class Resp:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b"PNGDATA"
            with mock.patch.object(P.urllib.request, "urlopen", return_value=Resp()) as up:
                self.assertEqual(P.fetch_tile(16, 1, 2, d), b"PNGDATA")
                self.assertEqual(P.fetch_tile(16, 1, 2, d), b"PNGDATA")    # second time from the cache
                self.assertEqual(up.call_count, 1)
                req = up.call_args[0][0]
                self.assertIn("running-coach", req.get_header("User-agent"))
            with mock.patch.object(P.urllib.request, "urlopen", side_effect=OSError("offline")):
                self.assertIsNone(P.fetch_tile(16, 3, 4, d))               # offline: no tile, no exception
                self.assertFalse(os.path.exists(os.path.join(d, "16", "3", "4.png")))

    def test_heart_rate_from_the_fit_file_when_the_csv_is_clipped(self):
        import types, unittest.mock as mock
        from datetime import timedelta

        class Msg:
            def __init__(self, **kw):
                self.kw = kw

            def get_values(self):
                return self.kw

        def fake_fitparse(records):
            mod = types.ModuleType("fitparse")
            mod.FitFile = lambda path: types.SimpleNamespace(get_messages=lambda kind: [Msg(**r) for r in records])
            return mod

        name = "2026.05.07 07.51-RUNNING.csv"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, name)
            write_run(path, icu_format=True)
            pts = P.extract_points(P.load_csv(path), P.filename_start(path))
            cap = max(p["hr"] for p in pts)
            start = P.filename_start(path)
            clipped = lambda k: 300 <= k < 360
            for p in pts:
                if clipped(int(round((p["ts"] - start).total_seconds()))):
                    p["hr"] = cap                                                # the CSV is clipped there; the FIT has the real value
            before = [p["hr"] for p in pts]

            def records(bias=0):
                out = [{"timestamp": start}]                                      # a first record without heart rate, as Health Sync writes
                for k in range(0, len(pts)):
                    for half in (0, 1):                                           # two records per second, as in the real files
                        hr = (cap + 20 if clipped(k) else before[k]) + bias
                        out.append({"timestamp": start + timedelta(seconds=k), "heart_rate": hr + (1 if half else -1)})
                return out

            self.assertFalse(P.apply_fit_hr(pts, path))                           # no FIT_DIR: nothing changes
            fit_dir = os.path.join(d, "fit")
            os.mkdir(fit_dir)
            open(os.path.join(fit_dir, name[:-4] + ".fit"), "wb").close()
            with mock.patch.object(P, "FIT_DIR", fit_dir):
                with mock.patch.dict(sys.modules, {"fitparse": None}):            # fitparse not installed
                    self.assertFalse(P.apply_fit_hr(pts, path))
                with mock.patch.dict(sys.modules, {"fitparse": fake_fitparse(records(bias=15))}):
                    self.assertFalse(P.apply_fit_hr(pts, path))                   # off by 15 bpm everywhere: another run
                self.assertEqual([p["hr"] for p in pts], before)
                with mock.patch.dict(sys.modules, {"fitparse": fake_fitparse(records())}):
                    self.assertTrue(P.apply_fit_hr(pts, path))
            self.assertEqual(pts[330]["hr"], cap + 20)                            # the clipped seconds have the real heart rate
            self.assertEqual(pts[100]["hr"], before[100])                         # the mean of the two records of that second
            self.assertEqual(P.fit_path(path), None)                              # FIT_DIR is unset again outside the patch

    def test_the_fit_file_is_read_only_when_the_csv_looks_clipped(self):
        import types, unittest.mock as mock
        name = "2026.05.07 07.51-RUNNING.csv"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, name)
            write_run(path, icu_format=True)
            fit_dir = os.path.join(d, "fit")
            os.mkdir(fit_dir)
            open(os.path.join(fit_dir, name[:-4] + ".fit"), "wb").close()
            opened = []
            stub = types.ModuleType("fitparse")
            stub.FitFile = lambda p: opened.append(p) or types.SimpleNamespace(get_messages=lambda kind: [])
            pts = P.extract_points(P.load_csv(path), P.filename_start(path))
            for k, p in enumerate(pts):
                p["hr"] = 130 + k % 7                                                       # a heart rate that moves, with a peak of a few seconds
            for p in pts[100:104]:
                p["hr"] = 160
            top = 160
            with mock.patch.object(P, "FIT_DIR", fit_dir), mock.patch.dict(sys.modules, {"fitparse": stub}):
                self.assertFalse(P.apply_fit_hr(pts, path))
                self.assertEqual(opened, [])                                                # a natural peak: the FIT is not even opened
                for p in pts[:P.FIT_MIN_AT_MAX]:
                    p["hr"] = top                                                           # a long stay exactly at the maximum
                P.apply_fit_hr(pts, path)
                self.assertEqual(len(opened), 1)

    def test_interval_workout_is_recognised_and_steady_runs_are_not(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "2026.05.20 07.30-RUNNING.csv")
            write_intervals_run(path)
            res = P.analyze_file(path, fetch=False)
            s = res["session"]
            self.assertEqual(s["kind"], "intervals")
            self.assertEqual(len(res["reps"]), 3)
            self.assertAlmostEqual(res["reps"][0]["dur"], 180, delta=15)
            self.assertAlmostEqual(res["reps"][0]["speed"], 3.3, delta=0.3)
            self.assertEqual((s["drift_pct"], s["ef_m_beat"]), ("", ""))             # not defined without a steady state
            self.assertFalse(res["is_sprint"])
            # the TSS second by second counts the hard seconds more than the average heart rate would
            self.assertGreater(s["tss"], P.compute_tss(res["dur_s"], res["avg_hr"]))
            for kw in ({}, {"sprint": True}, {"km": 3.0, "base_speed": 2.0}):        # steady runs, with or without a final sprint
                steady = os.path.join(d, "2026.05.21 07.30-RUNNING.csv")
                write_run(steady, icu_format=True, **kw)
                r = P.analyze_file(steady, fetch=False)
                self.assertEqual((r["reps"], r["session"]["kind"]), ([], ""))
            self.assertEqual(P.analyze_file(steady, fetch=False)["is_sprint"], False)
            write_run(steady, icu_format=True, sprint=True)
            self.assertTrue(P.analyze_file(steady, fetch=False)["is_sprint"])          # the sprint rule still works on a steady run
            # two repetitions with a recovery that is as fast as they are, or too short: not an interval workout
            write_intervals_run(path, fast=3.3, slow=3.0)
            self.assertEqual(P.analyze_file(path, fetch=False)["reps"], [])
            write_intervals_run(path, rest_s=10)
            self.assertEqual(P.analyze_file(path, fetch=False)["session"]["kind"], "")

    def test_reprocessing_an_interval_workout_does_not_bring_back_drift_and_efficiency(self):
        old = {"date": "20/05/2026", "time": "07:30", "drift_pct": "-20.2", "ef_m_beat": "0.98", "tss": "23", "kind": "", "temp_c": "12"}
        new = {"date": "20/05/2026", "time": "07:30", "drift_pct": "", "ef_m_beat": "", "tss": "24", "kind": "intervals", "temp_c": ""}
        out = P.upsert([old], new)[0]
        self.assertEqual((out["drift_pct"], out["ef_m_beat"], out["kind"]), ("", "", "intervals"))
        self.assertEqual(out["temp_c"], "12")                                               # the weather is still kept
        back = P.upsert([dict(new)], {"date": "20/05/2026", "time": "07:30", "drift_pct": "3", "kind": "", "tss": "30"})[0]
        self.assertEqual(back["kind"], "")                                                  # and a run that is no longer one stays steady

    def test_advice_after_an_interval_workout_starts_from_the_last_steady_run(self):
        steady = [{"date": f"0{d}/05/2026", "time": "07:00", "dist_km": "5.0", "pace_minkm": "7:30", "tss": "40", "drift_pct": "2",
                   "ef_m_beat": "0.90", "kind": ""} for d in (1, 4, 7)]
        hard = {"date": "10/05/2026", "time": "07:00", "dist_km": "2.4", "pace_minkm": "8:10", "tss": "25", "drift_pct": "", "ef_m_beat": "",
                "kind": "intervals"}
        after_steady = P.compute_recommendations(steady, steady[-1])
        after_hard = P.compute_recommendations(steady + [hard], hard)
        self.assertEqual(after_hard["rest"], after_steady["rest"] + 1)
        self.assertEqual(after_hard["dist_km"], 5.0)                                        # not the 2.4 km of the workout
        self.assertEqual(after_hard["base_pace_s"], 450)                                    # 7:30 of the last steady run, not 8:10
        self.assertTrue(any("interval" in text for _, text in after_hard["notes"]))

    def test_ef_and_tss(self):
        self.assertAlmostEqual(P.compute_ef(5.0, 2250, 155), 5000 / (155 * 37.5), places=3)
        self.assertAlmostEqual(P.compute_tss(3600, P.THRESHOLD_HR), 100.0, places=1)


if __name__ == "__main__":
    unittest.main()
