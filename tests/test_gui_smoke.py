import json, os, sys, tempfile, time, unittest
from importlib.machinery import SourceFileLoader
import importlib.util

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
from synth import write_run, write_intervals_run


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

                app.fitness_now()                                              # the advice lives at the top of the Fitness tab
                self.assertIsNotNone(app.fit_view["next"])
                self.assertEqual(app.fit_view["next"]["rows"][0][0], "Distance")
                self.assertNotIn("Next session", [app.tabs.tab(t, "text") for t in app.tabs.tabs()])

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


    def test_fitness_tab_table_summary_and_charts(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name, km in (("2026.04.14 07.00", 4.0), ("2026.04.21 07.00", 5.0), ("2026.04.28 07.00", 6.0), ("2026.05.05 07.00", 7.0)):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=km, base_speed=2.6, icu_format=True)
            with open(os.path.join(d, "athlete.json"), "w") as f:
                json.dump({"hr_max": 185, "hr_rest": 55, "mass_kg": 68}, f)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                app.fitness_now()
                self.assertEqual(len(app.fit_rows), 4)
                self.assertEqual(len(app.adv), 4)
                kids = app.fit_tree.get_children()
                self.assertEqual(len(kids), 4)
                self.assertEqual(app.fit_tree.set(kids[0], "date"), "05/05/2026")           # newest first
                self.assertTrue(app.fit_view["load"]["headline"])
                self.assertIn("mass 68 kg", app.fit_view["footer"])
                self.assertNotIn("No athlete.json", app.fit_message.get())
                app.fit_tree.selection_set(kids[0])
                app.update()
                self.assertEqual(len(app.fit_panel["zones"]), 5)                          # the panel of the selected run
                self.assertEqual(app.fit_panel["title"], "05/05/2026  07:00" if False else app.fit_panel["title"])
                self.assertEqual(len(app.fit_strip.winfo_children()), 4)                   # the four cards
                app.tabs.select(1)                                                          # Charts: the canvas must be visible to be drawn
                app.update()
                for label in ("Fitness, CTL", "VO2max estimate (ml/kg/min)", "Pace at a fixed HR (min/km)", "Training effect (indicative)"):
                    app.metric.set(label)
                    app.draw_chart()
                app.metric.set("Training effect (indicative)")
                app.draw_chart()
                self.assertTrue(any(app.canvas.type(i) == "oval" for i in app.canvas.find_all()))   # four points plotted
                os.remove(os.path.join(d, "athlete.json"))
                app.fitness_now()
                self.assertIn("No athlete.json", app.fit_message.get())
            finally:
                app._close()


    def test_gauges_tab_draws_a_card_per_value_and_follows_the_run(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026.04.14 07.00", "2026.04.21 07.00", "2026.04.28 07.00"):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=4.0, base_speed=2.6, icu_format=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.fitness_now()
                app.update()
                self.assertEqual(len(app.gauge_cards), 8)
                card, g = app.gauge_cards[0]
                boxes = [w for w in card.winfo_children() if w.winfo_class() == "Text"]
                self.assertEqual(len(boxes), 3)                                        # the verdict, the explanation and the reference
                self.assertIn(g["label"], boxes[0].get("1.0", "end"))
                self.assertIn(g["why"], boxes[1].get("1.0", "end"))
                boxes[1].tag_add("sel", "1.0", "end-1c")
                self.assertEqual(boxes[1].selection_get(), g["why"])                   # it can be selected (and so copied)
                before = boxes[1].get("1.0", "end")
                boxes[1].focus_force()
                boxes[1].event_generate("<Key>", keysym="x")                           # but not edited
                app.update()
                self.assertEqual(boxes[1].get("1.0", "end"), before)
                self.assertEqual(app.gauge_run.get().split()[0], "2026-04-28")          # the newest run first
                self.assertTrue(all(g["title"] for _, g in app.gauge_cards))
                app.gauge_run.set(app.gauge_box.cget("values")[-1])                    # an older run
                app.draw_gauges()
                self.assertTrue(app.gauge_run.get().startswith("2026-04-14"))
                self.assertEqual(len(app.gauge_cards), 8)
                app.history = []                                                       # nothing to show: a message, no cards
                app.apply_fitness(app.fit_id, [], None)
                self.assertEqual(app.gauge_cards, [])
            finally:
                app._close()

    def test_interval_workout_is_marked_in_the_detail_and_the_fitness_text(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026.04.14 07.00", "2026.04.21 07.00"):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=4.0, base_speed=2.6, icu_format=True)
            write_intervals_run(os.path.join(d, "2026.04.28 07.00-RUNNING.csv"))
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            try:
                app.fetch.set(False)
                app.import_now()
                app.update()
                key = "2026-04-28|07:00"
                app.tree.selection_set(key)
                app.show_detail()
                self.assertIn("Interval workout", app.detail.get("1.0", "end"))
                app.fitness_now()
                m = app.adv[("2026-04-28", "07:00")]
                self.assertEqual(len(m["reps"]), 3)
                self.assertIn("interval workout", app.fit_view["next"]["sub"])           # the advice says where its pace comes from
                self.assertEqual(len(app.fit_panel["reps"]), 3)                          # and the panel lists the repetitions
                self.assertEqual(app.fit_panel["badge"], "INTERVALS")
                self.assertEqual(len(app.gauge_cards), 8)
            finally:
                app._close()

    def test_athlete_dialog_values_are_checked_and_saved(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026.04.14 07.00", "2026.04.21 07.00", "2026.04.28 07.00"):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=4.0, base_speed=2.6, icu_format=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))
            ok = {"hr_max": "185", "hr_rest": "55", "mass_kg": "70,5", "age": "40", "height_cm": "175"}
            try:
                app.fetch.set(False)
                app.import_now()
                values = app.apply_athlete(ok, "F", False)
                self.assertEqual((values["mass_kg"], values["age"], values["sex"], values["hr_max_estimated"]), (70.5, 40, "F", None))
                saved = gui.rm.read_athlete(d)
                self.assertEqual((saved["hr_max"], saved["hr_rest"], saved["age"], saved["sex"], saved["height_cm"]), (185, 55, 40, "F", 175))
                for bad in (dict(ok, hr_max="abc"), dict(ok, hr_max="300"), dict(ok, age="5"), dict(ok, mass_kg=""), dict(ok, height_cm="17")):
                    with self.assertRaises(ValueError):
                        app.apply_athlete(bad, "F", False)
                with self.assertRaises(ValueError):
                    app.apply_athlete(ok, "X", False)
                self.assertEqual(gui.rm.read_athlete(d)["age"], 40)                       # nothing was saved by the failed attempts
                app.apply_athlete(dict(ok, age=""), "", True)                              # age and sex are optional
                saved = gui.rm.read_athlete(d)
                self.assertEqual((saved["age"], saved["sex"], saved["hr_max_estimated"]), (None, None, True))
            finally:
                app._close()

    def test_health_sync_folder_is_remembered_and_checked(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as fit:
            cfg = os.path.join(d, "cfg.json")
            app = gui.App(data_dir=d, config_path=cfg)
            try:
                app.fit_dir.set(fit)
                app.set_fit_dir()
                self.assertEqual(gui.core.FIT_DIR, fit)
                self.assertEqual(json.load(open(cfg))["fit_dir"], fit)
                app.fit_dir.set(os.path.join(d, "missing"))
                app.set_fit_dir()
                self.assertIsNone(gui.core.FIT_DIR)                        # a folder that does not exist is ignored
                self.assertIn("does not exist", app.status.get())
            finally:
                gui.core.FIT_DIR = None
                app._close()

    def test_fitness_tab_computes_in_a_thread_when_opened(self):
        gui = load_gui()
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026.04.14 07.00", "2026.04.21 07.00", "2026.04.28 07.00"):
                write_run(os.path.join(d, f"{name}-RUNNING.csv"), km=4.0, base_speed=2.6, icu_format=True)
            app = gui.App(data_dir=d, config_path=os.path.join(d, "cfg.json"))

            def wait():
                for _ in range(200):
                    app.update()
                    if app.fit_rows and not app.fit_stale:
                        break
                    time.sleep(0.05)

            try:
                app.fetch.set(False)
                app.import_now()                                          # an import opens the Fitness tab, which computes in a thread
                wait()
                self.assertEqual(len(app.fit_rows), 3)
                self.assertEqual(len(app.fit_tree.get_children()), 3)
                self.assertIsNotNone(app.fit_view["next"])
                self.assertFalse(app.fit_stale)
                # the history changes while another tab is showing: stale, but nothing is computed until a tab needs it
                app.tabs.select(0)
                app.update()
                before = app.fit_thread
                app.start_fitness()
                self.assertTrue(app.fit_stale)
                self.assertIs(app.fit_thread, before)
                app.tabs.select(app.fit_tab)
                wait()
                self.assertFalse(app.fit_stale)
                self.assertIsNot(app.fit_thread, before)
            finally:
                app._close()


if __name__ == "__main__":
    unittest.main()
