import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import parse_run_csv as P
from synth import write_run


def load(**kw):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "2026.05.07 07.51-RUNNING.csv")
        write_run(path, **kw)
        pts = P.compute_distances(P.extract_points(P.load_csv(path), P.filename_start(path)))
    return pts, P.km_splits(pts)


class ParseRunTests(unittest.TestCase):
    def test_distance_ignores_doubled_samsung_field_and_gps_lockup(self):
        pts, _ = load(km=6.0)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.05)

    def test_intervals_icu_format_elapsed_seconds_and_fixed_altitude(self):
        pts, splits = load(km=6.0, sprint=True, icu_format=True)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.05)
        self.assertEqual(pts[0]["ts"].strftime("%H:%M"), "07:51")
        self.assertIsNotNone(pts[-1]["alt"])
        self.assertTrue(P.detect_sprint(splits)[0])

    def test_elevation_gain_counts_slow_climb_below_per_sample_threshold(self):
        pts = [{"alt": 100 + 0.1 * i} for i in range(101)]
        self.assertAlmostEqual(P.elevation_gain(pts), 10.0, delta=0.6)
        flat_noise = [{"alt": 100 + (0.2 if i % 2 else 0)} for i in range(100)]
        self.assertEqual(P.elevation_gain(flat_noise), 0.0)

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

    def test_no_sprint_on_steady_run(self):
        _, splits = load(km=6.0)
        self.assertFalse(P.detect_sprint(splits)[0])

    def test_history_upsert_dedups_same_session(self):
        a = {"data": "2026-05-07", "orario": "07:00", "gambe": "ok"}
        b = {"data": "2026-05-07", "orario": "07:00", "gambe": "dolenti"}
        rows = P.upsert(P.upsert([], a), b)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gambe"], "dolenti")

    def test_upsert_keeps_recorded_legs_when_reprocessed(self):
        old = {"data": "2026-05-07", "orario": "07:51", "gambe": "dolenti"}
        new = {"data": "2026-05-07", "orario": "07:51", "gambe": ""}
        self.assertEqual(P.upsert([old], new)[0]["gambe"], "dolenti")

    def test_is_latest_only_for_newest_session(self):
        hist = [{"data": "2026-05-07", "orario": "07:51"}, {"data": "2026-05-09", "orario": "08:00"}]
        self.assertFalse(P.is_latest(hist, hist[0]))
        self.assertTrue(P.is_latest(hist, hist[1]))

    def test_ef_and_tss(self):
        self.assertAlmostEqual(P.compute_ef(5.0, 2250, 155), 5000 / (155 * 37.5), places=3)
        self.assertAlmostEqual(P.compute_tss(3600, P.THRESHOLD_HR), 100.0, places=1)


if __name__ == "__main__":
    unittest.main()
