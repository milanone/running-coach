import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import parse_run_csv as P
from synth import write_run


def load(**kw):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "run.csv")
        write_run(path, **kw)
        pts = P.compute_distances(P.extract_points(P.load_csv(path)))
    return pts, P.km_splits(pts)


class ParseRunTests(unittest.TestCase):
    def test_distance_ignores_doubled_samsung_field_and_gps_lockup(self):
        pts, _ = load(km=6.0)
        self.assertAlmostEqual(pts[-1]["dist_m"] / 1000, 6.0, delta=0.05)

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

    def test_ef_and_tss(self):
        self.assertAlmostEqual(P.compute_ef(5.0, 2250, 155), 5000 / (155 * 37.5), places=3)
        self.assertAlmostEqual(P.compute_tss(3600, P.THRESHOLD_HR), 100.0, places=1)


if __name__ == "__main__":
    unittest.main()
