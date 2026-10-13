import json, os, sys, tempfile, time, unittest
from importlib.machinery import SourceFileLoader
import importlib.util

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
from synth import write_run


def write_run_bytes(**kw):
    """Bytes of a synthetic run, as the API would serve them."""
    with tempfile.TemporaryDirectory() as t:
        path = os.path.join(t, "2026.05.20 07.30-RUNNING.csv")
        write_run(path, icu_format=True, **kw)
        with open(path, "rb") as f:
            return f.read()

try:
    import tkinter
    tkinter.Tk().destroy()
    GUI_OK = True
except Exception:
    GUI_OK = False


def load_gui():
    path = os.path.join(HERE, "..", "running-coach-gui.pyw")
    spec = importlib.util.spec_from_loader("rc_gui", SourceFileLoader("rc_gui", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(GUI_OK, "tkinter or a display is not available")
class GuiSmokeTests(unittest.TestCase):
    def test_import_detail_chart_and_config(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name, km in (("2026.05.05 07.00", 4.0), ("2026.05.09 18.43", 5.0), ("2026.05.15 07.10", 6.0)):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=km, icu_format=True, sprint=km == 5.0)
            cfg = os.path.join(d, "cfg.json")
            app = gui.App(data_dir=d, config_path=cfg)
            self.assertEqual(json.load(open(cfg))["data_dir"], d)
            try:
                app.fetch.set(False)
                app.update()
                app.import_now()
                app.update()

                self.assertEqual(len(app.tree.get_children()), 3)
                self.assertNotIn("legs", open(os.path.join(d, "running_history.csv")).read())

                app.tree.selection_set(app.tree.get_children()[1])
                app.update()
                self.assertGreaterEqual(len(app.splits.get_children()), 4)
                self.assertIn("Final sprint", app.detail.get("1.0", "end"))

                app.tabs.select(1)
                app.update()
                km = lambda: [app.tree.set(i, "dist_km") for i in app.tree.get_children()]
                app.sort_by("dist_km")
                self.assertEqual(km(), ["4.0", "5.0", "6.0"])
                self.assertIn("▲", app.tree.heading("dist_km")["text"])
                app.sort_by("dist_km")
                self.assertEqual(km(), ["6.0", "5.0", "4.0"])
                self.assertIn("▼", app.tree.heading("dist_km")["text"])
                app.sort_by("date")
                self.assertEqual(app.tree.set(app.tree.get_children()[0], "date"), "15/05/2026")
                app.sort_by("date")
                app.sort_by("date")
                for label, *_ in gui.METRICS:
                    app.metric.set(label)
                    app.draw_chart()
                self.assertTrue(app.canvas.find_all())

                self.assertNotEqual(app.reco_vars["dist"].get(), "—")

                app.import_now()
                self.assertIn("0 sessions imported, 3 already present", app.status.get())

                # "Download new sessions from intervals.icu": fake API, the new run lands in the data folder
                stream = write_run_bytes(km=4.5)
                core = gui.core
                core.intervals_get = lambda path, key: (
                    json.dumps([{"id": "i9", "type": "Run", "start_date_local": "2026-05-20T07:30:00"}]).encode()
                    if "/activities" in path else stream)
                core.read_api_key = lambda data_dir=None: "k"
                app.start_import(download=True)
                for _ in range(100):
                    app.update()
                    if not app.busy:
                        break
                    time.sleep(0.1)
                self.assertTrue(os.path.exists(os.path.join(d, "2026.05.20 07.30-RUNNING.csv")))
                self.assertIn("1 runs downloaded", app.status.get())
                self.assertEqual(len(app.tree.get_children()), 4)
            finally:
                app._close()
            self.assertEqual(json.load(open(cfg))["data_dir"], d)


if __name__ == "__main__":
    unittest.main()
