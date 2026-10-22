"""
parse_run_csv.py - the logic behind the running-coach GUI (running-coach-gui.pyw): reading the intervals.icu CSV exports of runs
recorded with Samsung Health, per-km splits, session metrics (EF, cardiac drift, TSS), sprint detection, Open-Meteo weather, the
history file, merged outings, the intervals.icu download and the next-session rules. There is no command line any more.
"""

import csv, os, math, re, json, base64
import urllib.request
from datetime import datetime, timedelta

# ─── Constants ────────────────────────────────────────────────────────────────
THRESHOLD_HR    = 170    # lactate threshold HR (bpm); matches the original TSS values
EF_BASELINE     = 0.92   # m/beat baseline (good aerobic efficiency)
SPRINT_PACE_S   = 15     # sec/km faster than median to flag sprint
SPRINT_HR_BPM   = 5      # bpm above median to confirm sprint
# Drift thresholds (%) calibrated for drift measured without warm-up
DRIFT_LOW       = 3      # below: good aerobic coupling
DRIFT_HIGH      = 6      # above: fatigue/heat flag, +1 rest day
DRIFT_VERY_HIGH = 8      # above: recovery-length next run
DRIFT_PROGRESS  = 4      # below (with improving EF): distance can progress
DRIFT_INTENSIFY = 3      # below (with improving EF): pace can drop slightly
WARMUP_S        = 600    # warm-up excluded from cardiac drift (capped at 25% of the run)
FETCH_WEATHER   = True   # False: no coordinates sent to Open-Meteo (the GUI box overrides it)
HISTORY_FILE    = "running_history.csv"
# Column names written by earlier (Italian) versions of the history file
LEGACY_FIELDS   = {
    "data": "date", "orario": "time", "durata_hms": "duration_hms", "passo_minkm": "pace_minkm",
    "fc_media_bpm": "avg_hr_bpm", "ef_m_batt": "ef_m_beat", "umidita_pct": "humidity_pct",
    "vento_kmh": "wind_kmh",
}
HISTORY_FIELDS  = [
    "date", "time", "dist_km", "duration_hms", "pace_minkm",
    "avg_hr_bpm", "ef_m_beat", "drift_pct", "tss",
    "temp_c", "humidity_pct", "wind_kmh", "kind",
]                                        # kind: "intervals" for an interval workout, else empty

# ─── Utilities ────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_pace(sec_per_km):
    if not sec_per_km or sec_per_km <= 0 or sec_per_km > 1200:
        return "--:--"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}"


def hms_csv(seconds):
    h, rem = divmod(int(seconds), 3600)
    return f"{h:02d}:{rem // 60:02d}:{rem % 60:02d}"


def pace_csv(sec_per_km):
    m, sec = divmod(int(round(sec_per_km)), 60)
    return f"{m:02d}:{sec:02d}"


def parse_pace(v):
    """'07:27' -> 447.0; a plain number is taken as seconds/km."""
    v = (v or "").strip() if isinstance(v, str) else v
    if isinstance(v, str) and ":" in v:
        m, _, sec = v.partition(":")
        try:
            return int(m) * 60 + float(sec)
        except ValueError:
            return None
    return safe_float(v)


def date_iso(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def date_dmy(s):
    iso = date_iso(s)
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return s


def safe_float(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_ts(s):
    if not s:
        return None
    s = s.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",  "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",  "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def first_key(row, *candidates):
    for c in candidates:
        if c in row:
            return c
    return None

# ─── CSV Loading ──────────────────────────────────────────────────────────────

def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return [{k.lower().strip(): (v or "").strip() for k, v in row.items()} for row in rows]


def pick_col(rows, *candidates):
    """First candidate column that exists AND holds at least one value."""
    for c in candidates:
        if c in rows[0] and any(r.get(c) for r in rows):
            return c
    return None


def filename_start(path):
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2}) (\d{2})\.(\d{2})", os.path.basename(path))
    return datetime(*map(int, m.groups())) if m else None


NUMBER = re.compile(r"-?\d+(\.\d+)?$")


def extract_points(rows, start=None):
    """`time` may be an ISO timestamp or elapsed seconds (intervals.icu); for the latter
    the start datetime comes from `start` (the filename)."""
    if not rows:
        return []
    ts_col   = pick_col(rows, "time", "timestamp", "datetime", "date")
    lat_col  = pick_col(rows, "lat", "latitude")
    lon_col  = pick_col(rows, "lng", "lon", "longitude")
    alt_col  = pick_col(rows, "altitude", "alt", "elevation", "fixed_altitude")
    hr_col   = pick_col(rows, "heartrate", "heart_rate", "hr", "bpm")
    dist_col = pick_col(rows, "distance", "dist", "cum_distance", "cumulative_distance")
    spd_col  = pick_col(rows, "velocity_smooth", "speed", "enhanced_speed")
    raw_alt_col = pick_col(rows, "altitude", "alt", "elevation")
    base = start or datetime(2000, 1, 1)

    pts = []
    for row in rows:
        raw = row.get(ts_col, "") if ts_col else ""
        ts = None if NUMBER.match(raw) else parse_ts(raw)      # elapsed seconds are most of the rows: do not try six date formats on each
        if ts is None:
            secs = safe_float(raw)
            if secs is None:
                continue
            ts = base + timedelta(seconds=secs)
        pts.append({
            "ts":   ts,
            "lat":  safe_float(row.get(lat_col,  "")) if lat_col  else None,
            "lon":  safe_float(row.get(lon_col,  "")) if lon_col  else None,
            "alt":  safe_float(row.get(alt_col,  "")) if alt_col  else None,
            "hr":   safe_float(row.get(hr_col,   "")) if hr_col   else None,
            "dist_raw": safe_float(row.get(dist_col, "")) if dist_col else None,
            "speed": safe_float(row.get(spd_col, "")) if spd_col else None,    # m/s, as saved in the CSV
            "cadence": safe_float(row.get("cadence", "")),
            "alt_raw": safe_float(row.get(raw_alt_col, "")) if raw_alt_col else None,
            "alt_fixed": safe_float(row.get("fixed_altitude", "")),
        })
    return sorted(pts, key=lambda p: p["ts"])


