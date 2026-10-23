#!/usr/bin/env python3
"""running-coach-gui.pyw – Tkinter front-end for parse_run_csv.py (standard library only).

Double-click on Windows (opens without a console) or run: py running-coach-gui.pyw
"""
import base64
import bisect
import json
import math
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_run_csv as core  # noqa: E402
import run_metrics as rm  # noqa: E402

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
    ("VO2max estimate (ml/kg/min)", "adv.vo2max", lambda v: v, lambda v: f"{v:.1f}", False),
    ("Pace at a fixed HR (min/km)", "adv.pace_at_hr", lambda v: v and v / 60, lambda v: core.pace_csv(v * 60), True),
    ("HR at the usual speed (bpm)", "adv.hr_at_speed", lambda v: v, lambda v: f"{v:.0f}", False),
    ("Fitness, CTL", "adv.ctl", lambda v: v, lambda v: f"{v:.1f}", False),
    ("Fatigue, ATL", "adv.atl", lambda v: v, lambda v: f"{v:.1f}", False),
    ("Form, TSB", "adv.tsb", lambda v: v, lambda v: f"{v:+.1f}", False),
    ("TRIMP (Edwards)", "adv.trimp", lambda v: v, lambda v: f"{v:.0f}", False),
    ("Training effect (indicative)", "adv.te", lambda v: v, lambda v: f"{v:.1f}", False),
    ("Performance condition (%)", "adv.perf_pct", lambda v: v, lambda v: f"{v:+.1f}", False),
    ("Cadence (steps/min)", "adv.spm", lambda v: v, lambda v: f"{v:.0f}", False),
    ("Step length (m)", "adv.step_m", lambda v: v, lambda v: f"{v:.2f}", False),
    ("Power, rough model (W)", "adv.power_w", lambda v: v, lambda v: f"{v:.0f}", False),
    ("Second half vs first (%)", "adv.split_balance_pct", lambda v: v, lambda v: f"{v:+.1f}", False),
    ("HR rise time (s)", "adv.hr_rise_s", lambda v: v, lambda v: f"{v:.0f}", False),
    ("HR recovery in 60 s (bpm)", "adv.hrr60", lambda v: v, lambda v: f"{v:.0f}", False),
]
RUN_PANELS = [  # title, [(key in the series, colour, label)]: every value column of the CSV
    ("Speed (km/h)", [("spd", "#2f6fdb", "velocity_smooth")]),
    ("Heart rate (bpm)", [("hr", "#d64545", "heartrate")]),
    ("Cadence", [("cad", "#8a5cc2", "cadence")]),
    ("Elevation (m)", [("alt_raw", "#9a9a9a", "altitude"), ("alt_fixed", "#4a9a5b", "fixed_altitude")]),
]
SPEED_STOPS = [(0.0, (43, 92, 214)), (0.35, (47, 181, 160)), (0.7, (242, 194, 0)), (1.0, (214, 69, 69))]   # slow -> fast


def speed_color(f):
    """Hex colour for f in 0..1 on the slow (blue) to fast (red) scale."""
    f = max(0.0, min(1.0, f))
    for (a, ca), (b, cb) in zip(SPEED_STOPS, SPEED_STOPS[1:]):
        if f <= b:
            t = (f - a) / (b - a)
            return "#%02x%02x%02x" % tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


# The list of runs: key, title, width. The history fields come from the row, the metrics from the per-run results (blank until they are computed),
# `note` says whether a run is an interval workout or several recordings merged. DEFAULT_COLUMNS are shown, the rest is in the Columns menu.
RUN_COLUMNS = [
    ("date", "Date", 78), ("time", "Time", 46), ("dist_km", "km", 46), ("duration_hms", "Duration", 66), ("pace_minkm", "Pace", 52),
    ("avg_hr_bpm", "HR", 40), ("tss", "TSS", 40), ("note", "Note", 112), ("trimp", "TRIMP", 50), ("te", "TE*", 40), ("vo2max", "VO2max", 56),
    ("ef_m_beat", "EF", 50), ("drift_pct", "Drift %", 56), ("pace_at_hr", "Pace@HR", 60), ("hr_at_speed", "HR@speed", 66),
    ("spm", "Steps/min", 62), ("step_m", "Step m", 50), ("power_w", "W*", 40), ("perf_pct", "Perf %", 50), ("tsb", "Form", 46),
    ("moving", "Moving", 56), ("temp_c", "\u00b0C", 40),
]
DEFAULT_COLUMNS = ["date", "time", "dist_km", "pace_minkm", "avg_hr_bpm", "tss", "note"]
HISTORY_COLUMNS = {"date", "time", "dist_km", "duration_hms", "pace_minkm", "avg_hr_bpm", "ef_m_beat", "drift_pct", "tss", "temp_c"}


def _opt(m, key, fmt):
    v = m.get(key)
    return "" if v is None else fmt(v)


METRIC_CELLS = {      # the text of a metric in the list, from a row of compute_all
    "trimp": lambda m: _opt(m, "trimp", lambda v: f"{v:.0f}"), "te": lambda m: _opt(m, "te", lambda v: f"{v:.1f}"),
    "vo2max": lambda m: _opt(m, "vo2max", lambda v: f"{v:.1f}"), "pace_at_hr": lambda m: _opt(m, "pace_at_hr", core.format_pace),
    "hr_at_speed": lambda m: _opt(m, "hr_at_speed", lambda v: f"{v:.0f}"), "spm": lambda m: _opt(m, "spm", lambda v: f"{v:.0f}"),
    "step_m": lambda m: _opt(m, "step_m", lambda v: f"{v:.2f}"), "power_w": lambda m: _opt(m, "power_w", lambda v: f"{v:.0f}"),
    "perf_pct": lambda m: _opt(m, "perf_pct", lambda v: f"{v:+.1f}"), "tsb": lambda m: _opt(m, "tsb", lambda v: f"{v:+.0f}"),
    "moving": lambda m: _opt(m, "moving_s", rm.fmt_time),
}
METRIC_FIELD = {"moving": "moving_s"}               # the column key when it differs from the field of the metrics
KIND_FILTERS = ["All runs", "Steady runs", "Interval workouts"]
PERIOD_FILTERS = ["All time", "Last 90 days", "Last 30 days"]
NOW_GAUGES = ("load_ratio", "form", "vo2max")      # the gauges that are about today, not about a run: they go on the Today tab


FIT_CARD_W, FIT_PANEL_W = 235, 330                                     # widths in pixels of the cards and of the panel of a run
ZONE_FILL = ["#7aa7d9", "#4caf73", "#e6c04a", "#e8903a", "#d95050"]      # zones 1 to 5
GAUGE_W, GAUGE_H = 300, 168
GAUGE_FILL = {"great": "#2e8b57", "good": "#4caf73", "ok": "#e6c04a", "warn": "#e8903a", "bad": "#d95050", "info": "#7aa7d9"}        # the bands of a gauge
GAUGE_TEXT = {"great": "#1b6b3e", "good": "#1f7a45", "ok": "#8a6d00", "warn": "#b35f10", "bad": "#b02a2a", "info": "#3d6ea8"}        # the verdict text
GAUGE_LEVEL = {"sourced": "from a source that was read", "secondary": "from secondary sources", "mine": "a rule of thumb of this program"}


WINDOW_FRACTION = 0.8                      # the window opens at this fraction of the width and of the height of the screen
MIN_WINDOW = (900, 600)


def window_geometry(screen_w, screen_h, fraction=WINDOW_FRACTION):
    """Tk geometry string 'WxH+X+Y' of a window that takes `fraction` of the screen (not below MIN_WINDOW, not beyond the screen), centred."""
    w = min(screen_w, max(MIN_WINDOW[0], int(screen_w * fraction)))
    h = min(screen_h, max(MIN_WINDOW[1], int(screen_h * fraction)))
    return f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}"


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


