import json, math, os, sys, tempfile, unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import parse_run_csv as P
import run_metrics as R
from synth import write_run

ATHLETE = dict(R.DEFAULT_ATHLETE, hr_max=190, hr_rest=60, mass_kg=70)


def make_pts(segments, cadence=88):
    """Points at 1 Hz from [(seconds, speed m/s, hr or callable(t_in_segment))]; flat altitude."""
    t0, d, out, t = datetime(2026, 5, 7, 7, 0, 0), 0.0, [], 0
    for secs, v, hr in segments:
        for k in range(secs):
            d += v
            out.append({"ts": t0 + timedelta(seconds=t), "hr": hr(k) if callable(hr) else hr, "speed": v, "cadence": cadence if v > 0 else 0,
                        "alt": 50.0, "alt_raw": 50.0, "lat": None, "lon": None, "dist_m": d})
            t += 1
    return out


class RunMetricsTests(unittest.TestCase):
    def test_zones_trimp_and_training_effect(self):
        S = R.series(make_pts([(1800, 3.0, 140)]))                       # 140 / 190 = 74%: zone 3
        zones = R.zone_seconds(S, ATHLETE)
        self.assertAlmostEqual(zones[3], 1799, delta=1)                  # 1800 samples, 1799 intervals
        trimp = R.edwards_trimp(zones)
        self.assertAlmostEqual(trimp, 90, delta=0.1)
        self.assertAlmostEqual(R.aerobic_te(trimp), 5 * (1 - math.exp(-trimp / 110)), places=6)
        self.assertEqual(R.aerobic_te(0), 0)
        self.assertLess(R.aerobic_te(10000), 5.0001)
        self.assertAlmostEqual(R.recovery_hours(3.0), 36.0)

    def test_clipped_heart_rate_plateau_is_left_out(self):
        jitter = lambda k: 150 + (k % 3)
        pts = make_pts([(600, 3.0, jitter), (120, 3.4, 167), (300, 3.0, jitter)])
        S = R.series(pts)
        self.assertEqual(S["clipped_s"], 120)                                  # 120 s at exactly the run's maximum
        self.assertEqual(len([h for h in S["hr"] if h is None]), 120)
        m = R.run_metrics(pts, ATHLETE)
        self.assertEqual(m["hr_clipped_s"], 120)
        self.assertTrue(any("clipped" in line for line in R.run_lines(dict(m, perf_pct=None))))
        # a peak that moves is a real heart rate: nothing is dropped
        moving = make_pts([(600, 3.0, jitter), (120, 3.4, lambda k: 160 + k % 4), (300, 3.0, jitter)])
        self.assertEqual(R.series(moving)["clipped_s"], 0)
        # a short stay at the maximum (under 30 s) is kept too
        self.assertEqual(R.series(make_pts([(600, 3.0, jitter), (20, 3.4, 167), (300, 3.0, jitter)]))["clipped_s"], 0)

    def test_best_efforts_find_the_fast_stretch(self):
        S = R.series(make_pts([(600, 2.0, 130), (600, 4.0, 150), (600, 2.0, 130)]))
        best = R.best_efforts(S)
        self.assertAlmostEqual(best["1 km"][0], 250.0, delta=1)         # inside the 4 m/s part
        self.assertAlmostEqual(best["1 km"][1], 600 + 0, delta=130)
        self.assertNotIn("5 km", best)                                    # the run is 4.8 km long
        self.assertAlmostEqual(best["3 km"][0], 900, delta=1)             # 600 m at 2 m/s + 2400 m at 4 m/s, wherever the stretch starts
        S2 = R.series(make_pts([(2000, 3.0, 130)]))
        self.assertAlmostEqual(R.best_efforts(S2)["5 km"][0], 5000 / 3.0, delta=1)

    def test_vdot_matches_the_daniels_table_and_riegel(self):
        # Daniels' tables as I remember them: VDOT 50 -> 5 km 19:57, 10 km 41:21
        self.assertAlmostEqual(R.race_time_of_vdot(50, 5000), 19 * 60 + 57, delta=3)
        self.assertAlmostEqual(R.race_time_of_vdot(50, 10000), 41 * 60 + 21, delta=5)
        self.assertAlmostEqual(R.vdot_of_race(5000, 19 * 60 + 57), 50, delta=0.1)
        self.assertGreater(R.race_time_of_vdot(55, 5000), 0)
        self.assertLess(R.race_time_of_vdot(55, 5000), R.race_time_of_vdot(50, 5000))   # fitter = faster
        self.assertAlmostEqual(R.riegel(1200, 5000, 10000), 1200 * 2 ** 1.06)

    def test_vo2max_from_a_steady_stretch(self):
        # 3 m/s = 180 m/min -> 0.2 * 180 + 3.5 = 39.5; HR 138 = 60% of the reserve -> (39.5 - 3.5) / 0.6 + 3.5 = 63.5
        S = R.series(make_pts([(1200, 3.0, 138)]))
        self.assertAlmostEqual(R.vo2max_estimate(S, ATHLETE), 63.5, delta=0.05)
        self.assertIsNone(R.vo2max_estimate(R.series(make_pts([(200, 3.0, 138)])), ATHLETE))      # too short
        noisy = R.series(make_pts([(1200, 3.0, lambda k: 120 + (k % 2) * 30)]))
        self.assertIsNone(R.vo2max_estimate(noisy, ATHLETE))                                      # no steady heart rate

    def test_estimated_max_heart_rate_gives_a_vo2max_range(self):
        S = make_pts([(1200, 3.0, 138)])
        base = R.run_metrics(S, ATHLETE)
        self.assertIsNone(base["vo2max_if_hrmax_low"])                       # measured maximum: no range
        guess = R.run_metrics(S, dict(ATHLETE, hr_max_estimated=True))
        # hr_max 175: reserve 115, fraction 78 / 115 -> (39.5 - 3.5) / 0.678 + 3.5 = 56.6; hr_max 205: reserve 145 -> 70.4
        self.assertAlmostEqual(guess["vo2max_if_hrmax_low"], 56.6, delta=0.1)
        self.assertAlmostEqual(guess["vo2max_if_hrmax_high"], 70.4, delta=0.1)
        self.assertLess(guess["vo2max_if_hrmax_low"], guess["vo2max"])
        self.assertLess(guess["vo2max"], guess["vo2max_if_hrmax_high"])

    def test_pace_at_heart_rate_and_heart_rate_at_speed(self):
        S = R.series(make_pts([(1800, 3.0, 138)]))
        self.assertAlmostEqual(R.pace_at_hr(S, 138), 1000 / 3.0, delta=0.5)
        self.assertIsNone(R.pace_at_hr(S, 160))                                                   # never at that heart rate
        self.assertAlmostEqual(R.hr_at_speed(S, 3.0), 138, delta=0.01)
        self.assertIsNone(R.hr_at_speed(S, 4.0))

    def test_cadence_step_length_and_power(self):
        S = R.series(make_pts([(600, 3.0, 138)], cadence=88))            # 88 per foot -> 176 steps/min
        spm, step, factor = R.cadence_stats(S, ATHLETE)
        self.assertEqual((round(spm), factor), (176, 2))
        self.assertAlmostEqual(step, 3.0 * 60 / 176, places=3)
        spm2, _, factor2 = R.cadence_stats(R.series(make_pts([(600, 3.0, 138)], cadence=176)), ATHLETE)
        self.assertEqual((round(spm2), factor2), (176, 1))               # already steps per minute
        w, wkg = R.power_estimate(S, ATHLETE)
        self.assertAlmostEqual(w, 70 * 3.0 * 1.04, delta=0.5)
        self.assertAlmostEqual(wkg, 3.0 * 1.04, delta=0.01)

    def test_heart_rate_recovery_and_rise_time(self):
        run = [(600, 3.0, lambda k: 150 - k % 2), (120, 0.0, lambda k: 150 - 0.4 * k)]      # a heart rate that moves: not a clipped plateau
        self.assertAlmostEqual(R.hr_recovery(R.series(make_pts(run))), 24.0, delta=1.0)   # 60 s later: 150 - 0.4 * 60
        self.assertIsNone(R.hr_recovery(R.series(make_pts([(600, 3.0, 150)]))))           # no data after the stop
        rise = [(900, 3.0, lambda k: 140 - 50 * math.exp(-k / 60.0))]
        self.assertAlmostEqual(R.hr_rise_time(R.series(make_pts(rise))), 60, delta=8)
        self.assertIsNone(R.hr_rise_time(R.series(make_pts([(300, 3.0, 140)]))))

    def test_split_balance_variability_and_efforts(self):
        S = R.series(make_pts([(600, 4.0, 150), (600, 3.0, 150)]))       # 2400 m fast, then 1800 m slower
        self.assertAlmostEqual(R.split_balance(S), (675 - 525) / 525 * 100, delta=0.5)
        self.assertGreater(R.pace_variability(S), 5)
        self.assertAlmostEqual(R.pace_variability(R.series(make_pts([(1500, 3.0, 140)]))), 0, delta=0.5)
        burst = R.find_efforts(R.series(make_pts([(600, 3.0, 140), (120, 4.5, 165), (600, 3.0, 140)])))
        self.assertEqual(len(burst), 1)
        self.assertAlmostEqual(burst[0]["dur"], 120, delta=35)
        self.assertEqual(R.find_efforts(R.series(make_pts([(1200, 3.0, 140)]))), [])

    def test_training_load_and_status(self):
        history = [{"date": "01/05/2026", "time": "07:00", "tss": "100"}]
        load = R.training_load(history, today=date(2026, 5, 3))
        self.assertEqual([e["date"] for e in load], ["2026-05-01", "2026-05-02", "2026-05-03"])
        ctl, atl = 100 / 42.0, 100 / 7.0
        self.assertAlmostEqual(load[0]["ctl"], ctl)
        self.assertAlmostEqual(load[0]["atl"], atl)
        self.assertAlmostEqual(load[1]["ctl"], ctl - ctl / 42.0)
        self.assertAlmostEqual(load[2]["atl"], (atl - atl / 7.0) - (atl - atl / 7.0) / 7.0)
        self.assertAlmostEqual(load[2]["acute"], 100)
        self.assertAlmostEqual(load[2]["chronic"], 25)                    # 100 over four weeks
        self.assertEqual(R.training_status(load)[0], "Not enough history")
        # a steady 35 TSS every Monday for ten weeks: productive early, then maintaining, never strained right after a run
        steady = []
        d0 = date(2026, 5, 4)
        for k in range(10):
            steady.append({"date": (d0 + timedelta(weeks=k)).strftime("%d/%m/%Y"), "time": "07:00", "tss": "35"})
        load = R.training_load(steady, today=d0 + timedelta(weeks=9))
        label, why = R.training_status(load)
        self.assertIn(label, ("Maintaining", "Productive"), why)
        # 20 days without running: detraining
        label, _ = R.training_status(R.training_load(steady, today=d0 + timedelta(weeks=9, days=20)))
        self.assertEqual(label, "Detraining")
        # a second run within the week of a once-a-week runner doubles the ratio, but the load is small: ramping up, not strain
        extra = steady + [{"date": (d0 + timedelta(weeks=9, days=5)).strftime("%d/%m/%Y"), "time": "07:00", "tss": "35"}]
        label, why = R.training_status(R.training_load(extra, today=d0 + timedelta(weeks=9, days=5)))
        self.assertEqual(label, "Ramping up", why)
        # a sudden load spike
        spike = steady[:6] + [{"date": (d0 + timedelta(weeks=5, days=d)).strftime("%d/%m/%Y"), "time": "08:%02d" % d, "tss": "90"}
                              for d in (1, 2, 3, 4, 5)]
        label, _ = R.training_status(R.training_load(spike, today=d0 + timedelta(weeks=5, days=5)))
        self.assertEqual(label, "Overreaching")                            # ratio ~3.3, past the 2.0 of Garmin's "very high"

    def test_gauges_pick_the_band_of_each_value(self):
        load = {"acute": 150.0, "chronic": 100.0, "tsb": -12.0}
        summ = {"load": load, "status": ("Productive", ""), "athlete": ATHLETE}
        m = {"te": 3.4, "perf_pct": -7.0, "hrr60": 9.0}
        session = {"drift_pct": "2.5", "avg_hr_bpm": "133"}                        # 133 / 190 = 70%: the edge of zone 3
        g = {x["key"]: x for x in R.gauges(m, session, summ)}
        self.assertEqual(len(g), 8)
        self.assertEqual((g["load_ratio"]["label"], g["load_ratio"]["tone"]), ("High", "warn"))      # 1.5: in Garmin's 1.4-2.0
        self.assertNotIn("small", g["load_ratio"]["advice"])                                 # a heavy base: no low-base remark
        self.assertEqual(g["form"]["label"], "Building")
        self.assertEqual(g["drift"]["label"], "Steady")
        self.assertEqual(g["intensity"]["label"], "Zone 3")
        self.assertEqual(g["te"]["label"], "Improving")
        self.assertEqual(g["condition"]["label"], "Below usual")
        self.assertEqual((g["hrr"]["label"], g["hrr"]["tone"]), ("Slow", "bad"))
        for x in g.values():                                                             # every gauge says where its range comes from
            self.assertIn(x["level"], ("sourced", "secondary", "mine"))
            self.assertTrue(x["why"] and x["source"])
            self.assertLessEqual(x["lo"], x["bands"][0][0])
            self.assertGreaterEqual(x["hi"], x["bands"][-1][1])
        # a small weekly base: a high ratio carries the remark
        small = dict(summ, load={"acute": 60.0, "chronic": 30.0, "tsb": -3.0})
        self.assertIn("small", {x["key"]: x for x in R.gauges(m, session, small)}["load_ratio"]["advice"])
        # no data at all: every gauge says so instead of failing
        empty = R.gauges(None, None, None)
        self.assertEqual({x["label"] for x in empty}, {"No data"})
        self.assertTrue(all(x["value"] is None for x in empty))
        # a value past the end of the scale falls in the last band
        far = R.gauges({"te": 5.0}, {"drift_pct": "40"}, summ)
        self.assertEqual({x["key"]: x["label"] for x in far}["drift"], "High")
        self.assertEqual({x["key"]: x["label"] for x in far}["te"], "Highly improving")

    def test_vo2max_gauge_uses_the_norms_of_age_and_sex(self):
        self.assertEqual(R.vo2max_norm(55, "M"), (35.6, 39.2, 43.4, 48.9))                 # the Cooper Institute table, men 50-59
        self.assertEqual(R.vo2max_norm(55, "F"), (30.1, 33.0, 36.7, 41.1))
        self.assertEqual(R.vo2max_norm(20, "M")[0], 41.7)
        self.assertEqual(R.vo2max_norm(79, "F")[3], 36.7)
        self.assertIsNone(R.vo2max_norm(19, "M"))                                          # outside the table
        self.assertIsNone(R.vo2max_norm(80, "M"))
        self.assertIsNone(R.vo2max_norm(55, None))
        self.assertIsNone(R.vo2max_norm(None, "M"))
        summ = {"load": None, "vo2max": 41.0, "vo2max_range": (38.0, 44.0), "status": ("", ""), "athlete": dict(ATHLETE, age=55, sex="M")}
        g = {x["key"]: x for x in R.gauges({}, {}, summ)}["vo2max"]
        self.assertEqual((g["label"], g["tone"]), ("Good", "good"))                          # 39.2 <= 41 < 43.4
        self.assertIn("38-44", g["advice"])                                                 # the spread of the last runs is said
        self.assertEqual((g["lo"], g["hi"]), (20, 57))                                      # 48.9 + 8
        self.assertEqual([b[3] for b in g["bands"]], ["Poor", "Fair", "Good", "Excellent", "Superior"])
        female = dict(summ, athlete=dict(ATHLETE, age=55, sex="F"))
        self.assertEqual({x["key"]: x for x in R.gauges({}, {}, female)}["vo2max"]["label"], "Excellent")   # 41.0 is just under the 41.1 of Superior for women 50-59
        # without age or sex the gauge asks for them and still shows the value
        bare = {x["key"]: x for x in R.gauges({}, {}, dict(summ, athlete=ATHLETE))}["vo2max"]
        self.assertIsNone(bare["value"])
        self.assertIn("age", bare["advice"])
        self.assertEqual(bare["text"], "41.0")

    def test_next_session_advice(self):
        def run(day, tss, pace="6:30"):
            return {"date": day, "time": "07:00", "dist_km": "5.0", "pace_minkm": pace, "tss": str(tss), "drift_pct": "1.0", "ef_m_beat": "1.0"}
        d0 = date(2026, 5, 4)
        steady = [run((d0 + timedelta(weeks=k)).strftime("%d/%m/%Y"), 35) for k in range(10)]
        today = d0 + timedelta(weeks=9, days=2)
        load = R.training_load(steady, today=today)
        summ = {"load": load[-1], "status": R.training_status(load)}
        rows = [{"pace_at_hr": p, "hr_ref": 138.0} for p in (480, 480, 480, 470, 470, 470)]
        adv = R.next_session(steady, rows, summ, today=today)
        self.assertEqual(adv["since"], 2)
        self.assertAlmostEqual(adv["pace_s"], 470 + (adv["pace_s"] - 470), places=3)
        self.assertTrue(any("Easy pace 7:50" in t for _, t in adv["notes"]))          # median of the last three pace-at-HR values
        self.assertTrue(any("10 s/km faster" in t for _, t in adv["notes"]))
        lines = R.next_session_lines(adv)
        self.assertIn("NEXT SESSION", lines[0])
        self.assertIn("Distance:", lines[1])
        # a heavy week on top of a light month: at least two rest days and a warning
        spike = steady[:6] + [run((d0 + timedelta(weeks=5, days=k)).strftime("%d/%m/%Y"), 90) for k in (1, 2, 3, 4, 5)]
        today = d0 + timedelta(weeks=5, days=5)
        load = R.training_load(spike, today=today)
        adv = R.next_session(spike, [], {"load": load[-1], "status": R.training_status(load)}, today=today)
        self.assertGreaterEqual(adv["rest"], 2)
        self.assertTrue(any("Load ratio" in t for _, t in adv["notes"]))
        self.assertFalse(adv["easy_pace"])                                             # no pace-at-HR data: the core's pace
        self.assertIsNone(R.next_session([], [], None))
        self.assertIn("no session", R.next_session_lines(None)[0])

    def test_athlete_age_sex_and_writing(self):
        with tempfile.TemporaryDirectory() as d:
            R.write_athlete(d, {"hr_max": 185, "age": 40, "sex": "F", "hr_max_estimated": True})
            a = R.read_athlete(d)
            self.assertEqual((a["hr_max"], a["age"], a["sex"], a["hr_max_estimated"]), (185, 40, "F", True))
            R.write_athlete(d, {"age": 41, "hr_max_estimated": None})                      # None removes a key; the others are kept
            a = R.read_athlete(d)
            self.assertEqual((a["hr_max"], a["age"], a["sex"], a["hr_max_estimated"]), (185, 41, "F", None))
            with open(os.path.join(d, R.ATHLETE_FILE), "w") as f:
                json.dump({"age": "old", "sex": "x", "hr_max": 180, "colour": "red"}, f)
            a = R.read_athlete(d)
            self.assertEqual((a["age"], a["sex"], a["hr_max"]), (None, None, 180))         # a bad age or sex is ignored, not fatal
            R.write_athlete(d, {"sex": "m"})
            self.assertEqual(R.read_athlete(d)["sex"], "M")
            self.assertEqual(json.load(open(os.path.join(d, R.ATHLETE_FILE)))["colour"], "red")   # an unknown key survives a rewrite

    def test_athlete_file_and_formatting(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(R.read_athlete(d), R.DEFAULT_ATHLETE)           # no file: the defaults
            with open(os.path.join(d, R.ATHLETE_FILE), "w") as f:
                json.dump({"hr_max": 185, "mass_kg": 68, "unknown": 1}, f)
            a = R.read_athlete(d)
            self.assertEqual((a["hr_max"], a["hr_rest"], a["mass_kg"]), (185, 60, 68))
            self.assertNotIn("unknown", a)
            with open(os.path.join(d, R.ATHLETE_FILE), "w") as f:
                f.write("{not json")
            self.assertEqual(R.read_athlete(d), R.DEFAULT_ATHLETE)           # a broken file does not stop the program
        self.assertEqual(R.fmt_time(75), "1:15")
        self.assertEqual(R.fmt_time(3725), "1:02:05")

    def test_compute_all_summary_and_cache(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as d:
            for name, km in (("2026.04.14 07.00", 4.0), ("2026.04.21 07.00", 5.0), ("2026.04.28 07.00", 6.0), ("2026.05.05 07.00", 7.0)):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=km, base_speed=2.6, icu_format=True, hr_start=138)
            history = P.merge_sessions([], [P.analyze_file(os.path.join(d, f), fetch=False)["session"] for f in P.find_run_files(d)])
            rows = R.compute_all(d, history, ATHLETE)
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(r["trimp"] > 0 and r["best"] for r in rows))
            self.assertIn("5 km", rows[-1]["best"])
            self.assertNotIn("5 km", rows[0]["best"])                        # 4 km run
            self.assertIsNotNone(rows[-1]["ctl"])
            s = R.summary(rows, history, ATHLETE, today=date(2026, 5, 6))
            text = "\n".join(R.summary_lines(s))
            self.assertIn("Training status", text)
            self.assertIn("5 km", text)
            self.assertTrue(os.path.exists(os.path.join(d, R.CACHE_FILE)))
            with mock.patch.object(P, "analyze_file", side_effect=AssertionError("the cache was not used")):
                again = R.compute_all(d, history, ATHLETE)                   # second time: straight from the cache
            self.assertEqual([r["trimp"] for r in again], [r["trimp"] for r in rows])
            with mock.patch.object(P, "analyze_file", side_effect=ValueError("recomputed")):
                other = R.compute_all(d, history, dict(ATHLETE, hr_max=180))  # other parameters: the cache is not valid
            self.assertEqual(other, [])                                       # every file was tried again (and failed here)
            self.assertGreater(len(R.run_lines(rows[-1])), 5)
            self.assertNotIn("bpm off", text)                          # a measured maximum heart rate: no range line
            guess = dict(ATHLETE, hr_max_estimated=True)
            text2 = "\n".join(R.summary_lines(R.summary(R.compute_all(d, history, guess), history, guess, today=date(2026, 5, 6))))
            self.assertIn("bpm off", text2)
            self.assertIn("(estimated)", text2)


if __name__ == "__main__":
    unittest.main()