# ─── Heart rate from the original FIT file ───────────────────────────────────
# The intervals.icu CSV of a run can hold a heart rate clipped at a ceiling (intervals.icu cuts a heart rate above the maximum set for the sport, while the watch and the
# FIT/TCX/GPX files written by Health Sync have the real values). When FIT_DIR points to the folder with those FIT files, the heart
# rate of a run is read from the FIT of the same name instead of the CSV. Needs the optional fitparse package.

FIT_DIR = os.environ.get("RUNNING_COACH_FIT") or None
FIT_MIN_AT_MAX = 20         # seconds exactly at the run's maximum in the CSV from which it looks clipped; with fewer the FIT file is not even read
FIT_MAX_MISMATCH = 3.0      # bpm: the mean difference between the CSV and the FIT heart rate (where the CSV is not clipped) above which the FIT is not the same run


def fit_path(csv_path):
    if not FIT_DIR:
        return None
    path = os.path.join(FIT_DIR, os.path.splitext(os.path.basename(csv_path))[0] + ".fit")
    return path if os.path.isfile(path) else None


def read_fit_hr(path):
    """{second since the first record: mean heart rate of that second} from a FIT file (two records per second are usual), or {}
    if fitparse is not installed or the file is unreadable."""
    try:
        import fitparse
    except ImportError:
        return {}
    try:
        recs = [m.get_values() for m in fitparse.FitFile(path).get_messages("record")]
    except Exception:           # fitparse raises its own error types and struct errors on a damaged file
        return {}
    recs = [r for r in recs if r.get("timestamp") is not None]
    if not recs:
        return {}
    t0 = min(r["timestamp"] for r in recs)
    bins = {}
    for r in recs:
        if r.get("heart_rate") is not None:
            bins.setdefault(int((r["timestamp"] - t0).total_seconds()), []).append(r["heart_rate"])
    return {k: sum(v) / len(v) for k, v in bins.items()}


def apply_fit_hr(pts, csv_path):
    """Replace the heart rate of the points with the FIT file's, second by second, when the CSV looks clipped (FIT_MIN_AT_MAX seconds or more
    exactly at its maximum) and the FIT belongs to the same run (its heart rate agrees with the CSV's where the CSV is below its own maximum).
    Returns True if the FIT was used."""
    start = filename_start(csv_path)
    fp = fit_path(csv_path)
    if not fp or start is None:
        return False
    top = max((p["hr"] for p in pts if p["hr"] is not None), default=None)
    if top is None or sum(1 for p in pts if p["hr"] == top) < FIT_MIN_AT_MAX:
        return False                                  # a clipped recording sits at its ceiling for many seconds: reading a FIT costs 0.4 s, so only then
    fit = read_fit_hr(fp)
    if not fit:
        return False
    diffs = []
    for p in pts:
        k = int(round((p["ts"] - start).total_seconds()))
        if p["hr"] is not None and p["hr"] < top and k in fit:
            diffs.append(abs(p["hr"] - fit[k]))
    if len(diffs) >= 30 and sum(diffs) / len(diffs) > FIT_MAX_MISMATCH:
        return False
    for p in pts:
        k = int(round((p["ts"] - start).total_seconds()))
        if k in fit:
            p["hr"] = fit[k]
    return True

# ─── Distance (GPS + pre-GPS estimation) ─────────────────────────────────────

def distances_from_field(pts):
    """Use the file's cumulative `distance` column (forward-filled, monotonic).
    Returns False if the column is missing or unusable."""
    raw = [p["dist_raw"] for p in pts if p["dist_raw"] is not None]
    if len(raw) < 10 or max(raw) <= 0:
        return False
    cum = 0.0
    for p in pts:
        if p["dist_raw"] is not None:
            cum = max(cum, p["dist_raw"])
        p["dist_m"] = cum
    for i, p in enumerate(pts):
        p["delta_m"] = max(0.0, p["dist_m"] - (pts[i - 1]["dist_m"] if i else 0))
    return True


