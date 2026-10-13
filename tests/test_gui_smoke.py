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


    def test_merge_runs_from_the_history_menu(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026.08.18 16.45", "2026.08.18 17.21", "2026.08.18 17.38"):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=3.0, icu_format=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                kids = app.tree.get_children()
                self.assertEqual(len(kids), 3)
                app.tree.selection_set(kids[0])
                app.update_menu()
                self.assertEqual(str(app.menu.entrycget("Merge runs", "state")), "disabled")   # one run: nothing to merge
                app.tree.selection_set(kids[0], kids[1])
                app.update_menu()
                self.assertEqual(str(app.menu.entrycget("Merge runs", "state")), "normal")
                self.assertEqual(str(app.menu.entrycget("Unmerge runs", "state")), "disabled")
                asked = []
                gui.messagebox.askyesno = lambda title, msg: (asked.append(msg), True)[1]
                app.merge_selected()
                app.update()
                self.assertEqual(len(asked), 1)
                kids = app.tree.get_children()
                self.assertEqual(len(kids), 2)
                self.assertTrue(any("merged" in app.tree.item(i, "tags") for i in kids))
                merged = next(i for i in kids if "merged" in app.tree.item(i, "tags"))
                app.tree.selection_set(merged)
                app.update()
                self.assertIn("Merged from 2 recordings", app.detail.get("1.0", "end"))
                app.update_menu()
                self.assertEqual(str(app.menu.entrycget("Unmerge runs", "state")), "normal")
                self.assertIn("2026.08.18 17.21", open(os.path.join(d, "merged_sessions.json")).read())   # the list is newest first
                app.unmerge_selected()
                app.update()
                self.assertEqual(len(app.tree.get_children()), 3)
                self.assertFalse(os.path.exists(os.path.join(d, "merged_sessions.json")))
            finally:
                app._close()


    def test_run_detail_tab_shows_every_csv_column(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            write_run(os.path.join(d, "2026.05.07 07.51-RUNNING.csv"), km=4.0, icu_format=True, sprint=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                self.assertIsNotNone(app.run)
                kids = app.tree.get_children()
                app.tree.selection_set(kids[0])
                app.tabs.select(app.run_tab)
                app.update()
                self.assertIsNotNone(app.run_geom)
                series = app.run_series()
                self.assertGreater(len(series), 500)
                for key in ("t", "km", "spd", "hr", "cad", "alt_raw", "alt_fixed", "lat", "lon"):
                    self.assertIn(key, series[0])
                self.assertTrue(any(p["spd"] is not None for p in series))            # velocity_smooth, as in the CSV
                self.assertTrue(any(p["alt_fixed"] is not None for p in series))
                c = app.run_canvas
                c.event_generate("<Motion>", x=int(c.winfo_width() * 0.5), y=120)
                app.update()
                text = " ".join(c.itemcget(i, "text") for i in c.find_withtag("hover") if c.type(i) == "text")
                self.assertIn("km/h", text)
                self.assertIn("HR", text)
                self.assertIn("cadence", text)
                self.assertIn("fixed", text)
                app.run_speed.set("min/km")
                app.draw_run()
                texts = [c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"]
                self.assertTrue(texts[0].startswith("Pace"))
                for t in texts[1:5]:                                                  # the four ticks of the first panel: mm:ss
                    self.assertRegex(t, r"^\d+:\d\d$")
                self.assertFalse(any(t.startswith("Speed") for t in texts))
                app.run_speed.set("km/h")
                app.draw_run()
                self.assertTrue(any(c.type(i) == "text" and c.itemcget(i, "text").startswith("Speed") for i in c.find_all()))
                app.run_x.set("Distance")
                app.draw_run()
                self.assertEqual(app.run_geom[8], "km")
                app.tree.selection_remove(*app.tree.selection())
                app.update()
                self.assertIsNone(app.run)                                             # nothing selected, nothing drawn
            finally:
                app._close()


    def test_map_tab_draws_track_over_tiles(self):
        import struct
        import time
        import zlib
        gui = load_gui()

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + b"\xcc\xdd\xcc" * 256 for _ in range(256)))) + chunk(b"IEND", b""))
        real = gui.core.fetch_tile
        asked = []
        with tempfile.TemporaryDirectory() as d:
            write_run(os.path.join(d, "2026.05.07 07.51-RUNNING.csv"), km=4.0, icu_format=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                app.tree.selection_set(app.tree.get_children()[0])
                app.update()
                self.assertEqual(asked, [])                               # not on the Map tab: nothing is downloaded
                gui.core.fetch_tile = lambda z, x, y, cache_dir=None: (asked.append((z, x, y)), png)[1]
                app.tabs.select(app.map_tab)
                for _ in range(60):
                    app.update()
                    if not app.map_pending and not app._tile_job:
                        break
                    time.sleep(0.05)
                c = app.map_canvas
                self.assertGreater(len(asked), 0)
                self.assertGreater(len(c.find_withtag("tile")), 0)
                self.assertGreater(len(c.find_withtag("track")), 2)       # the line (twice) and the two markers
                texts = " ".join(c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text")
                self.assertIn("OpenStreetMap", texts)                     # attribution required by the tile policy
                gui.core.fetch_tile = lambda z, x, y, cache_dir=None: None  # offline: grey background, track still there
                app.map_tiles.clear()
                app.draw_map()
                for _ in range(60):
                    app.update()
                    if not app.map_pending:
                        break
                    time.sleep(0.05)
                self.assertEqual(len(c.find_withtag("tile")), 0)
                self.assertGreater(len(c.find_withtag("track")), 2)
            finally:
                gui.core.fetch_tile = real
                app._close()


    def test_map_pan_zoom_hover_and_speed_colours(self):
        import struct
        import time
        import zlib
        gui = load_gui()

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + b"\xcc\xdd\xcc" * 256 for _ in range(256)))) + chunk(b"IEND", b""))
        real = gui.core.fetch_tile
        gui.core.fetch_tile = lambda z, x, y, cache_dir=None: png
        self.assertEqual(gui.speed_color(0), "#2b5cd6")
        self.assertEqual(gui.speed_color(1), "#d64545")
        with tempfile.TemporaryDirectory() as d:
            write_run(os.path.join(d, "2026.05.07 07.51-RUNNING.csv"), km=4.0, icu_format=True, sprint=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))

            def settle():
                for _ in range(60):
                    app.update()
                    if not app.map_pending and not app._tile_job:
                        break
                    time.sleep(0.05)

            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                app.tree.selection_set(app.tree.get_children()[0])
                app.tabs.select(app.map_tab)
                settle()
                c = app.map_canvas
                z0, cx0, cy0 = app.map_view
                colours = {c.itemcget(i, "fill") for i in c.find_withtag("track") if c.type(i) == "line"}
                self.assertGreaterEqual(len(colours), 3)                    # white casing plus at least two speed colours
                self.assertGreater(len(c.find_withtag("legend")), 10)
                # drag: the view moves by the pointer displacement
                c.event_generate("<ButtonPress-1>", x=300, y=300)
                c.event_generate("<B1-Motion>", x=350, y=330)
                c.event_generate("<ButtonRelease-1>", x=350, y=330)
                settle()
                z1, cx1, cy1 = app.map_view
                self.assertEqual(z1, z0)
                self.assertAlmostEqual(cx1, cx0 - 50)
                self.assertAlmostEqual(cy1, cy0 - 30)
                # wheel: one level in, then one out; the world point under the cursor does not move
                w, h = c.winfo_width(), c.winfo_height()
                before = (cx1 - w / 2 + 400, cy1 - h / 2 + 250)
                c.event_generate("<MouseWheel>", x=400, y=250, delta=120)
                settle()
                z2, cx2, cy2 = app.map_view
                self.assertEqual(z2, z0 + 1)
                self.assertAlmostEqual(cx2 - w / 2 + 400, before[0] * 2, delta=1)
                self.assertAlmostEqual(cy2 - h / 2 + 250, before[1] * 2, delta=1)
                c.event_generate("<MouseWheel>", x=400, y=250, delta=-120)
                settle()
                self.assertEqual(app.map_view[0], z0)
                # fit brings back the whole track; hovering a point of the track shows its values
                app.map_fit()
                settle()
                self.assertEqual(app.map_view, (z0, cx0, cy0))
                xy, data = app.map_geom
                i = len(xy) // 2
                c.event_generate("<Motion>", x=int(xy[i][0]), y=int(xy[i][1]))
                app.update()
                text = " ".join(c.itemcget(k, "text") for k in c.find_withtag("hover") if c.type(k) == "text")
                self.assertIn("km/h", text)
                self.assertIn("HR", text)
                c.event_generate("<Motion>", x=2, y=h - 60)                  # far from the track: nothing
                app.update()
                self.assertEqual(len(c.find_withtag("hover")), 0)
            finally:
                gui.core.fetch_tile = real
                app._close()


if __name__ == "__main__":
    unittest.main()