class App(tk.Tk):
    def __init__(self, data_dir=None, config_path=CONFIG_PATH):
        super().__init__()
        self.title("Running Coach")
        self.geometry(window_geometry(self.winfo_screenwidth(), self.winfo_screenheight()))
        self.minsize(*MIN_WINDOW)
        self.config_path = config_path
        cfg = load_config(config_path)
        self.history = []
        known = {c[0] for c in RUN_COLUMNS}
        self.columns = {k for k in (cfg.get("columns") or DEFAULT_COLUMNS) if k in known} or set(DEFAULT_COLUMNS)
        self.last_run = cfg.get("last_run")                 # the run that was selected when the program was closed
        self.adv = {}
        self.sort_col, self.sort_desc = "date", True
        self.queue = queue.Queue()
        self.busy = False
        self.run = None
        self.run_geom = None

        sibling = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "running_coach_data")
        self.data_dir = tk.StringVar(value=data_dir or os.environ.get("RUNNING_COACH_DATA") or cfg.get("data_dir", "")
                                     or (sibling if os.path.isdir(sibling) else ""))
        self.fetch = tk.BooleanVar(value=cfg.get("fetch_weather", True))
        self.fit_dir = tk.StringVar(value=cfg.get("fit_dir", "") or core.FIT_DIR or "")
        core.FIT_DIR = self.fit_dir.get().strip() or None
        self.metric = tk.StringVar(value=METRICS[0][0])
        self.status = tk.StringVar(value="")

        self._build_top()
        self._build_tabs()
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken", padding=(8, 2)).pack(fill="x", side="bottom")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.report_callback_exception = self._on_error
        self._poll_job = self.after(150, self._poll)
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
        self.btn_import = ttk.Button(bar, text="Import local CSVs (legacy)", command=self.start_import)
        self.btn_import.pack(side="left")
        self.btn_all = ttk.Button(bar, text="Reprocess all", command=self.reprocess_all)
        self.btn_all.pack(side="left", padx=(6, 0))
        self.btn_download = ttk.Button(bar, text="Download new sessions from intervals.icu",
                                       command=lambda: self.start_import(download=True))
        self.btn_download.pack(side="left", padx=(6, 0))
        bar2 = ttk.Frame(self, padding=(8, 0, 8, 4))
        bar2.pack(fill="x")
        ttk.Button(bar2, text="Athlete…", command=self.edit_athlete).pack(side="left", padx=(0, 14))
        ttk.Label(bar2, text="Health Sync folder (FIT files, optional):").pack(side="left")
        fit_entry = ttk.Entry(bar2, textvariable=self.fit_dir)
        fit_entry.pack(side="left", fill="x", expand=True, padx=6)
        fit_entry.bind("<Return>", lambda e: self.set_fit_dir())
        ttk.Button(bar2, text="Browse…", command=self.browse_fit).pack(side="left")

    def _build_tabs(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self._build_today_tab()
        self._build_runs_tab()
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())

    def _build_runs_tab(self):
        """The list of runs, always at the left, and for the selected run its Summary, Chart, Map and Gauges."""
        tab = ttk.Frame(self.tabs)
        self.runs_tab = tab
        self.tabs.add(tab, text="Runs")
        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        paned.add(left, weight=2)
        bar = ttk.Frame(left, padding=(0, 4, 0, 4))
        bar.pack(fill="x")
        self.filter_kind = tk.StringVar(value=KIND_FILTERS[0])
        self.filter_period = tk.StringVar(value=PERIOD_FILTERS[0])
        for var, values, width in ((self.filter_kind, KIND_FILTERS, 16), (self.filter_period, PERIOD_FILTERS, 12)):
            box = ttk.Combobox(bar, textvariable=var, values=values, state="readonly", width=width)
            box.pack(side="left", padx=(0, 4))
            box.bind("<<ComboboxSelected>>", lambda e: self.refresh_filtered())
        self.col_menu = tk.Menu(self, tearoff=0)
        self.col_vars = {}
        for key, title, _ in RUN_COLUMNS:
            self.col_vars[key] = tk.BooleanVar(value=key in self.columns)
            self.col_menu.add_checkbutton(label=title, variable=self.col_vars[key], command=lambda k=key: self.toggle_column(k))
        ttk.Menubutton(bar, text="Columns", menu=self.col_menu).pack(side="right")
        body = ttk.Frame(left)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in RUN_COLUMNS], show="headings", selectmode="extended")
        for key, title, width in RUN_COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor="center")
        self.tree["displaycolumns"] = self.columns_shown()
        self.update_headings()
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("merged", background="#e6f0ff")
        self.tree.tag_configure("intervals", foreground="#3d6ea8")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.tree.bind("<Double-1>", lambda e: self.run_tabs.select(self.run_tab) if self.tree.identify_row(e.y) else None)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Merge runs", command=self.merge_selected)
        self.menu.add_command(label="Unmerge runs", command=self.unmerge_selected)
        self.tree.bind("<Button-3>", self.show_menu)
        right = ttk.Frame(paned)
        paned.add(right, weight=5)
        self.run_tabs = ttk.Notebook(right)
        self.run_tabs.pack(fill="both", expand=True)
        self.run_tabs.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        self._build_summary_tab()
        self._build_run_tab()
        self._build_map_tab()
        self._build_gauges_tab()

    def _build_summary_tab(self):
        tab = ttk.Frame(self.run_tabs)
        self.summary_tab = tab
        self.run_tabs.add(tab, text="Summary")
        self.splits, self.run_kms, self.run_extra, self._panel_blocks = None, [], {}, None
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=4, pady=(6, 4))
        self.fit_canvas, self.fit_inner = self._scroll_area(body, on_resize=lambda w: self._layout_panel(w))

    def _scroll_area(self, parent, width=None, on_resize=None):
        """A vertically scrollable frame: (canvas, inner frame). With a width the frame keeps it, else it follows the canvas."""
        canvas = tk.Canvas(parent, highlightthickness=0, background=self.cget("background"), **({"width": width} if width else {}))
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=(10, 0, 4, 6))
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        def resized(e):
            if not width:
                canvas.itemconfigure("inner", width=e.width)
            if on_resize:
                on_resize(e.width)

        canvas.bind("<Configure>", resized)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        return canvas, inner

    def _build_chart_tab(self, parent):
        tab = ttk.Frame(parent)
        self.trends_tab = tab
        parent.add(tab, text="Trends")
        row = ttk.Frame(tab, padding=8)
        row.pack(fill="x")
        ttk.Label(row, text="Metric:").pack(side="left")
        box = ttk.Combobox(row, textvariable=self.metric, values=[m[0] for m in METRICS],
                           state="readonly", width=28)
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self.draw_chart())
        self.canvas = tk.Canvas(tab, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda e: self.draw_chart())

    def show_run_tab(self, tab):
        """Open the Runs tab at one of its views (Summary, Chart, Map or Gauges)."""
        self.tabs.select(self.runs_tab)
        self.run_tabs.select(tab)

    def show_today_tab(self, tab):
        self.tabs.select(self.today_tab)
        self.today_tabs.select(tab)

    def _run_view_visible(self, tab):
        return self.tabs.select() == str(self.runs_tab) and self.run_tabs.select() == str(tab)

    # ── data ─────────────────────────────────────────────────────────────
    def _dir(self):
        d = self.data_dir.get().strip()
        return d if d and os.path.isdir(d) else None

    def browse(self):
        d = filedialog.askdirectory(title="Folder with the CSVs and the history", initialdir=self._dir() or ".")
        if d:
            self.data_dir.set(d)
            self.reload()

    def browse_fit(self):
        d = filedialog.askdirectory(title="Folder with the FIT files written by Health Sync", initialdir=self.fit_dir.get() or ".")
        if d:
            self.fit_dir.set(d)
            self.set_fit_dir()

    def set_fit_dir(self):
        """The heart rate of a run comes from the FIT of the same name when this folder has it: reread everything that depends on it."""
        d = self.fit_dir.get().strip()
        core.FIT_DIR = d if d and os.path.isdir(d) else None
        self._save_config()
        self.start_fitness()
        self.show_detail()                                    # the selected run is read again, with the new heart rate
        if d and core.FIT_DIR is None:
            self.status.set("The Health Sync folder does not exist.")
        elif core.FIT_DIR:
            try:
                import fitparse                                  # noqa: F401
            except ImportError:
                self.status.set("fitparse is not installed (py -m pip install fitparse): the FIT files cannot be read.")
                return
            self.status.set("Heart rate from the FIT files of that folder. The History columns (HR, EF, TSS...) change with "
                            "'Reprocess all'; the Fitness metrics and the run detail already use them.")

    def reload(self):
        d = self._dir()
        self.history = core.load_folder_history(d) if d else []
        if d:
            self._save_config()
        for b in (self.btn_import, self.btn_all, self.btn_download):
            b.state(["!disabled"] if d else ["disabled"])
        self.refresh_table()
        self.draw_chart()
        self.start_fitness()
        self.status.set(f"{len(self.history)} sessions in the history." if d else
                        "Choose the folder that holds the run CSVs and running_history.csv.")

    def save_history(self):
        d = self._dir()
        if d:
            core.save_history(self.history, core.history_path(d))

    def _cell(self, row, key, merged):
        """The text of one cell of the list: a field of the history row, or a metric of the run (blank until the metrics are computed)."""
        if key in HISTORY_COLUMNS:
            return row.get(key, "")
        if key == "note":
            return ", ".join(n for n, on in (("intervals", row.get("kind") == "intervals"), ("merged", core.session_key(row) in merged)) if on)
        m = self.adv.get(core.session_key(row))
        return METRIC_CELLS[key](m) if m else ""

    def _row_values(self, row, merged):
        return [self._cell(row, k, merged) for k, _, _ in RUN_COLUMNS]

    def refresh_table(self, select=None):
        self.tree.delete(*self.tree.get_children())
        merged = self.merged_keys()
        for r in self.sorted_history():
            iid = "|".join(core.session_key(r))
            tags = (("merged",) if core.session_key(r) in merged else ()) + (("intervals",) if r.get("kind") == "intervals" else ())
            self.tree.insert("", "end", iid=iid, values=self._row_values(r, merged), tags=tags)
        kids = self.tree.get_children()
        if select is None:
            select = self.last_run
        target = select if select in kids else (kids[0] if kids else None)
        if target:
            self.tree.selection_set(target)
            self.tree.see(target)
        else:
            self.show_detail()

    def refresh_filtered(self):
        current = self.tree.selection()
        self.refresh_table(select=current[0] if current else None)

    def refresh_cells(self):
        """The metrics arrived: fill their columns in place (the selection and the views stay as they are)."""
        if self.sort_col in METRIC_CELLS or self.sort_col == "note":
            self.refresh_filtered()                      # the order itself depends on them
            return
        merged = self.merged_keys()
        by_iid = {"|".join(core.session_key(r)): r for r in self.history}
        for iid in self.tree.get_children():
            if iid in by_iid:
                self.tree.item(iid, values=self._row_values(by_iid[iid], merged))

    def columns_shown(self):
        return [k for k, _, _ in RUN_COLUMNS if k in self.columns] or ["date"]

    def toggle_column(self, key):
        if self.col_vars[key].get():
            self.columns.add(key)
        else:
            self.columns.discard(key)
        if not self.columns:                              # a list needs at least one column
            self.columns.add("date")
            self.col_vars["date"].set(True)
        self.tree["displaycolumns"] = self.columns_shown()
        self._save_config()

    def sort_value(self, row, key):
        if key in METRIC_CELLS:
            m = self.adv.get(core.session_key(row))
            return None if not m else m.get(METRIC_FIELD.get(key, key))
        if key == "note":
            return self._cell(row, "note", self.merged_keys()) or None
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
        return core.safe_float(raw)

    def filtered_history(self):
        """The runs the filters above the list let through."""
        rows = self.history
        kind = self.filter_kind.get()
        if kind == KIND_FILTERS[1]:
            rows = [r for r in rows if r.get("kind") != "intervals"]
        elif kind == KIND_FILTERS[2]:
            rows = [r for r in rows if r.get("kind") == "intervals"]
        days = {PERIOD_FILTERS[1]: 90, PERIOD_FILTERS[2]: 30}.get(self.filter_period.get())
        if days:
            limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = [r for r in rows if core.session_key(r)[0] >= limit]
        return rows

    def sorted_history(self):
        """The filtered history ordered by the active column; rows without a value always go last."""
        rows = sorted(self.filtered_history(), key=core.session_key, reverse=True)
        keyed = [(self.sort_value(r, self.sort_col), r) for r in rows]
        present = sorted((kr for kr in keyed if kr[0] is not None), key=lambda kr: kr[0], reverse=self.sort_desc)
        return [r for _, r in present] + [r for v, r in keyed if v is None]

    def update_headings(self):
        for key, title, _ in RUN_COLUMNS:
            arrow = (" \u25bc" if self.sort_desc else " \u25b2") if key == self.sort_col else ""
            self.tree.heading(key, text=title + arrow)

    def sort_by(self, key):
        if key == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = key, key == "date"
        self.update_headings()
        current = self.tree.selection()
        self.refresh_table(select=current[0] if current else None)

    def merged_keys(self):
        """Keys of the sessions that absorbed other recordings (highlighted in the table)."""
        d = self._dir()
        return {core.file_key(p) for p in core.merged_members(d)} if d else set()

    def selected_rows(self):
        sel = set(self.tree.selection())
        return [r for r in self.history if "|".join(core.session_key(r)) in sel]

    def update_menu(self):
        rows = self.selected_rows()
        self.menu.entryconfigure("Merge runs", state="normal" if len(rows) >= 2 else "disabled")
        single_merged = len(rows) == 1 and core.session_key(rows[0]) in self.merged_keys()
        self.menu.entryconfigure("Unmerge runs", state="normal" if single_merged else "disabled")

    def show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.update_menu()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def merge_selected(self):
        rows, d = self.selected_rows(), self._dir()
        if len(rows) < 2 or not d:
            return
        first = sorted(rows, key=core.session_key)[0]
        warn = "\n\nThe runs are on different days." if len({r["date"] for r in rows}) > 1 else ""
        if not messagebox.askyesno("Merge runs", f"Merge {len(rows)} runs into one session starting at "
                                   f"{first['date']} {first['time']}?\n\nThe original CSVs stay in the folder; "
                                   f"'Unmerge runs' restores them.{warn}"):
            return
        try:
            self.history = core.merge_runs(d, self.history, rows)
        except ValueError as e:
            messagebox.showerror("Merge runs", str(e))
            return
        self.save_history()
        self.refresh_table(select="|".join(core.session_key(first)))
        self.draw_chart()
        self.start_fitness()
        self.status.set(f"{len(rows)} runs merged into {first['date']} {first['time']}.")

    def unmerge_selected(self):
        rows, d = self.selected_rows(), self._dir()
        if len(rows) != 1 or not d:
            return
        try:
            self.history = core.unmerge_run(d, self.history, rows[0])
        except ValueError as e:
            messagebox.showerror("Unmerge runs", str(e))
            return
        self.save_history()
        self.refresh_table(select="|".join(core.session_key(rows[0])))
        self.draw_chart()
        self.start_fitness()
        self.status.set(f"{rows[0]['date']} {rows[0]['time']} split into its original runs.")

    # ── run detail (per-second values as saved in the CSV) ───────────────
    def _build_run_tab(self):
        tab = ttk.Frame(self.run_tabs)
        self.run_tab = tab
        self.run_tabs.add(tab, text="Chart")
        row = ttk.Frame(tab, padding=8)
        row.pack(fill="x")
        self.run_title = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.run_title, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.run_x = tk.StringVar(value="Time")
        box = ttk.Combobox(row, textvariable=self.run_x, values=["Time", "Distance"], state="readonly", width=9)
        box.pack(side="right")
        ttk.Label(row, text="X axis:").pack(side="right", padx=(0, 6))
        box.bind("<<ComboboxSelected>>", lambda e: self.draw_run())
        self.run_speed = tk.StringVar(value="km/h")
        sbox = ttk.Combobox(row, textvariable=self.run_speed, values=["km/h", "min/km"], state="readonly", width=7)
        sbox.pack(side="right", padx=(0, 16))
        ttk.Label(row, text="Speed as:").pack(side="right", padx=(0, 6))
        sbox.bind("<<ComboboxSelected>>", lambda e: self.draw_run())
        self.run_canvas = tk.Canvas(tab, background="white", highlightthickness=0)
        self.run_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.run_canvas.bind("<Configure>", lambda e: self.draw_run())
        self.run_canvas.bind("<Motion>", self.run_hover)
        self.run_canvas.bind("<Leave>", lambda e: self.run_canvas.delete("hover"))

    def run_series(self):
        """One dict per CSV row: t (s), km, spd (km/h), pace (min/km, None below 3 km/h: standing), hr (bpm), cad, alt_raw,
        alt_fixed (m), lat, lon; None where the CSV has no value."""
        pts = self.run[1]
        t0 = pts[0]["ts"]
        return [{"t": (p["ts"] - t0).total_seconds(), "km": p.get("dist_m", 0) / 1000,
                 "spd": None if p.get("speed") is None else p["speed"] * 3.6, "hr": p["hr"],
                 "pace": 60 / (p["speed"] * 3.6) if p.get("speed") and p["speed"] * 3.6 >= 3 else None,
                 "cad": p.get("cadence"), "alt_raw": p.get("alt_raw"), "alt_fixed": p.get("alt_fixed"),
                 "lat": p.get("lat"), "lon": p.get("lon")}
                for p in pts]

    def draw_run(self):
        c = self.run_canvas
        c.delete("all")
        self.run_geom = None
        w, h = c.winfo_width(), c.winfo_height()
        if w < 200 or h < 200:
            return
        if not self.run:
            c.create_text(w / 2, h / 2, text="Select a run in the list", fill="#777")
            return
        data = self.run_series()
        xkey = "t" if self.run_x.get() == "Time" else "km"
        xs = [d[xkey] for d in data]
        x0, x1 = xs[0], xs[-1]
        left, right, top, bottom, gap = 70, 24, 34, 34, 16
        panels = RUN_PANELS
        if self.run_speed.get() == "min/km":           # same data, the first panel as pace (faster is up)
            panels = [("Pace (min/km, faster is up)", [("pace", RUN_PANELS[0][1][0][1], RUN_PANELS[0][1][0][2])])] + RUN_PANELS[1:]
        n = len(panels)
        ph = (h - top - bottom - (n - 1) * gap) / n

        def X(v):
            return left + (v - x0) / ((x1 - x0) or 1) * (w - left - right)

        for k, (title, series) in enumerate(panels):
            y0 = top + k * (ph + gap)
            vals = [d[key] for d in data for key, _, _ in series if d[key] is not None]
            pace = series[0][0] == "pace"
            c.create_rectangle(left, y0, w - right, y0 + ph, outline="#bbb")
            c.create_text(left + 6, y0 + 8, anchor="w", text=title, fill=series[-1][1], font=("TkDefaultFont", 9, "bold"))
            if not vals:
                c.create_text(w / 2, y0 + ph / 2, text="no values in the CSV", fill="#777")
                continue
            lo, hi = min(vals), max(vals)
            if pace:    # a few near-standing seconds would flatten the axis: show the 1st-99th percentile, the line is clamped to it
                sv = sorted(vals)
                lo, hi = sv[int(len(sv) * 0.01)], sv[min(len(sv) - 1, int(len(sv) * 0.99))]
            pad = (hi - lo) * 0.1 or 1
            lo, hi = (0 if lo >= 0 and lo - pad < 0 else lo - pad), hi + pad   # speed/cadence axes never go below 0

            def Y(v, y0=y0, lo=lo, hi=hi, flip=pace):
                f = (max(lo, min(hi, v)) - lo) / (hi - lo)
                return y0 + (f if flip else 1 - f) * ph

            for i in range(4):
                v = lo + (hi - lo) * i / 3
                c.create_line(left, Y(v), w - right, Y(v), fill="#eee")
                c.create_text(left - 6, Y(v), anchor="e", text=core.format_pace(v * 60) if pace else (f"{v:.0f}" if hi - lo > 20 else f"{v:.1f}"), fill="#555")
            for si, (key, color, label) in enumerate(series):
                if len(series) > 1:                           # legend when a panel overlays several columns
                    c.create_text(w - right - 6 - 110 * (len(series) - 1 - si), y0 + 8, anchor="e", text=label, fill=color)
                seg = []
                for d in data + [{key: None, xkey: 0}]:       # a gap in the CSV breaks the line
                    if d[key] is None:
                        if len(seg) >= 4:
                            c.create_line(*seg, fill=color, width=1)
                        seg = []
                    else:
                        seg += [X(d[xkey]), Y(d[key])]
        for i in range(7):
            v = x0 + (x1 - x0) * i / 6
            c.create_line(X(v), h - bottom + 2, X(v), h - bottom + 6, fill="#888")
            label = f"{int(v // 60)}:{int(v % 60):02d}" if xkey == "t" else f"{v:.2f} km"
            c.create_text(X(v), h - bottom + 18, text=label, fill="#555")
        self.run_geom = (left, right, top, bottom, x0, x1, xs, data, xkey, w, h)

    def run_hover(self, event):
        c = self.run_canvas
        c.delete("hover")
        if not self.run_geom:
            return
        left, right, top, bottom, x0, x1, xs, data, xkey, w, h = self.run_geom
        if not left <= event.x <= w - right:
            return
        v = x0 + (event.x - left) / (w - left - right) * (x1 - x0)
        i = min(bisect.bisect_left(xs, v), len(xs) - 1)
        d = data[i]
        c.create_line(event.x, top, event.x, h - bottom, fill="#888", dash=(3, 3), tags="hover")
        def f(v, fmt):
            return "-" if v is None else format(v, fmt)

        spd = d["spd"]
        pace = f" ({core.format_pace(3600 / spd)}/km)" if spd and spd > 0.3 else ""
        gps = f"   {d['lat']:.5f}, {d['lon']:.5f}" if d["lat"] is not None and d["lon"] is not None else ""
        text = (f"{int(d['t'] // 60)}:{int(d['t'] % 60):02d}   {d['km']:.3f} km   speed {f(spd, '.2f')} km/h{pace}   "
                f"HR {f(d['hr'], '.0f')}   cadence {f(d['cad'], '.0f')}   altitude {f(d['alt_raw'], '.1f')} m   "
                f"fixed {f(d['alt_fixed'], '.1f')} m{gps}")
        c.create_text(left, 16, anchor="w", text=text, tags="hover", font=("TkDefaultFont", 9))

    # ── map: OpenStreetMap tiles under the GPS track (drag, wheel, hover) ──
    def _build_map_tab(self):
        tab = ttk.Frame(self.run_tabs)
        self.map_tab = tab
        self.run_tabs.add(tab, text="Map")
        top = ttk.Frame(tab, padding=8)
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.run_title, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Fit", width=5, command=self.map_fit).pack(side="right")
        ttk.Label(top, text="drag to move, wheel to zoom, hover the track for the values", foreground="#666").pack(side="right", padx=10)
        c = self.map_canvas = tk.Canvas(tab, background="#e4e4e4", highlightthickness=0)
        c.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        c.bind("<Configure>", lambda e: self.draw_map())
        c.bind("<ButtonPress-1>", self.map_press)
        c.bind("<B1-Motion>", self.map_drag)
        c.bind("<ButtonRelease-1>", lambda e: c.configure(cursor=""))
        c.bind("<MouseWheel>", lambda e: self.map_zoom(e.x, e.y, 1 if e.delta > 0 else -1))
        c.bind("<Button-4>", lambda e: self.map_zoom(e.x, e.y, 1))        # X11 wheel
        c.bind("<Button-5>", lambda e: self.map_zoom(e.x, e.y, -1))
        c.bind("<Motion>", self.map_hover)
        c.bind("<Leave>", lambda e: c.delete("hover"))
        self.map_tiles = {}        # (z, x, y) -> PhotoImage, or None when the download failed
        self.map_pending = set()   # tiles asked of the worker thread and not yet back
        self.map_wanted = set()    # tiles of the current view: the worker skips the others (the view moved on)
        self.map_view = None       # (zoom, centre x, centre y in world pixels); None = fit the whole track
        self.map_geom = None       # (screen position of every point, its values) for the hover
        self.map_cache = None
        self._map_drag = None
        self.tile_req = queue.Queue()
        self.tile_queue = queue.Queue()
        self._tile_thread = None
        self._tile_job = None

    def _map_data(self):
        """The run_series() entries that have a GPS position (cached: the map redraws on every mouse move)."""
        pts = self.run[1]
        if self.map_cache is None or self.map_cache[0] is not pts:
            self.map_cache = (pts, [d for d in self.run_series() if d["lat"] is not None and d["lon"] is not None])
        return self.map_cache[1]

    def map_fit(self):
        self.map_view = None
        self.draw_map()

    def map_press(self, event):
        self._map_drag = (event.x, event.y, self.map_view) if self.map_view else None
        self.map_canvas.configure(cursor="fleur")

    def map_drag(self, event):
        if not self._map_drag:
            return
        x0, y0, (z, cx, cy) = self._map_drag
        self.map_view = (z, cx - (event.x - x0), cy - (event.y - y0))
        self.map_canvas.delete("hover")
        self.draw_map()

    def map_zoom(self, x, y, step):
        """One zoom level in or out, keeping the point under the cursor where it is."""
        if not self.map_view or not self.run:
            return
        z, cx, cy = self.map_view
        nz = max(1, min(core.MAX_ZOOM, z + step))
        if nz == z:
            return
        k = 2 ** (nz - z)
        w, h = self.map_canvas.winfo_width(), self.map_canvas.winfo_height()
        wx, wy = cx - w / 2 + x, cy - h / 2 + y                      # world pixel under the cursor
        self.map_view = (nz, wx * k - x + w / 2, wy * k - y + h / 2)
        self.draw_map()

    def _map_origin(self, w, h, data):
        if self.map_view is None:
            lats, lons = [d["lat"] for d in data], [d["lon"] for d in data]
            z = core.fit_zoom(lats, lons, w, h)
            x0, y0 = core.world_px(max(lats), min(lons), z)
            x1, y1 = core.world_px(min(lats), max(lons), z)
            self.map_view = (z, (x0 + x1) / 2, (y0 + y1) / 2)
        z, cx, cy = self.map_view
        return z, cx - w / 2, cy - h / 2                             # zoom, world pixel of the canvas corner

    def draw_map(self):
        c = self.map_canvas
        c.delete("all")
        self.map_geom = None
        if not self._run_view_visible(self.map_tab):
            return                                    # tiles are downloaded only while the Map tab is open
        w, h = c.winfo_width(), c.winfo_height()
        if w < 200 or h < 200:
            return
        if not self.run:
            c.create_text(w / 2, h / 2, text="Select a run in the list", fill="#777")
            return
        data = self._map_data()
        if not data:
            c.create_text(w / 2, h / 2, text="No GPS coordinates in this run", fill="#777")
            return
        z, ox, oy = self._map_origin(w, h, data)
        last = 2 ** z - 1
        keys = [(z, tx, ty) for tx in range(max(0, int(ox // 256)), min(last, int((ox + w) // 256)) + 1)
                for ty in range(max(0, int(oy // 256)), min(last, int((oy + h) // 256)) + 1)]
        self.map_wanted = set(keys)
        need = []
        for key in keys:
            if self.map_tiles.get(key):
                c.create_image(key[1] * 256 - ox, key[2] * 256 - oy, anchor="nw", image=self.map_tiles[key], tags="tile")
            elif key not in self.map_tiles and key not in self.map_pending:
                need.append(key)
        xy = [(px - ox, py - oy) for px, py in (core.world_px(d["lat"], d["lon"], z) for d in data)]
        self.map_geom = (xy, data)
        if len(xy) >= 2:
            c.create_line(*[v for p in xy for v in p], fill="white", width=7, capstyle="round", joinstyle="round", tags="track")
            self._draw_speed_track(xy, data)
        for (px, py), color, r in ((xy[0], "#2e9e4f", 9), (xy[-1], "#d64545", 5)):    # start (larger, under), end
            c.create_oval(px - r, py - r, px + r, py + r, fill=color, outline="white", width=2, tags="track")
        # scale bar and the attribution the OpenStreetMap tile policy requires
        lat = sum(d["lat"] for d in data) / len(data)
        mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** z
        meters = max([m for m in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000) if m / mpp <= 150] or [1])
        c.create_rectangle(8, h - 34, 24 + meters / mpp, h - 8, fill="white", outline="")
        c.create_line(16, h - 16, 16 + meters / mpp, h - 16, width=2)
        c.create_text(16, h - 26, anchor="w", text=f"{meters} m" if meters < 1000 else f"{meters // 1000} km", font=("TkDefaultFont", 8))
        t = c.create_text(w - 12, h - 12, anchor="se", text="\u00a9 OpenStreetMap contributors", font=("TkDefaultFont", 8))
        bx = c.bbox(t)
        c.create_rectangle(bx[0] - 4, bx[1] - 2, bx[2] + 4, bx[3] + 2, fill="white", outline="")
        c.tag_raise(t)
        for key in need:
            self.map_pending.add(key)
            self.tile_req.put(key)
        if need:
            if self._tile_thread is None:
                self._tile_thread = threading.Thread(target=self._tile_worker, daemon=True)
                self._tile_thread.start()
            if self._tile_job is None:
                self._tile_job = self.after(100, self._tile_poll)

    def _draw_speed_track(self, xy, data):
        """The track coloured by speed, slow (blue) to fast (red) between the 5th and 95th percentile of the run."""
        c = self.map_canvas
        speeds = sorted(d["spd"] for d in data if d["spd"] is not None and d["spd"] > 1.0)
        if not speeds:
            c.create_line(*[v for p in xy for v in p], fill="#2f6fdb", width=3, capstyle="round", joinstyle="round", tags="track")
            return
        lo, hi = speeds[int(len(speeds) * 0.05)], speeds[min(len(speeds) - 1, int(len(speeds) * 0.95))]
        span = (hi - lo) or 1.0
        bucket = lambda d: -1 if d["spd"] is None else round(max(0.0, min(1.0, (d["spd"] - lo) / span)) * 11)
        run, cur = [xy[0]], None
        for i in range(1, len(xy)):
            b = bucket(data[i])
            if b != cur and len(run) >= 2:
                c.create_line(*[v for p in run for v in p], fill="#9a9a9a" if cur < 0 else speed_color(cur / 11),
                              width=3, capstyle="round", joinstyle="round", tags="track")
                run = [run[-1]]
            cur = b
            run.append(xy[i])
        if len(run) >= 2:
            c.create_line(*[v for p in run for v in p], fill="#9a9a9a" if cur < 0 else speed_color(cur / 11),
                          width=3, capstyle="round", joinstyle="round", tags="track")
        w = c.winfo_width()
        c.create_rectangle(w - 202, 10, w - 10, 50, fill="white", outline="#bbb", tags="legend")
        c.create_text(w - 106, 18, text="Speed (km/h)", font=("TkDefaultFont", 8), tags="legend")
        for i in range(12):
            c.create_rectangle(w - 190 + i * 16, 26, w - 174 + i * 16, 38, fill=speed_color(i / 11), outline="", tags="legend")
        c.create_text(w - 190, 44, anchor="w", text=f"{lo:.1f}", font=("TkDefaultFont", 8), tags="legend")
        c.create_text(w - 14, 44, anchor="e", text=f"{hi:.1f}", font=("TkDefaultFont", 8), tags="legend")

    def map_hover(self, event):
        c = self.map_canvas
        c.delete("hover")
        if not self.map_geom:
            return
        xy, data = self.map_geom
        i = min(range(len(xy)), key=lambda k: (xy[k][0] - event.x) ** 2 + (xy[k][1] - event.y) ** 2)
        px, py = xy[i]
        if (px - event.x) ** 2 + (py - event.y) ** 2 > 14 ** 2:
            return                                    # the pointer is not on the track
        d = data[i]

        def f(v, fmt):
            return "-" if v is None else format(v, fmt)

        spd = d["spd"]
        pace = f" ({core.format_pace(3600 / spd)}/km)" if spd and spd > 0.3 else ""
        c.create_oval(px - 8, py - 8, px + 8, py + 8, outline="black", width=2, tags="hover")
        t = c.create_text(14, 14, anchor="nw", tags="hover", font=("TkDefaultFont", 9),
                          text=f"{int(d['t'] // 60)}:{int(d['t'] % 60):02d}   {d['km']:.3f} km   speed {f(spd, '.2f')} km/h{pace}   "
                               f"HR {f(d['hr'], '.0f')}   cadence {f(d['cad'], '.0f')}   altitude {f(d['alt_raw'], '.1f')} m")
        bx = c.bbox(t)
        box = c.create_rectangle(bx[0] - 6, bx[1] - 4, bx[2] + 6, bx[3] + 4, fill="white", outline="#999", tags="hover")
        c.tag_lower(box, t)

    def _tile_worker(self):
        while True:
            key = self.tile_req.get()
            if key is None:
                return
            # one download at a time (the tile servers ask for few connections); skip what the view has moved away from
            self.tile_queue.put((key, core.fetch_tile(*key) if key in self.map_wanted else False))

    def _tile_poll(self):
        self._tile_job = None
        got = False
        while True:
            try:
                key, data = self.tile_queue.get_nowait()
            except queue.Empty:
                break
            self.map_pending.discard(key)
            if data is False:
                continue                              # skipped, not cached: it is requested again if the view returns
            try:
                self.map_tiles[key] = tk.PhotoImage(data=base64.b64encode(data)) if data else None
            except tk.TclError:
                self.map_tiles[key] = None
            got = True
        if got:
            self.draw_map()
        if self.map_pending:
            self._tile_job = self.after(100, self._tile_poll)

    # ── fitness: advanced metrics (estimates) ────────────────────────────
    def _build_today_tab(self):
        tab = ttk.Frame(self.tabs)
        self.today_tab = tab
        self.tabs.add(tab, text="Today")
        self.fit_footer = ttk.Label(tab, text="", foreground="#666", font=("TkDefaultFont", 8), wraplength=1100, justify="left")
        self.fit_footer.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        top = ttk.Frame(tab)
        top.pack(fill="x")
        head = ttk.Frame(top)
        head.pack(fill="x", padx=10, pady=(8, 2))
        self.fit_message = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.fit_message, foreground="#b35f10").pack(side="left")
        ttk.Button(head, text="Recompute all metrics", command=self.recompute_fitness).pack(side="right")
        ttk.Label(head, text="The metrics of a run are kept in a file and recomputed only when the run changes.",
                  foreground="#666").pack(side="right", padx=10)
        self.fit_strip = ttk.Frame(top)                                   # the four cards
        self.fit_strip.pack(fill="x", padx=8)
        self.fit_best = ttk.Frame(top)                                    # the best efforts
        self.fit_best.pack(fill="x", padx=10, pady=(4, 4))
        bottom = ttk.Frame(tab)
        bottom.pack(fill="both", expand=True)
        self.today_tabs = ttk.Notebook(bottom)
        self.today_tabs.pack(fill="both", expand=True)
        self._build_chart_tab(self.today_tabs)                            # the trends over the runs
        stand = ttk.Frame(self.today_tabs)
        self.stand_tab = stand
        self.today_tabs.add(stand, text="Where you stand")
        self.now_canvas, self.now_inner = self._scroll_area(stand, on_resize=lambda w: self._gauges_resized("now", w))
        self.fit_rows, self.adv = [], {}
        self.fit_view, self.fit_panel = None, None
        self.gauge_cards, self.now_cards, self.gauge_summ = [], [], None
        self._gauge_width, self._gauge_job = {}, {}
        self.fit_queue = queue.Queue()
        self.fit_job, self.fit_thread, self.fit_id, self.fit_cancel, self.fit_threads = None, None, 0, None, []
        self.athlete = dict(rm.DEFAULT_ATHLETE)

    def _card(self, col, title, card, wide=False):
        """One card of the strip: a title, a big headline, a line under it, rows of label and value, and notes (selectable)."""
        frame = ttk.LabelFrame(self.fit_strip, text=title, padding=(10, 4, 10, 8))
        frame.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
        self.fit_strip.columnconfigure(col, weight=1, uniform="card")
        if card is None:
            ttk.Label(frame, text="Nothing to show yet.", foreground="#888").pack(anchor="w")
            return frame
        if card.get("table"):                                           # the race predictions: a small table instead of a headline
            table = ttk.Frame(frame)
            table.pack(anchor="w", fill="x")
            for c, text in enumerate(card["table"]["head"], 1):
                ttk.Label(table, text=text, foreground="#666", font=("TkDefaultFont", 8), wraplength=84, justify="right").grid(row=0, column=c, sticky="e", padx=(10, 0))
            for r, cells in enumerate(card["table"]["rows"], 1):
                ttk.Label(table, text=cells[0]).grid(row=r, column=0, sticky="w")
                for c, text in enumerate(cells[1:], 1):
                    ttk.Label(table, text=text, font=("TkDefaultFont", 10, "bold")).grid(row=r, column=c, sticky="e", padx=(10, 0))
            table.columnconfigure(0, weight=1)
        else:
            line = ttk.Frame(frame)
            line.pack(anchor="w", fill="x")
            ttk.Label(line, text=card["headline"], font=("TkDefaultFont", 17, "bold"), foreground=GAUGE_TEXT.get(card.get("tone"), "#222")).pack(side="left")
            if card.get("unit"):
                ttk.Label(line, text=" " + card["unit"], foreground="#666").pack(side="left", pady=(7, 0))
            if card.get("sub"):
                ttk.Label(frame, text=card["sub"], foreground="#555", wraplength=FIT_CARD_W, justify="left").pack(anchor="w", pady=(0, 4))
            grid = ttk.Frame(frame)
            grid.pack(anchor="w", fill="x")
            for r, (label, value) in enumerate(card.get("rows", [])):
                ttk.Label(grid, text=label, foreground="#666").grid(row=r, column=0, sticky="w")
                ttk.Label(grid, text=value, font=("TkDefaultFont", 10, "bold")).grid(row=r, column=1, sticky="e", padx=(14, 0))
            grid.columnconfigure(0, weight=1)
        if card.get("notes"):
            self._selectable(frame, "\n".join(f"{i} {t}" for i, t in card["notes"]), ("TkDefaultFont", 8), "#555", pixels=FIT_CARD_W, pady=(6, 0))
        return frame

    def _fill_dashboard(self, view):
        """The strip of cards and the best efforts, from rm.dashboard()'s data."""
        for w in list(self.fit_strip.winfo_children()) + list(self.fit_best.winfo_children()):
            w.destroy()
        view = view or {"next": None, "load": None, "vo2": None, "races": None, "best": [], "footer": ""}
        self._card(0, "NEXT SESSION", view["next"])
        self._card(1, "TRAINING STATUS", view["load"])
        self._card(2, "VO2MAX (ESTIMATE)", view["vo2"])
        races = view["races"]
        self._card(3, "RACE PREDICTIONS", {"table": races, "notes": races["notes"]} if races else None)
        if view["best"]:
            ttk.Label(self.fit_best, text="Best efforts", foreground="#666").pack(side="left", padx=(0, 10))
            for label, secs, day in view["best"]:
                chip = ttk.Frame(self.fit_best, padding=(0, 0, 14, 0))
                chip.pack(side="left")
                ttk.Label(chip, text=label + " ", foreground="#666").pack(side="left")
                ttk.Label(chip, text=secs, font=("TkDefaultFont", 10, "bold")).pack(side="left")
                ttk.Label(chip, text=f" ({day})", foreground="#888", font=("TkDefaultFont", 8)).pack(side="left")
        self.fit_footer.configure(text=view["footer"])

    def _show_fit_panel(self, panel, message="Select a run in the list.", kms=None):
        """The summary of the selected run: its facts, its numbers, the time in each zone as a bar, the kilometres, the repetitions, notes.
        Two columns when the window is wide enough, one under the other when it is not."""
        for w in self.fit_inner.winfo_children():
            w.destroy()
        self.fit_panel, self.splits, self._panel_blocks = panel, None, None
        if not panel:
            ttk.Label(self.fit_inner, text=message, foreground="#888", padding=10).pack(anchor="w")
            return
        left, right = ttk.Frame(self.fit_inner), ttk.Frame(self.fit_inner)
        self._panel_blocks = (left, right)
        top = ttk.Frame(left)
        top.pack(anchor="w", fill="x", pady=(2, 6))
        ttk.Label(top, text=panel["title"], font=("TkDefaultFont", 13, "bold")).pack(side="left")
        ttk.Label(top, text="   " + panel["subtitle"], foreground="#555").pack(side="left")
        if panel["badge"]:
            ttk.Label(top, text=" " + panel["badge"] + " ", foreground="white", background=GAUGE_FILL["info"], font=("TkDefaultFont", 8, "bold")).pack(side="left", padx=8)

        def tiles(parent, items):
            grid = ttk.Frame(parent)
            grid.pack(anchor="w", fill="x")
            for i, (label, value) in enumerate(items):
                tile = ttk.Frame(grid, padding=(0, 0, 14, 6))
                tile.grid(row=i // 3, column=i % 3, sticky="w")
                ttk.Label(tile, text=label, foreground="#777", font=("TkDefaultFont", 8)).pack(anchor="w")
                ttk.Label(tile, text=value, font=("TkDefaultFont", 11, "bold"), foreground="#222" if value != "-" else "#aaa").pack(anchor="w")

        tiles(left, panel["facts"])
        ttk.Label(left, text="Estimates from the heart rate and the speed" if panel["has_metrics"] else "The metrics of this run are being computed...",
                  foreground="#777", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(6, 2))
        tiles(left, panel["tiles"])
        if panel["has_metrics"]:
            ttk.Label(left, text="Time in each heart-rate zone", foreground="#777", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(4, 0))
            bar = tk.Canvas(left, width=FIT_PANEL_W, height=44, highlightthickness=0, background=self.cget("background"))
            bar.pack(anchor="w")
            self._draw_zone_bar(bar, panel["zones"])
        if kms:
            ttk.Label(right, text="Kilometres", foreground="#777", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(4, 2))
            self.splits = ttk.Treeview(right, columns=("km", "pace", "hr", "dalt", "note"), show="headings", height=len(kms), selectmode="none")
            for key, title, width in (("km", "km", 46), ("pace", "Pace", 64), ("hr", "HR", 50), ("dalt", "Alt (m)", 64), ("note", "", 74)):
                self.splits.heading(key, text=title)
                self.splits.column(key, width=width, anchor="center", stretch=False)
            for values in kms:
                self.splits.insert("", "end", values=values)
            self.splits.pack(anchor="w")
        if panel["reps"]:
            ttk.Label(right, text="Repetitions", foreground="#777", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(8, 2))
            cols = ("n", "start", "time", "m", "pace", "hr", "max", "drop")
            reps = ttk.Treeview(right, columns=cols, show="headings", height=len(panel["reps"]), selectmode="none")
            for key, title, width in (("n", "#", 24), ("start", "Start", 46), ("time", "Time", 46), ("m", "m", 42), ("pace", "Pace", 46),
                                      ("hr", "HR", 36), ("max", "Max", 36), ("drop", "Drop 60s", 56)):
                reps.heading(key, text=title)
                reps.column(key, width=width, anchor="center", stretch=False)
            for row in panel["reps"]:
                reps.insert("", "end", values=row)
            reps.pack(anchor="w")
        if panel["warning"]:
            self._selectable(right, panel["warning"], ("TkDefaultFont", 9, "bold"), GAUGE_TEXT["bad"], pixels=FIT_PANEL_W, pady=(8, 0))
        if panel["notes"]:
            self._selectable(right, "\n".join(f"{i} {t}" for i, t in panel["notes"]), ("TkDefaultFont", 9), "#444", pixels=FIT_PANEL_W, pady=(8, 0))
        self._layout_panel()

    def _layout_panel(self, width=None):
        """Side by side when the canvas is wide enough for both columns, else the second under the first."""
        if not self._panel_blocks:
            return
        left, right = self._panel_blocks
        width = width or self.fit_canvas.winfo_width()
        left.update_idletasks()
        right.update_idletasks()
        side = width >= left.winfo_reqwidth() + right.winfo_reqwidth() + 60
        left.grid(row=0, column=0, sticky="nw")
        right.grid(row=0 if side else 1, column=1 if side else 0, sticky="nw", padx=(30, 0) if side else 0, pady=0 if side else (10, 0))

    def _draw_zone_bar(self, cv, minutes):
        total = sum(minutes)
        if total <= 0:
            cv.create_text(4, 14, text="no heart rate", anchor="w", fill="#888")
            return
        x = 0.0
        for z, m in enumerate(minutes, 1):
            w = FIT_PANEL_W * m / total
            if w > 0:
                cv.create_rectangle(x, 4, x + w, 24, fill=ZONE_FILL[z - 1], outline="")
                if w >= 26:
                    cv.create_text(x + w / 2, 14, text=f"Z{z}", fill="white", font=("TkDefaultFont", 8, "bold"))
            cv.create_text(x + w / 2, 35, text=f"{m:.0f}'" if m >= 0.5 else "", fill="#555", font=("TkDefaultFont", 8))
            x += w

    # ── athlete parameters ───────────────────────────────────────────────
    def edit_athlete(self):
        """A small window for the parameters of athlete.json (heart rates, mass, age, sex)."""
        d = self._dir()
        if not d:
            messagebox.showinfo("Athlete", "Choose the data folder first: the parameters are saved in it.")
            return
        a = rm.read_athlete(d)
        win = tk.Toplevel(self)
        win.title("Athlete parameters")
        win.transient(self)
        win.resizable(False, False)
        fields = [("hr_max", "Maximum heart rate (bpm)"), ("hr_rest", "Resting heart rate (bpm)"), ("mass_kg", "Mass (kg)"),
                  ("age", "Age (years)"), ("height_cm", "Height (cm)")]
        vars_ = {k: tk.StringVar(value="" if a[k] is None else f"{a[k]:g}") for k, _ in fields}
        sex = tk.StringVar(value=a["sex"] or "")
        estimated = tk.BooleanVar(value=bool(a["hr_max_estimated"]))
        frame = ttk.Frame(win, padding=14)
        frame.pack()
        for i, (k, label) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(frame, textvariable=vars_[k], width=10).grid(row=i, column=1, padx=(10, 0), pady=3)
        ttk.Label(frame, text="Sex").grid(row=len(fields), column=0, sticky="w", pady=3)
        ttk.Combobox(frame, textvariable=sex, values=["M", "F"], state="readonly", width=8).grid(row=len(fields), column=1, padx=(10, 0))
        ttk.Checkbutton(frame, text="The maximum heart rate is an estimate, not measured", variable=estimated).grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frame, text="Saved in athlete.json in the data folder (private: not in the repository).\nAge and sex place the VO2max "
                  "against norms; the age does not update itself.", foreground="#555").grid(row=len(fields) + 2, column=0, columnspan=2,
                                                                                         sticky="w", pady=(8, 0))

        def save():
            try:
                values = self.apply_athlete({k: v.get() for k, v in vars_.items()}, sex.get(), estimated.get())
            except ValueError as e:
                messagebox.showerror("Athlete", str(e), parent=win)
                return
            win.destroy()
            self.status.set("Athlete parameters saved: " + ", ".join(f"{k} {v}" for k, v in values.items() if v is not None))

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Save", command=save).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))
        win.grab_set()

    def apply_athlete(self, texts, sex, hr_max_estimated):
        """Check the texts of the dialog, save them to athlete.json and recompute. Raises ValueError with a message for the user."""
        d = self._dir()
        limits = {"hr_max": (100, 230, "Maximum heart rate"), "hr_rest": (25, 120, "Resting heart rate"), "mass_kg": (30, 250, "Mass"),
                  "age": (10, 110, "Age"), "height_cm": (100, 230, "Height")}
        values = {}
        for k, (lo, hi, name) in limits.items():
            text = str(texts.get(k, "")).strip().replace(",", ".")
            if not text:
                if k in ("age", "height_cm"):
                    values[k] = None                                  # age is optional; the others keep the defaults when empty
                    continue
                raise ValueError(f"{name} is needed.")
            try:
                v = float(text)
            except ValueError:
                raise ValueError(f"{name}: '{text}' is not a number.") from None
            if not lo <= v <= hi:
                raise ValueError(f"{name} should be between {lo} and {hi}.")
            values[k] = int(v) if k in ("hr_max", "hr_rest", "age", "height_cm") else v
        if sex not in ("", "M", "F"):
            raise ValueError("Sex is M or F.")
        values["sex"] = sex or None
        values["hr_max_estimated"] = True if hr_max_estimated else None
        rm.write_athlete(d, values)
        self.start_fitness()
        return values

    # ── gauges ───────────────────────────────────────────────────────────
    def _build_gauges_tab(self):
        tab = ttk.Frame(self.run_tabs)
        self.gauge_tab = tab
        self.run_tabs.add(tab, text="Gauges")
        ttk.Label(tab, text="Coloured bands are the reference ranges; the needle is the value of the selected run. The last line of each card says how far "
                            "the range can be trusted. The gauges about today (load ratio, form, VO2max) are on the Today tab.",
                  foreground="#555", wraplength=640, justify="left").pack(anchor="w", padx=10, pady=(8, 2))
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        self.gauge_canvas, self.gauge_inner = self._scroll_area(body, on_resize=lambda w: self._gauges_resized("run", w))

    def _gauge_cards(self, inner, gauges, columns=3, avail=760):
        """One card per gauge, `columns` to a row, sized to the room: the dial, the verdict, what the value means, and where the range comes from."""
        for w in inner.winfo_children():
            w.destroy()
        columns = max(1, min(columns, int(avail // 250)))          # three to a row when the room allows it, else two or one
        for c in range(4):
            inner.columnconfigure(c, weight=0, uniform="")
        cell = min(380, int(avail / columns) - 52)                 # the width of the text and of the dial of one card
        dw = min(GAUGE_W, cell)
        dh = int(dw * GAUGE_H / GAUGE_W)
        cards = []
        for i, g in enumerate(gauges):
            card = ttk.LabelFrame(inner, text=g["title"], padding=8)
            card.grid(row=i // columns, column=i % columns, sticky="nsew", padx=6, pady=6)
            inner.columnconfigure(i % columns, weight=1, uniform="g")
            cv = tk.Canvas(card, width=dw, height=dh, highlightthickness=0, background=self.cget("background"))
            cv.pack()
            self._draw_gauge(cv, g, dw, dh)
            verdict = f"{g['label']}: {g['advice']}" if g["value"] is not None else (g["advice"] or "No data for this run.")
            self._selectable(card, verdict, ("TkDefaultFont", 10, "bold"), GAUGE_TEXT[g["tone"]] if g["value"] is not None else None, pixels=cell, pady=(4, 2))
            self._selectable(card, g["why"], ("TkDefaultFont", 10), pixels=cell, pady=2)
            self._selectable(card, f"Reference ({GAUGE_LEVEL[g['level']]}): {g['source']}", ("TkDefaultFont", 8), "#666", pixels=cell, pady=(2, 0))
            cards.append((card, g))
        return cards

    def _gauge_room(self, canvas, key):
        w = canvas.winfo_width()
        w = w if w > 200 else 760                                  # not shown yet: a usual width
        self._gauge_width[key] = w
        return w - 24

    def _gauges_resized(self, key, width):
        """The window changed width: draw the cards again to fit (once it has settled)."""
        if abs(width - self._gauge_width.get(key, 0)) < 40:
            return
        if self._gauge_job.get(key):
            self.after_cancel(self._gauge_job[key])
        self._gauge_job[key] = self.after(250, self.draw_gauges if key == "run" else self.draw_now_gauges)

    def _message(self, inner, text):
        for w in inner.winfo_children():
            w.destroy()
        ttk.Label(inner, text=text, foreground="#888", padding=20).pack(anchor="w")

    def draw_gauges(self):
        """The gauges of the selected run."""
        row = self.selected_row()
        m = self.adv.get(core.session_key(row)) if row else None
        if not m:
            self.gauge_cards = []
            self._message(self.gauge_inner, "Select a run in the list." if not row else "The metrics of this run are not ready yet.")
            return
        gauges = [g for g in rm.gauges(m, row, self.gauge_summ) if g["key"] not in NOW_GAUGES]
        self.gauge_cards = self._gauge_cards(self.gauge_inner, gauges, 3, self._gauge_room(self.gauge_canvas, "run"))

    def draw_now_gauges(self):
        """The gauges about today (the load, the form, the VO2max), on the Today tab."""
        if not self.fit_rows or not self.gauge_summ:
            self.now_cards = []
            self._message(self.now_inner, "Import some runs to see where you stand.")
            return
        m = self.fit_rows[-1]
        session = next((r for r in self.history if core.session_key(r) == (m["date"], m["time"])), None)
        gauges = [g for g in rm.gauges(m, session, self.gauge_summ) if g["key"] in NOW_GAUGES]
        self.now_cards = self._gauge_cards(self.now_inner, gauges, 3, self._gauge_room(self.now_canvas, "now"))

    def _read_only(self, box):
        """Make a Text widget readable and selectable but not editable: the typing keys are blocked, Ctrl+C and Ctrl+A and a right-click menu work."""
        def key(event):
            ctrl = event.state & 0x4
            return None if ctrl and event.keysym.lower() in ("c", "a") else "break"

        def select_all(event):
            event.widget.tag_add("sel", "1.0", "end-1c")
            return "break"

        box.bind("<Key>", key)
        box.bind("<Control-a>", select_all)
        box.bind("<Button-1>", lambda e: box.focus_set())
        box.bind("<Button-3>", lambda e: self._copy_menu(e, box))

    def _selectable(self, parent, text, font, foreground=None, pixels=430, pady=(0, 0)):
        """A block of text that can be selected and copied (Ctrl+C, or the menu of a right click) but not edited: a Text widget with the
        typing keys blocked, sized to the lines the text wraps to (a label cannot be selected)."""
        f = tkfont.Font(root=self, font=font)
        lines, line = 1, ""
        for paragraph in text.split("\n"):
            for word in paragraph.split():                  # the same greedy wrapping as the widget, measured in pixels
                trial = f"{line} {word}".strip()
                if line and f.measure(trial) > pixels * 0.95 - 8:
                    lines, line = lines + 1, word
                else:
                    line = trial
            lines, line = lines + 1, ""
        lines -= 1
        box = tk.Text(parent, width=max(10, pixels // f.measure("0")), height=max(1, lines), wrap="word", relief="flat", borderwidth=0,
                      highlightthickness=0, padx=0, pady=0, background=self.cget("background"), font=font, cursor="arrow")
        if foreground:
            box.configure(foreground=foreground)
        box.insert("1.0", text)

        self._read_only(box)
        box.pack(anchor="w", pady=pady)
        return box

    def _copy_menu(self, event, box):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda: box.event_generate("<<Copy>>"))
        menu.add_command(label="Select all", command=lambda: box.tag_add("sel", "1.0", "end-1c"))
        menu.tk_popup(event.x_root, event.y_root)

    def _draw_gauge(self, cv, g, w=GAUGE_W, h=GAUGE_H):
        """A half-circle scale with the bands as coloured arcs and a needle at the value, scaled to the w x h of the canvas."""
        k = w / GAUGE_W
        cx, cy, r, width = w / 2, h - 34 * k, 98 * k, 20 * k
        box = (cx - r, cy - r, cx + r, cy + r)
        span = float(g["hi"] - g["lo"])
        frac = lambda v: (min(max(v, g["lo"]), g["hi"]) - g["lo"]) / span
        for a, b, tone, label, _ in g["bands"]:
            fa, fb = frac(a), frac(b)
            cv.create_arc(*box, start=180 - 180 * fb, extent=180 * (fb - fa) + 0.5, style="arc", width=width, outline=GAUGE_FILL[tone])
        edges = sorted({b[0] for b in g["bands"]} | {g["hi"]})
        for v in edges:                                               # the limits between the bands, outside the arc
            ang = math.radians(180 - 180 * frac(v))
            x, y = cx + (r + width / 2 + 11 * k) * math.cos(ang), cy - (r + width / 2 + 11 * k) * math.sin(ang)
            cv.create_text(x, y, text=f"{v:.3g}", fill="#666", font=("TkDefaultFont", 7))
        if g["value"] is None:
            cv.create_text(cx, cy - 20, text="no data", fill="#888", font=("TkDefaultFont", 12))
            return
        ang = math.radians(180 - 180 * frac(g["value"]))
        tip = (cx + (r - 6 * k) * math.cos(ang), cy - (r - 6 * k) * math.sin(ang))
        cv.create_line(cx, cy, *tip, width=3, fill="#222", capstyle="round")
        cv.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill="#222", outline="")
        cv.create_text(cx, cy + 20 * k, text=g["text"], font=("TkDefaultFont", 15, "bold"))
        if g["value"] < g["lo"] or g["value"] > g["hi"]:
            cv.create_text(cx, cy + 36 * k, text="(off the scale)", fill="#888", font=("TkDefaultFont", 8))

    def _write(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def recompute_fitness(self):
        """On request: throw away the file with the per-run metrics and compute them all again."""
        d = self._dir()
        if not d:
            return
        try:
            os.remove(os.path.join(d, rm.CACHE_FILE))
        except OSError:
            pass
        self.start_fitness()

    def start_fitness(self):
        """The history changed: compute the metrics again (in a thread; the file of results makes it quick)."""
        self.fit_id += 1                                   # a result still on its way belongs to the old history
        if not self._dir() or not self.history:
            self.apply_fitness(self.fit_id, [], None)
            return
        self._run_fitness()

    def _tab_changed(self):
        self.draw_map()

    def _run_fitness(self):
        """Compute the advanced metrics in a thread (a few seconds the first time, then the cache); the result comes back through
        fit_queue. The thread must not touch the window (Tk objects dying in another thread crash), so it gets plain values."""
        d = self._dir()
        self.fit_id += 1
        self.athlete = rm.read_athlete(d)
        if self.fit_cancel:
            self.fit_cancel.set()
        cancel = self.fit_cancel = threading.Event()
        job, history, athlete, out = self.fit_id, list(self.history), dict(self.athlete), self.fit_queue
        self.fit_message.set("Computing the advanced metrics...")

        def abort(n, total):
            if cancel.is_set():
                raise InterruptedError
            out.put((job, "progress", n, total))

        def work():
            try:
                rows = rm.compute_all(d, history, athlete, progress=abort)
                out.put((job, rows, rm.summary(rows, history, athlete)))
            except InterruptedError:
                pass
            except Exception as e:                       # a failure here must not stop the program: say it in the tab
                out.put((job, None, f"Could not compute the advanced metrics: {e}"))

        self.fit_thread = threading.Thread(target=work, daemon=True)
        self.fit_threads = [t for t in self.fit_threads if t.is_alive()] + [self.fit_thread]    # a cancelled one may still hold a CSV open
        self.fit_thread.start()
        if self.fit_job is None:
            self.fit_job = self.after(150, self._fit_poll)

    def _fit_poll(self):
        self.fit_job = None
        try:
            while True:
                item = self.fit_queue.get_nowait()
                if len(item) == 4:                                   # (job, "progress", done, total)
                    if item[0] == self.fit_id and self.fit_thread is not None and self.fit_thread.is_alive():
                        self.fit_message.set(f"Computing the advanced metrics... {item[2]} of {item[3]} runs")
                    continue
                job, rows, summ = item
                if job == self.fit_id:
                    self.apply_fitness(job, rows, summ)
        except queue.Empty:
            pass
        if self.fit_thread is not None and self.fit_thread.is_alive():
            self.fit_job = self.after(150, self._fit_poll)

    def fitness_now(self):
        """Synchronous version (used by the tests)."""
        d = self._dir()
        self.fit_id += 1
        self.athlete = rm.read_athlete(d) if d else dict(rm.DEFAULT_ATHLETE)
        rows = rm.compute_all(d, self.history, self.athlete) if d else []
        self.apply_fitness(self.fit_id, rows, rm.summary(rows, self.history, self.athlete) if rows else None)

    def apply_fitness(self, job, rows, summ):
        """The metrics are ready: the cards of the Today tab, the metric columns of the list, and the views of the selected run."""
        self.gauge_summ = summ if isinstance(summ, dict) else None
        self.fit_view = None
        if rows is None:
            self.fit_rows, self.adv = [], {}
            self.fit_message.set(summ)
        else:
            self.fit_rows = rows
            self.adv = {(m["date"], m["time"]): m for m in rows}
            d = self._dir()
            if not summ:
                self.fit_message.set("Import some runs to see the advanced metrics.")
            elif d and not os.path.exists(os.path.join(d, rm.ATHLETE_FILE)):
                self.fit_message.set(f"No {rm.ATHLETE_FILE} in the data folder: default parameters. Set yours with the Athlete button.")
            else:
                self.fit_message.set("")
            if summ:
                self.fit_view = rm.dashboard(rm.next_session(self.history, rows, summ), summ)
        self._fill_dashboard(self.fit_view)
        self.draw_now_gauges()
        self.refresh_cells()
        self.show_run_summary()
        self.draw_gauges()
        self.draw_chart()

    def show_run_summary(self):
        """The summary of the selected run, from its history row, its metrics (when they are computed) and what reading its CSV added."""
        row = self.selected_row()
        if not row:
            self._show_fit_panel(None, "Select a run in the list.")
            return
        self._show_fit_panel(rm.run_panel(self.adv.get(core.session_key(row)), row, self.run_extra), kms=self.run_kms)

    def _metric_value(self, row, key):
        if key.startswith("adv."):
            return self.adv.get(core.session_key(row), {}).get(key[4:])
        return row.get(key, "")

    def selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((r for r in self.history if "|".join(core.session_key(r)) == sel[0]), None)

    # ── detail ───────────────────────────────────────────────────────────
    def show_detail(self):
        self.run = None
        row = self.selected_row()
        if row:
            self.last_run = "|".join(core.session_key(row))
        self.run_title.set(f"{row['date']} {row['time']}   {row['dist_km']} km" if row else "")
        self.map_tiles = {k: v for k, v in self.map_tiles.items() if v}      # retry the tiles that failed (offline before)
        self.map_view = None                                                  # a new run: fit its track
        try:
            self._show_detail(row)
        finally:
            self.show_run_summary()
            self.draw_gauges()
            self.draw_run()
            self.draw_map()

    def _show_detail(self, row):
        """Read the CSV of the run: its points (for the chart and the map), its kilometres and what the summary adds to the history row."""
        self.run_kms, self.run_extra = [], {}
        if not row:
            return
        notes, d = [], self._dir()
        path = core.session_file(d, row) if d else None
        if path and core.merged_members(d).get(os.path.basename(path)):
            notes.append(("\u2022", f"Merged from {1 + len(core.merged_members(d)[os.path.basename(path)])} recordings (right-click in the list: Unmerge runs)."))
        alt = None
        if not path:
            notes.append(("\u26a0", "Run CSV not found in the folder: per-km detail not available."))
        else:
            try:
                res = core.analyze_file(path, fetch=False)
            except ValueError as e:
                notes.append(("\u26a0", f"Cannot read the CSV: {e}"))
            else:
                self.run = (row, res["points"])
                if res["alt_min"] is not None:
                    alt = (res["alt_min"], res["alt_max"])
                if res["is_sprint"]:
                    sk = res["splits"][res["sprint_idx"]]
                    notes.append(("\u26a1", f"Final sprint (km {sk['km']}, {core.format_pace(sk['pace_s'])}/km): excluded from the drift."))
                for w in core.weather_warnings({"temp_c": core.safe_float(row.get("temp_c")) or 0,
                                                "humidity_pct": core.safe_float(row.get("humidity_pct")) or 0}):
                    notes.append(("\u26a0", w))
                self.run_kms = [(sp["km"], core.format_pace(sp["pace_s"]), f"{sp['avg_hr']:.0f}" if sp.get("avg_hr") else "\u2014",
                                 f"{sp['alt_delta']:+.1f}" if sp["alt_delta"] is not None else "\u2014",
                                 "\u26a1 sprint" if res["is_sprint"] and k == res["sprint_idx"] else "") for k, sp in enumerate(res["splits"])]
        self.run_extra = {"alt": alt, "notes": notes}

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
            v = parse(self._metric_value(r, key))
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

    # ── import ───────────────────────────────────────────────────────────
    def set_busy(self, busy, msg=""):
        self.busy = busy
        for b in (self.btn_import, self.btn_all, self.btn_download):
            b.state(["disabled"] if busy or not self._dir() else ["!disabled"])
        if msg:
            self.status.set(msg)

    def start_import(self, reprocess=False, download=False):
        d = self._dir()
        if not d or self.busy:
            return
        self.set_busy(True, "Downloading new sessions from intervals.icu…" if download else "Analysing the CSVs…")
        fetch = self.fetch.get()

        def work():
            try:
                got = core.download_new_runs(d) if download else None
                res = core.import_sessions(d, fetch, reprocess)
                res["download"] = got
                self.queue.put(("done", res))
            except Exception as e:  # reported on the UI thread
                self.queue.put(("error", e))

        threading.Thread(target=work, daemon=True).start()

    def reprocess_all(self):
        if messagebox.askyesno("Reprocess all",
                               "Recompute every run from the CSVs and overwrite the metrics in the history "
                               "(weather already recorded is kept). Continue?"):
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
        self._poll_job = self.after(150, self._poll)

    def finish_import(self, result):
        self.set_busy(False)
        sessions, errors = result["sessions"], result["errors"]
        history = core.merge_sessions(result["history"], sessions)
        self.history = history
        self.save_history()
        self.last_run = None                               # after an import the newest run is the one to look at
        self.refresh_table()
        self.draw_chart()
        self.start_fitness()
        got = result.get("download")
        msg = f"{len(sessions)} sessions imported, {result['skipped']} already present"
        if got:
            msg = f"{len(got['downloaded'])} runs downloaded, " + msg
        if got and got["errors"]:
            errors = errors + got["errors"]
        if errors:
            msg += f", {len(errors)} unreadable files"
        self.status.set(msg + ".")
        if errors:
            messagebox.showwarning("Files not imported", "\n".join(f"{f}: {m}" for f, m in errors))
        if sessions:
            self.tabs.select(self.today_tab)

    # ── misc ─────────────────────────────────────────────────────────────
    def _on_error(self, exc, val, tb):
        messagebox.showerror("Error", f"{exc.__name__}: {val}")

    def _save_config(self):
        save_config(self.config_path, {"data_dir": self.data_dir.get(), "fetch_weather": self.fetch.get(), "fit_dir": self.fit_dir.get(),
                                       "columns": self.columns_shown(), "last_run": self.last_run})

    def _close(self):
        self._save_config()
        self.after_cancel(self._poll_job)   # a pending timer would fire on a destroyed window
        if self.fit_cancel:
            self.fit_cancel.set()           # the metrics thread stops at its next file; wait for it (it holds the CSVs open)
        if self.fit_job is not None:
            self.after_cancel(self.fit_job)
        for t in self.fit_threads:
            t.join(timeout=30)
        if self._tile_job is not None:
            self.after_cancel(self._tile_job)
        self.tile_req.put(None)                       # stops the tile worker
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