def compute_distances(pts):
    """
    Cumulative dist_m per point. Primary source: the `distance` column. Fallback (no usable
    column): haversine GPS, estimating the pre-GPS lockup period from the average GPS pace.
    """
    if distances_from_field(pts):
        return pts
    first_gps = next((i for i, p in enumerate(pts)
                      if p["lat"] is not None and p["lon"] is not None), None)

    if first_gps is None:
        # No GPS and no usable distance column: use whatever raw values exist
        cum = 0.0
        for i, p in enumerate(pts):
            raw = p["dist_raw"]
            p["dist_m"] = raw if raw is not None else cum
            p["delta_m"] = max(0.0, p["dist_m"] - (pts[i-1]["dist_m"] if i else 0))
            cum = p["dist_m"]
        return pts

    # Phase 1: GPS segment cumulative distances
    gps_pts = pts[first_gps:]
    cum = 0.0
    prev_lat = prev_lon = None
    for p in gps_pts:
        if prev_lat is not None and p["lat"] is not None:
            d = haversine(prev_lat, prev_lon, p["lat"], p["lon"])
        else:
            d = 0.0
        p["_gps_delta"] = d
        cum += d
        p["dist_m"] = cum
        if p["lat"] is not None:
            prev_lat, prev_lon = p["lat"], p["lon"]

    total_gps_m  = cum
    gps_duration = (gps_pts[-1]["ts"] - gps_pts[0]["ts"]).total_seconds()

    # Phase 2: estimate pre-GPS distance
    offset = 0.0
    if first_gps > 0 and total_gps_m > 0 and gps_duration > 0:
        avg_speed = total_gps_m / gps_duration  # m/s
        pre_dur   = (pts[first_gps]["ts"] - pts[0]["ts"]).total_seconds()
        pre_dist  = avg_speed * pre_dur
        offset    = pre_dist

        for p in pts[:first_gps]:
            elapsed  = (p["ts"] - pts[0]["ts"]).total_seconds()
            frac     = elapsed / pre_dur if pre_dur > 0 else 0.0
            p["dist_m"] = frac * pre_dist

        # Shift GPS segment
        for p in gps_pts:
            p["dist_m"] += offset

    # Fill delta_m
    for i, p in enumerate(pts):
        p["delta_m"] = max(0.0, p["dist_m"] - (pts[i-1]["dist_m"] if i else 0))

    return pts

# ─── Per-km splits ────────────────────────────────────────────────────────────

def km_splits(pts):
    """
    Returns list of dicts:
      { km, pace_s (sec/km), avg_hr, alt_start, alt_end, alt_delta (m, as read from the CSV, None
        without altitude), partial (bool), partial_dist_m }
    The altitude is not filtered or summed into an "ascent": the change over a km is end minus start.
    """
    if not pts:
        return []

    splits  = []
    km_num  = 1
    t0      = pts[0]["ts"]
    hr_sum  = hr_n = 0
    start_alt = last_alt = None

    def delta():
        return None if start_alt is None or last_alt is None else last_alt - start_alt

    for p in pts:
        d = p.get("dist_m", 0)

        if p["hr"] is not None:
            hr_sum += p["hr"]
            hr_n   += 1

        if p["alt"] is not None:
            last_alt = p["alt"]
            if start_alt is None:
                start_alt = last_alt

        if d >= km_num * 1000:
            dur = (p["ts"] - t0).total_seconds()
            splits.append({
                "km": km_num, "pace_s": dur, "dur_s": dur,
                "avg_hr": hr_sum / hr_n if hr_n else None,
                "alt_start": start_alt, "alt_end": last_alt, "alt_delta": delta(), "partial": False,
            })
            km_num   += 1
            t0        = p["ts"]
            hr_sum    = hr_n = 0
            start_alt = last_alt

    # Partial last km (> 100 m)
    last_d = pts[-1].get("dist_m", 0) - (km_num - 1) * 1000
    if last_d > 100:
        dur = (pts[-1]["ts"] - t0).total_seconds()
        pace = dur / (last_d / 1000)
        splits.append({
            "km": f"{km_num}*", "pace_s": pace, "dur_s": dur,
            "avg_hr": hr_sum / hr_n if hr_n else None,
            "alt_start": start_alt, "alt_end": last_alt, "alt_delta": delta(), "partial": True, "partial_dist_m": last_d,
        })

    return splits

# ─── Interval workouts ───────────────────────────────────────────────────────

INTERVAL_MIN_REP_S  = 30     # a fast stretch shorter than this is a stride, not a repetition
INTERVAL_MIN_GAP_S  = 20     # slower stretches shorter than this (a stumble, a corner) do not split a repetition
INTERVAL_CONTRAST   = 1.4    # fast over slow moving speed (90th over 10th percentile) needed to speak of repetitions
INTERVAL_MAX_SHARE  = 0.7    # repetitions that take more of the run than this are a run with walking breaks, not a workout
INTERVAL_MOVING_MS  = 1.0    # m/s: below this the athlete is standing or walking very slowly
INTERVAL_MIN_RECOVERY_S = 30   # the recovery between two repetitions lasts at least this
INTERVAL_MAX_RECOVERY_S = 600  # and at most this: a longer slow stretch is the body of a run with a fast start and a fast finish
INTERVAL_RECOVERY_RATIO = 0.75   # and is at most this fraction as fast as the slower of the two repetitions around it


def point_speeds(pts):
    """m/s of every point: the speed in the file, else the distance covered since the previous point over the time step."""
    out = []
    for i, p in enumerate(pts):
        v = p.get("speed")
        if v is None and i:
            dt = (p["ts"] - pts[i - 1]["ts"]).total_seconds()
            v = (p["dist_m"] - pts[i - 1]["dist_m"]) / dt if dt > 0 else 0.0
        out.append(v or 0.0)
    return out


