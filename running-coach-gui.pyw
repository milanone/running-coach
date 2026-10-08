#!/usr/bin/env python3
"""running-coach-gui.pyw – Tkinter front-end for parse_run_csv.py (standard library only).

Double-click on Windows (opens without a console) or run: py running-coach-gui.pyw
"""
import json
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_run_csv as core  # noqa: E402

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".running_coach_gui.json")

# label, history column, parser, formatter, higher-is-better-plotted-up
METRICS = [
    ("Distance (km)", "dist_km", core.safe_float, lambda v: f"{v:.1f}", False),
    ("Pace (min/km)", "pace_minkm", lambda v: (core.parse_pace(v) or 0) / 60 or None,
     lambda v: core.pace_csv(v * 60), True),
    ("Avg HR (bpm)", "avg_hr_bpm", core.safe_float, lambda v: f"{v:.0f}", False),
    ("EF (m/beat/min)", "ef_m_beat", core.safe_float, lambda v: f"{v:.3f}", False),
    ("Drift (%)", "drift_pct", core.safe_float, lambda v: f"{v:.1f}", False),
    ("TSS", "tss", core.safe_float, lambda v: f"{v:.0f}", False),
]
TABLE_COLUMNS = [
    ("date", "Date", 90), ("time", "Time", 50), ("dist_km", "km", 55), ("duration_hms", "Duration", 75),
    ("pace_minkm", "Pace", 60), ("avg_hr_bpm", "HR", 50), ("ef_m_beat", "EF", 60),
    ("drift_pct", "Drift %", 65), ("tss", "TSS", 45), ("temp_c", "°C", 45), ("legs", "Legs", 75),
]


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(path, cfg):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


