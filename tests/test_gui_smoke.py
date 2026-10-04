import json, os, sys, tempfile, unittest
from importlib.machinery import SourceFileLoader
import importlib.util

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
from synth import write_run

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
    def test_import_detail_chart_legs_and_config(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name, km in (("2026.05.05 07.00", 4.0), ("2026.05.09 18.43", 5.0), ("2026.05.15 07.10", 6.0)):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=km, icu_format=True, sprint=km == 5.0)
            cfg = os.path.join(d, "cfg.json")
            app = gui.App(data_dir=d, config_path=cfg)
            try:
                app.fetch.set(False)
                asked = []
                app.ask_legs = lambda text: (asked.append(text), "pesanti")[1]
                app.update()
                app.import_now()
                app.update()

                self.assertEqual(len(asked), 1)
                self.assertEqual(len(app.tree.get_children()), 3)
                self.assertIn("pesanti", open(os.path.join(d, "running_history.csv")).read())

                app.tree.selection_set(app.tree.get_children()[1])
                app.update()
                self.assertGreaterEqual(len(app.splits.get_children()), 4)
                self.assertIn("Sprint", app.detail.get("1.0", "end"))

                app.tabs.select(1)
                app.update()
                for label, *_ in gui.METRICS:
                    app.metric.set(label)
                    app.draw_chart()
                self.assertTrue(app.canvas.find_all())

                app.legs_var.set("dolenti")
                app.save_legs()
                self.assertEqual(app.selected_row()["gambe"], "dolenti")
                self.assertNotEqual(app.reco_vars["dist"].get(), "—")

                app.import_now()
                self.assertIn("0 sessioni importate, 3 già presenti", app.status.get())
            finally:
                app._close()
            self.assertEqual(json.load(open(cfg))["data_dir"], d)

    def test_legs_dialog_returns_choice(self):
        gui = load_gui()
        root = tkinter.Tk()
        try:
            dlg = gui.LegsDialog(root, "?", default="ok")
            dlg.var.set("fresche")
            dlg._ok()
            self.assertEqual(dlg.choice, "fresche")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