def detect_intervals(pts):
    """The repetitions of an interval workout, [{start, end, dur, dist, speed, hr, hr_max}] (seconds from the start), or [] for any other run.
    A run has repetitions when its moving speed has two clearly different levels (90th percentile at least INTERVAL_CONTRAST times the 10th),
    and the speed (smoothed over about 10 s) goes above the midpoint in at least two separate stretches of at least INTERVAL_MIN_REP_S,
    each pair of neighbouring ones with a recovery in between that is clearly slower (INTERVAL_RECOVERY_RATIO) and neither too short nor too long, and which
    together take no more than INTERVAL_MAX_SHARE of the run. A heuristic: a run with a long walking break can still look like one."""
    n = len(pts)
    if n < 120:
        return []
    t0 = pts[0]["ts"]
    t = [(p["ts"] - t0).total_seconds() for p in pts]
    v = point_speeds(pts)
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    sm = [(pre[min(n, i + 6)] - pre[max(0, i - 5)]) / (min(n, i + 6) - max(0, i - 5)) for i in range(n)]
    moving = sorted(x for x in sm if x >= INTERVAL_MOVING_MS)
    if len(moving) < 120:
        return []
    lo, hi = moving[int(0.1 * len(moving))], moving[int(0.9 * len(moving))]
    if hi < INTERVAL_CONTRAST * lo:
        return []
    limit = (lo + hi) / 2
    stretches, i = [], 0
    while i < n:
        if sm[i] > limit:
            j = i
            while j + 1 < n and sm[j + 1] > limit:
                j += 1
            if stretches and t[i] - t[stretches[-1][1]] < INTERVAL_MIN_GAP_S:
                stretches[-1][1] = j
            else:
                stretches.append([i, j])
            i = j + 1
        else:
            i += 1
    reps = [r for r in stretches if t[r[1]] - t[r[0]] >= INTERVAL_MIN_REP_S]
    mean_v = lambda a, b: sum(v[a:b + 1]) / (b - a + 1)
    linked = set()
    for k in range(len(reps) - 1):
        (a1, b1), (a2, b2) = reps[k], reps[k + 1]
        slower = min(mean_v(a1, b1), mean_v(a2, b2))
        if INTERVAL_MIN_RECOVERY_S <= t[a2] - t[b1] <= INTERVAL_MAX_RECOVERY_S and mean_v(b1, a2) <= INTERVAL_RECOVERY_RATIO * slower:
            linked.update((k, k + 1))
    reps = [r for k, r in enumerate(reps) if k in linked]
    if len(reps) < 2 or sum(t[b] - t[a] for a, b in reps) > INTERVAL_MAX_SHARE * (t[-1] - t[0]):
        return []
    out = []
    for a, b in reps:
        dur = t[b] - t[a]
        dist = pts[b]["dist_m"] - pts[a]["dist_m"]
        hrs = [p["hr"] for p in pts[a:b + 1] if p["hr"] is not None]
        out.append({"start": t[a], "end": t[b], "dur": dur, "dist": dist, "speed": dist / dur if dur > 0 else None,
                    "hr": sum(hrs) / len(hrs) if hrs else None, "hr_max": max(hrs) if hrs else None})
    return out


# ─── Sprint detection ─────────────────────────────────────────────────────────