class LegsDialog(tk.Toplevel):
    """Modal dialog asking how the legs feel; result in .choice (None if cancelled)."""

    def __init__(self, parent, text, default="ok"):
        super().__init__(parent)
        self.title("How do your legs feel")
        self.transient(parent)
        self.resizable(False, False)
        self.choice = None
        ttk.Label(self, text=text, padding=12, justify="left").pack()
        self.var = tk.StringVar(value=default)
        for opt in core.LEG_OPTIONS:
            ttk.Radiobutton(self, text=opt, value=opt, variable=self.var).pack(anchor="w", padx=24)
        row = ttk.Frame(self, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="Skip", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(row, text="OK", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_set()

    def _ok(self):
        self.choice = self.var.get()
        self.destroy()


class App(tk.Tk):
    def __init__(self, data_dir=None, config_path=CONFIG_PATH):
        super().__init__()
        self.title("Running Coach")
        self.geometry("1100x720")
        self.minsize(900, 600)
        self.config_path = config_path
        cfg = load_config(config_path)
        self.history = []
        self.sort_col, self.sort_desc = "date", True
        self.queue = queue.Queue()
        self.busy = False

        self.data_dir = tk.StringVar(value=data_dir or os.environ.get("RUNNING_COACH_DATA") or cfg.get("data_dir", ""))
        self.fetch = tk.BooleanVar(value=cfg.get("fetch_weather", True))
        self.metric = tk.StringVar(value=METRICS[0][0])
        self.status = tk.StringVar(value="")

        self._build_top()
        self._build_tabs()
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken", padding=(8, 2)).pack(fill="x", side="bottom")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.report_callback_exception = self._on_error
        self.after(150, self._poll)
        self.reload()

    # ── layout ───────────────────────────────────────────────────────────
    def _build_top(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="Data folder:").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.data_dir)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.reload())
        ttk.Button(bar, text="Browse…", command=self.browse).pack(side="left")
        ttk.Checkbutton(bar, text="Online weather", variable=self.fetch).pack(side="left", padx=10)
        self.btn_import = ttk.Button(bar, text="Import new runs", command=self.start_import)
        self.btn_import.pack(side="left")
        self.btn_all = ttk.Button(bar, text="Reprocess all", command=self.reprocess_all)
        self.btn_all.pack(side="left", padx=(6, 0))

    def _build_tabs(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self._build_history_tab()
        self._build_chart_tab()
        self._build_reco_tab()

    def _build_history_tab(self):
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="History")
        paned = ttk.PanedWindow(tab, orient="vertical")
        paned.pack(fill="both", expand=True)

        top = ttk.Frame(paned)
        paned.add(top, weight=3)
        self.tree = ttk.Treeview(top, columns=[c[0] for c in TABLE_COLUMNS], show="headings", selectmode="browse")
        for key, title, width in TABLE_COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor="center")
        self.update_headings()
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("sore", background="#fde2e2")
        self.tree.tag_configure("heavy", background="#fff1d6")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())

        bottom = ttk.Frame(paned, padding=(0, 6, 0, 0))
        paned.add(bottom, weight=2)
        left = ttk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True)
        self.detail = tk.Text(left, height=12, width=52, state="disabled", wrap="word", relief="flat",
                              background=self.cget("background"), font=("TkDefaultFont", 10))
        self.detail.pack(fill="both", expand=True)
        legs = ttk.Frame(left)
        legs.pack(fill="x", pady=(4, 0))
        ttk.Label(legs, text="Legs:").pack(side="left")
        self.legs_var = tk.StringVar()
        self.legs_box = ttk.Combobox(legs, textvariable=self.legs_var, values=[""] + core.LEG_OPTIONS,
                                     width=10, state="readonly")
        self.legs_box.pack(side="left", padx=6)
        ttk.Button(legs, text="Save", command=self.save_legs).pack(side="left")

        right = ttk.Frame(bottom)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        cols = ("km", "pace", "hr", "dplus", "note")
        self.splits = ttk.Treeview(right, columns=cols, show="headings", height=8)
        for key, title, width in (("km", "km", 50), ("pace", "Pace", 70), ("hr", "HR", 55),
                                  ("dplus", "d+", 55), ("note", "", 70)):
            self.splits.heading(key, text=title)
            self.splits.column(key, width=width, anchor="center")
        self.splits.pack(fill="both", expand=True)

    def _build_chart_tab(self):
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="Charts")
        row = ttk.Frame(tab, padding=8)
        row.pack(fill="x")
        ttk.Label(row, text="Metric:").pack(side="left")
        box = ttk.Combobox(row, textvariable=self.metric, values=[m[0] for m in METRICS],
                           state="readonly", width=24)
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self.draw_chart())
        self.canvas = tk.Canvas(tab, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda e: self.draw_chart())

    def _build_reco_tab(self):
        tab = ttk.Frame(self.tabs, padding=24)
        self.tabs.add(tab, text="Next session")
        self.reco_head = ttk.Label(tab, text="", font=("TkDefaultFont", 11))
        self.reco_head.pack(anchor="w")
        self.reco_vars = {k: tk.StringVar(value="—") for k in ("rest", "dist", "pace", "ef")}
        grid = ttk.Frame(tab)
        grid.pack(anchor="w", pady=16)
        for i, (key, label) in enumerate((("rest", "Rest"), ("dist", "Distance"),
                                          ("pace", "Pace"), ("ef", "EF trend"))):
            ttk.Label(grid, text=label, width=12).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Label(grid, textvariable=self.reco_vars[key], font=("TkDefaultFont", 16, "bold")).grid(
                row=i, column=1, sticky="w")
        self.reco_notes = ttk.Label(tab, text="", justify="left", wraplength=700)
        self.reco_notes.pack(anchor="w")

    # ── data ─────────────────────────────────────────────────────────────
    def _dir(self):
        d = self.data_dir.get().strip()
        return d if d and os.path.isdir(d) else None

    def browse(self):
        d = filedialog.askdirectory(title="Folder with the CSVs and the history", initialdir=self._dir() or ".")
        if d:
            self.data_dir.set(d)
            self.reload()

    def reload(self):
        d = self._dir()
        self.history = core.load_history(core.history_path(d)) if d else []
        if d:
            self._save_config()
        for b in (self.btn_import, self.btn_all):
            b.state(["!disabled"] if d else ["disabled"])
        self.refresh_table()
        self.draw_chart()
        self.refresh_reco()
        self.status.set(f"{len(self.history)} sessions in the history." if d else
                        "Choose the folder that holds the run CSVs and running_history.csv.")

    def save_history(self):
        d = self._dir()
        if d:
            core.save_history(self.history, core.history_path(d))

    def refresh_table(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for r in self.sorted_history():
            iid = "|".join(core.session_key(r))
            self.tree.insert("", "end", iid=iid, values=[r.get(k, "") for k, _, _ in TABLE_COLUMNS],
                             tags=(r.get("legs", ""),))
        kids = self.tree.get_children()
        target = select if select in kids else (kids[0] if kids else None)
        if target:
            self.tree.selection_set(target)
            self.tree.see(target)
        else:
            self.show_detail()

    def sort_value(self, row, key):
        raw = str(row.get(key) or "").strip()
        if key == "date":
            return core.session_key(row)
        if not raw:
            return None
        if key == "time":
            return raw
        if key == "pace_minkm":
            return core.parse_pace(raw)
        if key == "duration_hms":
            try:
                h, m, sec = (int(x) for x in raw.split(":"))
                return h * 3600 + m * 60 + sec
            except ValueError:
                return None
        if key == "legs":
            return core.LEG_OPTIONS.index(raw) if raw in core.LEG_OPTIONS else None
        return core.safe_float(raw)

    def sorted_history(self):
        """History ordered by the active column; rows without a value always go last."""
        rows = sorted(self.history, key=core.session_key, reverse=True)
        keyed = [(self.sort_value(r, self.sort_col), r) for r in rows]
        present = sorted((kr for kr in keyed if kr[0] is not None), key=lambda kr: kr[0], reverse=self.sort_desc)
        return [r for _, r in present] + [r for v, r in keyed if v is None]

    def update_headings(self):
        for key, title, _ in TABLE_COLUMNS:
            arrow = (" ▼" if self.sort_desc else " ▲") if key == self.sort_col else ""
            self.tree.heading(key, text=title + arrow)

    def sort_by(self, key):
        if key == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = key, key == "date"
        self.update_headings()
        current = self.tree.selection()
        self.refresh_table(select=current[0] if current else None)

    def selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((r for r in self.history if "|".join(core.session_key(r)) == sel[0]), None)

    # ── detail ───────────────────────────────────────────────────────────
    def _write_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def show_detail(self):
        self.splits.delete(*self.splits.get_children())
        row = self.selected_row()
        if not row:
            self._write_detail("")
            self.legs_var.set("")
            return
        self.legs_var.set(row.get("legs", ""))
        lines = [f"{row['date']} {row['time']}   {row['dist_km']} km   {row['duration_hms']}   {row['pace_minkm']}/km",
                 f"HR {row['avg_hr_bpm']} bpm   EF {row['ef_m_beat']}   Drift {row['drift_pct']}%   TSS {row['tss']}"]
        if row.get("temp_c"):
            lines.append(f"Weather: {row['temp_c']}°C  {row.get('humidity_pct', '')}% humidity  {row.get('wind_kmh', '')} km/h wind")
        d = self._dir()
        path = core.session_file(d, row) if d else None
        if not path:
            lines.append("\nRun CSV not found in the folder: per-km detail not available.")
        else:
            try:
                res = core.analyze_file(path, fetch=False)
            except ValueError as e:
                lines.append(f"\nCannot read the CSV: {e}")
            else:
                lines.append(f"Elevation: +{res['elev']:.0f} m")
                if res["is_sprint"]:
                    sk = res["splits"][res["sprint_idx"]]
                    lines.append(f"⚡ Final sprint (km {sk['km']}, {core.format_pace(sk['pace_s'])}/km): excluded from the drift")
                for w in core.weather_warnings({"temp_c": core.safe_float(row.get("temp_c")) or 0,
                                                "humidity_pct": core.safe_float(row.get("humidity_pct")) or 0}):
                    lines.append(f"⚠ {w}")
                for i, sp in enumerate(res["splits"]):
                    self.splits.insert("", "end", values=(
                        sp["km"], core.format_pace(sp["pace_s"]),
                        f"{sp['avg_hr']:.0f}" if sp.get("avg_hr") else "—",
                        f"+{sp['elev_gain']:.0f}" if sp["elev_gain"] > 0.5 else "—",
                        "⚡ sprint" if res["is_sprint"] and i == res["sprint_idx"] else ""))
        self._write_detail("\n".join(lines))

    def save_legs(self):
        row = self.selected_row()
        if row is None:
            return
        row["legs"] = self.legs_var.get()
        self.save_history()
        self.refresh_table(select="|".join(core.session_key(row)))
        self.refresh_reco()
        self.status.set(f"Legs of {row['date']} updated.")

    # ── chart ────────────────────────────────────────────────────────────
    def draw_chart(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 200 or h < 150:
            return
        label, key, parse, fmt, invert = next(m for m in METRICS if m[0] == self.metric.get())
        data = []
        for r in sorted(self.history, key=core.session_key):
            v = parse(r.get(key, ""))
            if v is not None:
                try:
                    day = datetime.strptime(core.session_key(r)[0], "%Y-%m-%d")
                except ValueError:
                    continue
                data.append((day, v))
        c.create_text(w / 2, 14, text=label + ("  (higher = faster)" if invert else ""),
                      font=("TkDefaultFont", 11, "bold"))
        if len(data) < 2:
            c.create_text(w / 2, h / 2, text="Not enough data for the chart", fill="#777")
            return
        left, right, top, bottom = 70, 24, 36, 44
        vals = [v for _, v in data]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.12 or abs(hi) * 0.05 or 1
        lo, hi = lo - pad, hi + pad
        t0, t1 = data[0][0].toordinal(), data[-1][0].toordinal()
        span = max(t1 - t0, 1)

        def X(day):
            return left + (day.toordinal() - t0) / span * (w - left - right)

        def Y(v):
            f = (v - lo) / (hi - lo)
            return top + (f if invert else 1 - f) * (h - top - bottom)

        for i in range(5):
            v = lo + (hi - lo) * i / 4
            y = Y(v)
            c.create_line(left, y, w - right, y, fill="#e3e3e3")
            c.create_text(left - 8, y, text=fmt(v), anchor="e", fill="#555")
        step = max(1, len(data) // 8)
        for i, (day, _) in enumerate(data):
            if i % step == 0 or i == len(data) - 1:
                x = X(day)
                c.create_line(x, h - bottom, x, h - bottom + 4, fill="#888")
                c.create_text(x, h - bottom + 16, text=day.strftime("%d/%m"), fill="#555")
        c.create_line(left, top, left, h - bottom, fill="#888")
        c.create_line(left, h - bottom, w - right, h - bottom, fill="#888")

        coords = [(X(d), Y(v)) for d, v in data]
        c.create_line(*[p for xy in coords for p in xy], fill="#2f6fdb", width=2)
        if len(vals) >= 3:
            ma = [sum(vals[max(0, i - 2):i + 1]) / len(vals[max(0, i - 2):i + 1]) for i in range(len(vals))]
            c.create_line(*[p for (d, _), m in zip(data, ma) for p in (X(d), Y(m))],
                          fill="#e08a1e", width=2, dash=(5, 3))
        for (x, y), (_, v) in zip(coords, data):
            c.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#2f6fdb", outline="white")
            if len(data) <= 24:
                c.create_text(x, y - 12, text=fmt(v), fill="#333", font=("TkDefaultFont", 8))
        c.create_text(w - right, h - 8, anchor="e", fill="#e08a1e", text="- - moving average (3 sessions)")

    # ── recommendation ───────────────────────────────────────────────────
    def refresh_reco(self):
        for v in self.reco_vars.values():
            v.set("—")
        self.reco_notes.configure(text="")
        if not self.history:
            self.reco_head.configure(text="No session in the history.")
            return
        last = sorted(self.history, key=core.session_key)[-1]
        legs = last.get("legs") or "ok"
        rec = core.compute_recommendations(self.history, last, legs)
        self.reco_head.configure(text=f"After the run of {last['date']} {last['time']} "
                                      f"({last['dist_km']} km, legs: {last.get('legs') or 'not set'}):")
        n = rec["rest"]
        self.reco_vars["rest"].set(f"{n} {'day' if n == 1 else 'days'}")
        self.reco_vars["dist"].set(f"{rec['dist_km']:.1f} km")
        self.reco_vars["pace"].set(f"{core.format_pace(rec['pace_s'])} /km")
        self.reco_vars["ef"].set({"improving": "↑ improving", "declining": "↓ declining"}.get(rec["ef_trend"], "—"))
        self.reco_notes.configure(text="\n".join(f"{i}  {t}" for i, t in rec["notes"]))

    # ── import ───────────────────────────────────────────────────────────
    def set_busy(self, busy, msg=""):
        self.busy = busy
        for b in (self.btn_import, self.btn_all):
            b.state(["disabled"] if busy or not self._dir() else ["!disabled"])
        if msg:
            self.status.set(msg)

    def start_import(self, reprocess=False):
        d = self._dir()
        if not d or self.busy:
            return
        self.set_busy(True, "Analysing the CSVs…")
        fetch = self.fetch.get()

        def work():
            try:
                self.queue.put(("done", core.import_sessions(d, fetch, reprocess)))
            except Exception as e:  # reported on the UI thread
                self.queue.put(("error", e))

        threading.Thread(target=work, daemon=True).start()

    def reprocess_all(self):
        if messagebox.askyesno("Reprocess all",
                               "Recompute every run from the CSVs and overwrite the metrics in the history "
                               "(weather and legs already recorded are kept). Continue?"):
            self.start_import(reprocess=True)

    def import_now(self, reprocess=False):
        """Synchronous import (used by tests)."""
        self.finish_import(core.import_sessions(self._dir(), self.fetch.get(), reprocess))

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "done":
                    self.finish_import(payload)
                else:
                    self.set_busy(False, "Error during the import.")
                    messagebox.showerror("Error", str(payload))
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def ask_legs(self, text):
        dlg = LegsDialog(self, text)
        self.wait_window(dlg)
        return dlg.choice

    def finish_import(self, result):
        self.set_busy(False)
        sessions, errors = result["sessions"], result["errors"]
        history = core.merge_sessions(result["history"], sessions)
        if sessions:
            newest = sessions[-1]
            if core.is_latest(history, newest):
                legs = self.ask_legs(f"How do your legs feel after the run of {newest['date']} {newest['time']}?")
                if legs:
                    newest["legs"] = legs
        self.history = history
        self.save_history()
        self.refresh_table()
        self.draw_chart()
        self.refresh_reco()
        msg = f"{len(sessions)} sessions imported, {result['skipped']} already present"
        if errors:
            msg += f", {len(errors)} unreadable files"
        self.status.set(msg + ".")
        if errors:
            messagebox.showwarning("Files not imported", "\n".join(f"{f}: {m}" for f, m in errors))
        if sessions:
            self.tabs.select(2)

    # ── misc ─────────────────────────────────────────────────────────────
    def _on_error(self, exc, val, tb):
        messagebox.showerror("Error", f"{exc.__name__}: {val}")

    def _save_config(self):
        save_config(self.config_path, {"data_dir": self.data_dir.get(), "fetch_weather": self.fetch.get()})

    def _close(self):
        self._save_config()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