def detect_sprint(splits):
    """
    Auto-detect a final-km sprint: last FULL km is ≥ SPRINT_PACE_S sec/km
    faster AND ≥ SPRINT_HR_BPM bpm higher than the median of other full kms.
    Returns (is_sprint: bool, sprint_km_index: int|None).
    """
    full = [s for s in splits
            if not s.get("partial") or s.get("partial_dist_m", 0) >= 500]
    if len(full) < 2:
        return False, None

    last   = full[-1]
    others = full[:-1]

    paces = sorted(s["pace_s"] for s in others)
    median_pace = paces[len(paces) // 2]

    hrs    = [s["avg_hr"] for s in others if s.get("avg_hr")]
    median_hr = sorted(hrs)[len(hrs) // 2] if hrs else 0

    pace_delta = median_pace - last["pace_s"]          # positive = faster
    hr_delta   = (last.get("avg_hr") or 0) - median_hr  # positive = higher

    is_sprint = pace_delta >= SPRINT_PACE_S and hr_delta >= SPRINT_HR_BPM
    # Return index in full[] (corresponds to position in splits)
    sprint_idx = splits.index(full[-1]) if is_sprint else None
    return is_sprint, sprint_idx

# ─── Cardiac drift ────────────────────────────────────────────────────────────

def cardiac_drift(pts, exclude_tail_s=0, warmup_s=None):
    """
    HR drift = (avg_hr second half – avg_hr first half) / first_half × 100, computed on the
    steady part of the run: the first `warmup_s` seconds (HR still rising from rest) and the
    last `exclude_tail_s` seconds (sprint segment) are left out.
    """
    if warmup_s is None:
        warmup_s = WARMUP_S
    t0 = pts[0]["ts"]
    total = (pts[-1]["ts"] - t0).total_seconds()
    start = t0 + timedelta(seconds=min(warmup_s, 0.25 * total))
    end = pts[-1]["ts"] - timedelta(seconds=exclude_tail_s)
    work = [p for p in pts if start <= p["ts"] <= end]

    hr_pts = [p["hr"] for p in work if p["hr"] is not None]
    if len(hr_pts) < 10:
        return None

    mid = len(hr_pts) // 2
    h1  = sum(hr_pts[:mid]) / mid
    h2  = sum(hr_pts[mid:]) / (len(hr_pts) - mid)
    return round((h2 - h1) / h1 * 100, 2)

# ─── TSS and EF ──────────────────────────────────────────────────────────────

def compute_tss(dur_s, avg_hr):
    """TSS = (duration_h × IF²) × 100  where IF = avg_hr / threshold_hr."""
    if not avg_hr or avg_hr <= 0:
        return None
    IF = avg_hr / THRESHOLD_HR
    return round((dur_s / 3600) * IF**2 * 100, 1)


def tss_from_points(pts):
    """TSS second by second, for a run whose heart rate is not steady (an interval workout): the square of the intensity factor is averaged
    over the seconds instead of squaring the average heart rate, which would count the recoveries as if they were as easy as a jog."""
    total = 0.0
    for prev, p in zip(pts, pts[1:]):
        if p["hr"] is not None:
            total += (p["hr"] / THRESHOLD_HR) ** 2 * min(5.0, max(0.0, (p["ts"] - prev["ts"]).total_seconds()))
    return round(total / 3600 * 100, 1)


def compute_ef(dist_km, dur_s, avg_hr):
    """
    EF = dist_m / (avg_hr × duration_min)   [m/beat proxy]
    Typical range 0.85–1.10; baseline ~0.92
    """
    if not avg_hr or avg_hr <= 0 or dur_s <= 0:
        return None
    return round((dist_km * 1000) / (avg_hr * (dur_s / 60)), 3)

# ─── Weather ─────────────────────────────────────────────────────────────────

def fetch_weather(lat, lon, dt):
    """Open-Meteo historical weather. Returns dict or None (silent fail)."""
    try:
        import urllib.request, json
        date = dt.strftime("%Y-%m-%d")
        url  = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            f"&start_date={date}&end_date={date}"
            f"&hourly=temperature_2m,relativehumidity_2m,windspeed_10m"
            f"&timezone=auto"
        )
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        h = dt.hour
        return {
            "temp_c":      data["hourly"]["temperature_2m"][h],
            "humidity_pct": data["hourly"]["relativehumidity_2m"][h],
            "wind_kmh":   data["hourly"]["windspeed_10m"][h],
        }
    except Exception:
        return None

# ─── History I/O ─────────────────────────────────────────────────────────────

def session_key(r):
    return (date_iso(r.get("date", "")), (r.get("time") or "").strip())


def upsert(records, rec):
    """Deduplicate by (date, time) across date formats; the new record wins, but fields it
    leaves empty (weather) keep the previously recorded value."""
    for i, r in enumerate(records):
        if session_key(r) == session_key(rec):
            blank_on_purpose = {"kind"} | ({"drift_pct", "ef_m_beat"} if rec.get("kind") == "intervals" else set())   # not defined for repetitions
            for k in HISTORY_FIELDS:
                if rec.get(k) in ("", None) and k not in blank_on_purpose:
                    rec[k] = r.get(k, "")
            records[i] = rec
            return records
    records.append(rec)
    return records


def normalize_row(r):
    r = {k: (r.get(k) or "").strip() for k in HISTORY_FIELDS}
    r["date"] = date_dmy(r["date"])
    if r["duration_hms"].count(":") == 1:
        r["duration_hms"] = "00:" + r["duration_hms"]
    pace = parse_pace(r["pace_minkm"])
    if pace:
        r["pace_minkm"] = pace_csv(pace)
    return r


def migrate_legacy_row(r):
    """Rows written by earlier versions had Italian column names (data, orario, ...); they are read as
    the English ones and written back that way on the next save. The old subjective `legs` / `gambe`
    column is no longer in HISTORY_FIELDS, so it is dropped on the next save."""
    return {LEGACY_FIELDS.get(k, k): v for k, v in r.items() if k is not None}


def history_path(data_dir):
    return os.path.join(data_dir, "running_history.csv")


def load_history(path=None):
    """Read the history in any mix of date formats; collapse duplicate sessions (the original
    dd/mm/yyyy row wins; ISO-dated rows from earlier script versions only fill its empty
    fields) and sort chronologically."""
    path = path or HISTORY_FILE
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = [migrate_legacy_row(r) for r in csv.DictReader(f)]
    raw.sort(key=lambda r: not re.match(r"\d{4}-\d{2}-\d{2}", (r.get("date") or "").strip()))
    out = []
    for r in raw:
        upsert(out, normalize_row(r))
    return sorted(out, key=session_key)


def save_history(records, path=None):
    with open(path or HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)


def merge_sessions(history, sessions):
    """Merge new session dicts into the history (new rows win) and return it sorted."""
    for sess in sorted(sessions, key=session_key):
        history = upsert(history, sess)
    return sorted(history, key=session_key)

# ─── Recommendations ─────────────────────────────────────────────────────────

def compute_recommendations(history, session):
    """Next-session advice: {rest, dist_km, pace_s, ef_trend, notes: [(icon, text)]}."""
    hist = sorted(history, key=session_key)
    steady = [r for r in hist if r.get("kind") != "intervals"]
    recent = steady[-4:]
    interval = session.get("kind") == "intervals"
    base = steady[-1] if interval and steady else session          # after repetitions the pace and the distance come from the last steady run

    ef_vals = [v for v in (safe_float(r.get("ef_m_beat")) for r in recent) if v]
    ef_trend = None
    if len(ef_vals) >= 2:
        ef_trend = "improving" if ef_vals[-1] > ef_vals[0] else "declining"

    drift = None if interval else safe_float(session.get("drift_pct"))
    tss   = safe_float(session.get("tss")) or 0
    dist  = safe_float(base.get("dist_km")) or 5.0
    pace  = base.get("_pace_s") or parse_pace(base.get("pace_minkm")) or 390

    rest = 1
    if interval: rest += 1                 # a hard session: one more day before the next
    if tss > 80:  rest += 1
    if tss > 120: rest += 1
    if drift is not None and drift > DRIFT_HIGH: rest += 1
    rest = min(rest, 4)

    dist_rec = dist
    if interval:
        pass                               # no progression after a hard session
    elif ef_trend == "improving" and (drift is None or drift < DRIFT_PROGRESS):
        dist_rec = round(dist * 1.08, 1)   # +8% load progression
    elif drift is not None and drift > DRIFT_VERY_HIGH:
        dist_rec = round(dist * 0.85, 1)   # recovery run

    pace_rec = pace
    if not interval and ef_trend == "improving" and (drift is None or drift < DRIFT_INTENSIFY):
        pace_rec -= 5

    notes = []
    if interval:
        notes.append(("•", "Last session was an interval workout: one more rest day, then an easy run at the pace and distance of your last steady run"))
    if drift is not None and drift > DRIFT_HIGH:
        notes.append(("⚠", f"High drift ({drift:.1f}%) → accumulated fatigue"))
    elif drift is not None and drift < DRIFT_LOW:
        notes.append(("✓", f"Low drift ({drift:.1f}%) → excellent aerobic form"))
    return {"rest": rest, "dist_km": dist_rec, "pace_s": pace_rec, "base_pace_s": pace, "ef_trend": ef_trend, "notes": notes}


# ─── Single-file analysis ─────────────────────────────────────────────────────

def weather_warnings(w):
    out = []
    if w["humidity_pct"] > 75:
        out.append("Humidity >75%: thermoregulation impaired")
    if w["temp_c"] > 22:
        out.append(f"Heat ({w['temp_c']:.0f}°C): expect a slowdown of ~{int((w['temp_c']-18)*12)} sec/km")
    return out


def file_points(filepath):
    """Points of one CSV with cumulative distances; raises ValueError if unusable."""
    rows = load_csv(filepath)
    if not rows:
        raise ValueError("Empty file.")
    pts = extract_points(rows, filename_start(filepath))
    if len(pts) < 5:
        raise ValueError("Too few points.")
    apply_fit_hr(pts, filepath)
    return compute_distances(pts)


def concat_runs(parts, gap_s=1):
    """Join the points of one outing recorded as several files. Each later part is shifted in time to
    follow the previous one (the pause between recordings is not moving time, and would distort the
    pace of the km that contains it) and its distance continues from the previous total."""
    out = list(parts[0])
    for part in parts[1:]:
        shift = out[-1]["ts"] - part[0]["ts"] + timedelta(seconds=gap_s)
        d0 = out[-1]["dist_m"]
        for p in part:
            q = dict(p)
            q["ts"] = p["ts"] + shift
            q["dist_m"] = p["dist_m"] + d0
            out.append(q)
    return out


def analyze_file(filepath, fetch=None):
    """Analyse one CSV (plus the CSVs merged into it, see merged_members). Returns {session, reps (the repetitions of an interval workout, else []), splits, is_sprint, sprint_idx, alt_min, alt_max, avg_hr, weather,
    start, dur_s, total_km, pace_s, ef, drift, tss, points}; raises ValueError(message) if unusable."""
    if fetch is None:
        fetch = FETCH_WEATHER
    pts = file_points(filepath)
    members = merged_members(os.path.dirname(filepath) or ".").get(os.path.basename(filepath), [])
    if members:
        pts = concat_runs([pts] + [file_points(os.path.join(os.path.dirname(filepath), m)) for m in members])

    start_ts  = pts[0]["ts"]
    dur_s     = (pts[-1]["ts"] - start_ts).total_seconds()
    total_km  = pts[-1]["dist_m"] / 1000
    if total_km < 0.5:
        raise ValueError("Distance < 0.5 km, skipped.")

    hr_vals  = [p["hr"] for p in pts if p["hr"] is not None]
    avg_hr   = sum(hr_vals) / len(hr_vals) if hr_vals else None
    alts     = [p["alt"] for p in pts if p["alt"] is not None]
    alt_min, alt_max = (min(alts), max(alts)) if alts else (None, None)
    splits   = km_splits(pts)

    reps = detect_intervals(pts)          # an interval workout has no steady heart rate: no drift, no efficiency, a TSS second by second
    is_sprint, sprint_idx = (False, None) if reps else detect_sprint(splits)
    sprint_tail_s = int(splits[sprint_idx]["dur_s"]) if is_sprint and sprint_idx is not None else 0

    drift  = None if reps else cardiac_drift(pts, exclude_tail_s=sprint_tail_s)
    pace_s = dur_s / total_km
    tss    = tss_from_points(pts) if reps else compute_tss(dur_s, avg_hr)
    ef     = None if reps else compute_ef(total_km, dur_s, avg_hr)

    first_gps_pt = next((p for p in pts if p.get("lat") and p.get("lon")), None)
    weather = fetch_weather(first_gps_pt["lat"], first_gps_pt["lon"], start_ts) if first_gps_pt and fetch else None

    session = {
        "date":         start_ts.strftime("%d/%m/%Y"),
        "time":       start_ts.strftime("%H:%M"),
        "dist_km":      round(total_km, 2),
        "duration_hms":   hms_csv(dur_s),
        "pace_minkm":  pace_csv(pace_s),
        "_pace_s":      pace_s,
        "avg_hr_bpm": round(avg_hr) if avg_hr else "",
        "ef_m_beat":    ef if ef is not None else "",
        "drift_pct":    drift if drift is not None else "",
        "tss":          round(tss) if tss is not None else "",
        "temp_c":       weather["temp_c"]      if weather else "",
        "humidity_pct":  weather["humidity_pct"] if weather else "",
        "wind_kmh":    weather["wind_kmh"]   if weather else "",
        "kind":         "intervals" if reps else "",
    }
    return {"session": session, "reps": reps, "splits": splits, "is_sprint": is_sprint, "sprint_idx": sprint_idx,
            "alt_min": alt_min, "alt_max": alt_max, "avg_hr": avg_hr, "weather": weather, "start": start_ts, "dur_s": dur_s,
            "total_km": total_km, "pace_s": pace_s, "ef": ef, "drift": drift, "tss": tss, "points": pts}


# ─── Map tiles (OpenStreetMap) ────────────────────────────────────────────────

TILE_URL   = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_CACHE = os.path.join(os.path.expanduser("~"), ".running_coach_tiles")    # outside the repos and the data folder
MAX_ZOOM   = 18


def world_px(lat, lon, z):
    """Web-Mercator pixel (x, y) of a coordinate on the whole-world map at zoom z (256 px tiles)."""
    n = 256 * 2 ** z
    s = math.sin(math.radians(max(-85.0511, min(85.0511, lat))))
    return (lon + 180) / 360 * n, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n


def fit_zoom(lats, lons, w, h, margin=40):
    """Highest zoom at which the bounding box of the track fits a w x h view with a margin."""
    for z in range(MAX_ZOOM, 0, -1):
        x0, y0 = world_px(max(lats), min(lons), z)
        x1, y1 = world_px(min(lats), max(lons), z)
        if x1 - x0 <= w - 2 * margin and y1 - y0 <= h - 2 * margin:
            return z
    return 1


def fetch_tile(z, x, y, cache_dir=None):
    """PNG bytes of one OpenStreetMap tile: from the local cache, else downloaded and cached. None on failure.
    Tile policy: an identifying User-Agent, a local cache, no bulk download (a run needs a handful of tiles)."""
    path = os.path.join(cache_dir or TILE_CACHE, str(z), str(x), f"{y}.png")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    try:
        req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y),
                                     headers={"User-Agent": "running-coach/1.0 (personal analyzer, github.com/milanone/running-coach)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return data
    except Exception:
        return None


# ─── intervals.icu download ───────────────────────────────────────────────────

INTERVALS_URL  = "https://intervals.icu/api/v1"
INTERVALS_KEY_FILE = "intervals_api_key.txt"
RUN_TYPES      = ("Run", "TrailRun", "VirtualRun")


def read_api_key(data_dir=None):
    """API key from $INTERVALS_API_KEY, else intervals_api_key.txt next to this program or in the
    data folder. The file is gitignored and excluded from the Drive mirror; never print the key."""
    key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if key:
        return key
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, data_dir):
        path = os.path.join(folder, INTERVALS_KEY_FILE) if folder else None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                return f.read().strip()
    return None


def intervals_get(path, key):
    """GET on the intervals.icu API (HTTP basic auth, user `API_KEY`). A User-Agent is required:
    Cloudflare rejects the default Python one with 403 (error 1010)."""
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    req = urllib.request.Request(INTERVALS_URL + path, headers={
        "Authorization": "Basic " + token, "User-Agent": "running-coach/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def run_filename(start_local):
    """'2026-10-12T08:28:32' -> '2026.10.12 08.28-RUNNING.csv' (the naming the rest of the program expects)."""
    d = datetime.strptime(start_local[:16], "%Y-%m-%dT%H:%M")
    return d.strftime("%Y.%m.%d %H.%M") + "-RUNNING.csv"


def download_new_runs(data_dir, since=None, key=None):
    """Fetch from intervals.icu the per-second streams of the runs not yet in `data_dir`.
    `since` (YYYY-MM-DD) defaults to 30 days before the newest CSV there (90 days ago if none).
    Returns {downloaded [names], skipped (already there), errors [(what, message)]}; raises
    ValueError if there is no key or the listing fails."""
    key = key or read_api_key(data_dir)
    if not key:
        raise ValueError(f"No API key: set INTERVALS_API_KEY or put it in {INTERVALS_KEY_FILE}.")
    if since is None:
        newest = max((filename_start(f) for f in find_run_files(data_dir)), default=None)
        since = (newest - timedelta(days=30) if newest else datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    athlete = os.environ.get("INTERVALS_ATHLETE_ID", "0")   # 0 = the athlete the key belongs to
    try:
        acts = json.loads(intervals_get(
            f"/athlete/{athlete}/activities?oldest={since}&newest={datetime.now():%Y-%m-%d}", key))
    except Exception as e:
        raise ValueError(f"intervals.icu request failed: {getattr(e, 'code', '')} {e}".strip())
    out = {"downloaded": [], "skipped": 0, "errors": []}
    for a in sorted((a for a in acts if a.get("type") in RUN_TYPES), key=lambda a: a["start_date_local"]):
        name = run_filename(a["start_date_local"])
        dest = os.path.join(data_dir, name)
        if os.path.exists(dest):
            out["skipped"] += 1
            continue
        try:
            raw = intervals_get(f"/activity/{a['id']}/streams.csv", key)
        except Exception as e:
            out["errors"].append((name, str(e)))
            continue
        if raw.count(b"\n") < 5:
            out["errors"].append((name, "no stream data"))
            continue
        with open(dest, "wb") as f:   # keep the bytes as served (identical to the manual export)
            f.write(raw)
        out["downloaded"].append(name)
    return out

# ─── Outings recorded as several files ───────────────────────────────────────

MERGE_FILE = "merged_sessions.json"


def merged_members(data_dir):
    """{primary CSV name: [member CSV names]} from `merged_sessions.json` in the data folder, e.g.
    {"2026.08.18 16.45": ["2026.08.18 17.21", "2026.08.18 17.38"]}. Some outings were recorded as
    several sessions (recording restarted after a pause); the members are analysed together with the
    primary one, as a single session. The member CSVs stay in the folder so a download never refetches them."""
    name = lambda k: k + "-RUNNING.csv"
    return {name(p): [name(m) for m in ms] for p, ms in read_merge_groups(data_dir).items()}


def read_merge_groups(data_dir):
    """The raw content of `merged_sessions.json`: {"YYYY.MM.DD HH.MM": ["YYYY.MM.DD HH.MM", ...]}."""
    path = os.path.join(data_dir, MERGE_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def write_merge_groups(data_dir, groups):
    path = os.path.join(data_dir, MERGE_FILE)
    if not groups:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=1, ensure_ascii=False)


def run_key(path):
    """'.../2026.08.18 16.45-RUNNING.csv' -> '2026.08.18 16.45' (the key used in merged_sessions.json)."""
    return os.path.basename(path)[:-len("-RUNNING.csv")]


def merged_member_keys(data_dir):
    """(ISO date, time) keys of the sessions absorbed into another one."""
    keys = set()
    for ms in merged_members(data_dir).values():
        keys.update(file_key(m) for m in ms)
    return keys


def merge_runs(data_dir, history, rows):
    """Merge the given history rows (two or more) into one outing starting at the earliest, as if it
    had been listed in merged_sessions.json. Returns the new history (not saved). Reversible: the CSVs
    stay in the folder and unmerge_run() undoes it. Raises ValueError (nothing is changed)."""
    rows = sorted(rows, key=session_key)
    if len(rows) < 2:
        raise ValueError("Select at least two runs to merge.")
    keys = []
    for r in rows:
        path = session_file(data_dir, r)
        if not path:
            raise ValueError(f"CSV of {r['date']} {r['time']} not found in the folder.")
        keys.append(run_key(path))
    old = read_merge_groups(data_dir)
    groups = {k: list(v) for k, v in old.items()}
    primary = keys[0]
    members = set(groups.get(primary, []))
    for k in keys[1:]:
        members.add(k)
        members.update(groups.pop(k, []))          # a selected run that already absorbed others
    groups[primary] = sorted(members)
    write_merge_groups(data_dir, groups)
    try:
        session = analyze_file(os.path.join(data_dir, primary + "-RUNNING.csv"), fetch=False)["session"]
    except ValueError:
        write_merge_groups(data_dir, old)
        raise
    drop = {file_key(m + "-RUNNING.csv") for m in members}
    return merge_sessions([r for r in history if session_key(r) not in drop], [session])


def unmerge_run(data_dir, history, row):
    """Undo a merge: the primary and its members become separate sessions again (the members get the
    weather recorded for the outing). Returns the new history (not saved)."""
    path = session_file(data_dir, row)
    groups = read_merge_groups(data_dir)
    members = groups.pop(run_key(path), None) if path else None
    if not members:
        raise ValueError("This run is not a merged session.")
    write_merge_groups(data_dir, groups)
    sessions = []
    for k in [run_key(path)] + members:
        s = analyze_file(os.path.join(data_dir, k + "-RUNNING.csv"), fetch=False)["session"]
        for field in ("temp_c", "humidity_pct", "wind_kmh"):
            if s.get(field) in ("", None):
                s[field] = row.get(field, "")
        sessions.append(s)
    return merge_sessions(history, sessions)


def load_folder_history(data_dir):
    """The history of a data folder, without the rows of sessions merged into another one."""
    drop = merged_member_keys(data_dir)
    return [r for r in load_history(history_path(data_dir)) if session_key(r) not in drop]

# ─── File discovery ───────────────────────────────────────────────────────────

def find_run_files(directory="."):
    pat = re.compile(r"^\d{4}\.\d{2}\.\d{2} \d{2}\.\d{2}-RUNNING\.csv$")
    return sorted(f for f in os.listdir(directory) if pat.match(f))


def file_key(path):
    st = filename_start(path)
    return (st.strftime("%Y-%m-%d"), st.strftime("%H:%M")) if st else None


def new_run_files(data_dir, history, reprocess_all=False):
    """CSV paths in data_dir to process, and how many were skipped as already recorded."""
    files = [os.path.join(data_dir, f) for f in find_run_files(data_dir)]
    if reprocess_all:
        return files, 0
    known = {session_key(r) for r in history}
    members = {m for ms in merged_members(data_dir).values() for m in ms}
    for primary, ms in merged_members(data_dir).items():
        if any(file_key(m) in known for m in ms):   # merge not applied yet: recompute the primary row
            known.discard(file_key(primary))
    files = [f for f in files if os.path.basename(f) not in members]
    todo = [f for f in files if file_key(f) not in known]
    return todo, len(files) - len(todo)


def import_sessions(data_dir, fetch=True, reprocess_all=False):
    """Analyse the CSVs not yet in the history. Does not save anything. Returns
    {history, sessions (sorted), errors [(file, message)], skipped}."""
    raw_history = load_history(history_path(data_dir))
    files, skipped = new_run_files(data_dir, raw_history, reprocess_all)
    history = load_folder_history(data_dir)
    sessions, errors = [], []
    for fp in files:
        try:
            sessions.append(analyze_file(fp, fetch)["session"])
        except ValueError as e:
            errors.append((os.path.basename(fp), str(e)))
    return {"history": history, "sessions": sorted(sessions, key=session_key),
            "errors": errors, "skipped": skipped}


def session_file(data_dir, row):
    """Path of the CSV that belongs to a history row, or None."""
    d, t = session_key(row)
    for f in find_run_files(data_dir):
        if file_key(f) == (d, t):
            return os.path.join(data_dir, f)
    return None

