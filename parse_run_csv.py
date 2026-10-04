#!/usr/bin/env python3
"""
parse_run_csv.py – Running activity analyzer for intervals.icu CSV exports
(Samsung Health source via intervals.icu)

Usage:
    python parse_run_csv.py             # process all YYYY.MM.DD HH.MM-RUNNING.csv
    python parse_run_csv.py file.csv    # single file

Output:
    - Per-km breakdown table
    - Session metrics (EF, cardiac drift, TSS)
    - Weather data (Open-Meteo, silent if unavailable)
    - Sprint detection (final km auto-detected)
    - Next-session recommendations
    - Appends/updates running_history.csv
"""

import sys, csv, os, math, re
from datetime import datetime, timedelta

# ─── Constants ────────────────────────────────────────────────────────────────
THRESHOLD_HR    = 165    # lactate threshold HR (bpm)
EF_BASELINE     = 0.92   # m/beat baseline (good aerobic efficiency)
NOISE_ELEV_M    = 0.5    # ignore elevation deltas below this (GPS noise filter)
SPRINT_PACE_S   = 15     # sec/km faster than median to flag sprint
SPRINT_HR_BPM   = 5      # bpm above median to confirm sprint
FETCH_WEATHER   = True   # False or --no-weather: no coordinates sent to Open-Meteo
HISTORY_FILE    = "running_history.csv"
HISTORY_FIELDS  = [
    "data", "orario", "dist_km", "durata_hms", "passo_minkm",
    "fc_media_bpm", "ef_m_batt", "drift_pct", "tss",
    "temp_c", "umidita_pct", "vento_kmh", "gambe",
]

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


def format_hms(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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


def date_it(s):
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


def extract_points(rows, start=None):
    """`time` may be an ISO timestamp or elapsed seconds (intervals.icu); for the latter
    the start datetime comes from `start` (the filename)."""
    if not rows:
        return []
    ts_col   = pick_col(rows, "time", "timestamp", "datetime", "date")
    lat_col  = pick_col(rows, "lat", "latitude")
    lon_col  = pick_col(rows, "lng", "lon", "longitude")
    alt_col  = pick_col(rows, "fixed_altitude", "altitude", "alt", "elevation")
    hr_col   = pick_col(rows, "heartrate", "heart_rate", "hr", "bpm")
    dist_col = pick_col(rows, "distance", "dist", "cum_distance", "cumulative_distance")
    base = start or datetime(2000, 1, 1)

    pts = []
    for row in rows:
        raw = row.get(ts_col, "") if ts_col else ""
        ts = parse_ts(raw)
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
        })
    return sorted(pts, key=lambda p: p["ts"])

# ─── Distance (GPS + pre-GPS estimation) ─────────────────────────────────────

def compute_distances(pts):
    """
    Build cumulative dist_m for each point using haversine GPS.
    Samsung Health doubles the distance field → use GPS coordinates instead.
    Pre-GPS lockup period: estimate distance via average GPS pace.
    """
    first_gps = next((i for i, p in enumerate(pts)
                      if p["lat"] is not None and p["lon"] is not None), None)

    if first_gps is None:
        # No GPS at all: fall back to dist_raw / 2
        cum = 0.0
        for i, p in enumerate(pts):
            raw = p["dist_raw"]
            p["dist_m"] = (raw / 2.0) if raw is not None else cum
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

# ─── Elevation gain ───────────────────────────────────────────────────────────

def climb_step(anchor, alt):
    """Hysteresis: count a climb only once altitude moves more than NOISE_ELEV_M away from
    the last reference point (per-sample deltas at 1 Hz are always below the threshold)."""
    if anchor is None:
        return 0.0, alt
    if alt - anchor > NOISE_ELEV_M:
        return alt - anchor, alt
    if anchor - alt > NOISE_ELEV_M:
        return 0.0, alt
    return 0.0, anchor


def elevation_gain(pts):
    gain, anchor = 0.0, None
    for p in pts:
        if p["alt"] is not None:
            g, anchor = climb_step(anchor, p["alt"])
            gain += g
    return gain

# ─── Per-km splits ────────────────────────────────────────────────────────────

def km_splits(pts):
    """
    Returns list of dicts:
      { km, pace_s (sec/km), avg_hr, elev_gain, partial (bool), partial_dist_m }
    """
    if not pts:
        return []

    splits  = []
    km_num  = 1
    t0      = pts[0]["ts"]
    hr_sum  = hr_n = 0
    dplus   = 0.0
    prev_alt = None

    for p in pts:
        d = p.get("dist_m", 0)

        if p["hr"] is not None:
            hr_sum += p["hr"]
            hr_n   += 1

        if p["alt"] is not None:
            g, prev_alt = climb_step(prev_alt, p["alt"])
            dplus += g

        if d >= km_num * 1000:
            dur = (p["ts"] - t0).total_seconds()
            splits.append({
                "km": km_num, "pace_s": dur, "dur_s": dur,
                "avg_hr": hr_sum / hr_n if hr_n else None,
                "elev_gain": dplus, "partial": False,
            })
            km_num   += 1
            t0        = p["ts"]
            hr_sum    = hr_n = 0
            dplus     = 0.0
            prev_alt  = p["alt"] if p["alt"] is not None else None

    # Partial last km (> 100 m)
    last_d = pts[-1].get("dist_m", 0) - (km_num - 1) * 1000
    if last_d > 100:
        dur = (pts[-1]["ts"] - t0).total_seconds()
        pace = dur / (last_d / 1000)
        splits.append({
            "km": f"{km_num}*", "pace_s": pace, "dur_s": dur,
            "avg_hr": hr_sum / hr_n if hr_n else None,
            "elev_gain": dplus, "partial": True, "partial_dist_m": last_d,
        })

    return splits

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

def cardiac_drift(pts, exclude_tail_s=0):
    """
    HR drift = (avg_hr second half – avg_hr first half) / first_half × 100
    Excludes the last `exclude_tail_s` seconds (sprint segment).
    """
    work = pts
    if exclude_tail_s > 0:
        cutoff = pts[-1]["ts"] - timedelta(seconds=exclude_tail_s)
        work = [p for p in pts if p["ts"] <= cutoff]

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
            "umidita_pct": data["hourly"]["relativehumidity_2m"][h],
            "vento_kmh":   data["hourly"]["windspeed_10m"][h],
        }
    except Exception:
        return None

# ─── History I/O ─────────────────────────────────────────────────────────────

def session_key(r):
    return (date_iso(r.get("data", "")), (r.get("orario") or "").strip())


def upsert(records, rec, keep_old_legs=False):
    """Deduplicate by (date, time) across date formats; the new record wins, but fields it
    leaves empty (weather, `gambe`) keep the previously recorded value. With keep_old_legs the
    recorded `gambe` always survives (used when merging duplicates already in the file)."""
    for i, r in enumerate(records):
        if session_key(r) == session_key(rec):
            for k in HISTORY_FIELDS:
                if rec.get(k) in ("", None) or (k == "gambe" and keep_old_legs and r.get(k)):
                    rec[k] = r.get(k, "")
            records[i] = rec
            return records
    records.append(rec)
    return records


def normalize_row(r):
    r = {k: (r.get(k) or "").strip() for k in HISTORY_FIELDS}
    r["data"] = date_it(r["data"])
    if r["durata_hms"].count(":") == 1:
        r["durata_hms"] = "00:" + r["durata_hms"]
    pace = parse_pace(r["passo_minkm"])
    if pace:
        r["passo_minkm"] = pace_csv(pace)
    return r


def load_history():
    """Read the history in any mix of date formats; collapse duplicate sessions (rows written
    in ISO format by newer runs win over older dd/mm/yyyy rows) and sort chronologically."""
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.DictReader(f))
    raw.sort(key=lambda r: bool(re.match(r"\d{4}-\d{2}-\d{2}", (r.get("data") or "").strip())))
    out = []
    for r in raw:
        upsert(out, normalize_row(r), keep_old_legs=True)
    return sorted(out, key=session_key)


def save_history(records):
    with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)


def is_latest(history, rec):
    return bool(history) and session_key(rec) == max(session_key(r) for r in history)

# ─── Recommendations ─────────────────────────────────────────────────────────

def get_leg_condition():
    opts = {"1": "fresche", "2": "ok", "3": "pesanti", "4": "dolenti"}
    print("\nCondizione gambe:")
    for k, v in opts.items():
        print(f"  {k}) {v}")
    ch = input("Scelta [1-4, default 2]: ").strip() or "2"
    return opts.get(ch, "ok")


def recommendations(history, session, legs):
    hist = sorted(history, key=lambda r: (r.get("data", ""), r.get("orario", "")))
    recent = hist[-4:] if len(hist) >= 4 else hist

    # EF trend over recent sessions
    ef_vals = []
    for r in recent:
        v = safe_float(r.get("ef_m_batt"))
        if v:
            ef_vals.append(v)
    ef_trend = None
    if len(ef_vals) >= 2:
        ef_trend = "improving" if ef_vals[-1] > ef_vals[0] else "declining"

    drift = safe_float(session.get("drift_pct"))
    tss   = safe_float(session.get("tss")) or 0
    dist  = safe_float(session.get("dist_km")) or 5.0
    pace  = session.get("_pace_s") or parse_pace(session.get("passo_minkm")) or 390

    # Rest days
    rest = 1
    if tss > 80:  rest += 1
    if tss > 120: rest += 1
    if legs == "dolenti": rest += 1
    if drift is not None and drift > 8: rest += 1
    rest = min(rest, 4)

    # Distance recommendation
    dist_rec = dist
    if ef_trend == "improving" and (drift is None or drift < 6):
        dist_rec = round(dist * 1.08, 1)   # +8% load progression
    elif legs in ("dolenti", "pesanti") or (drift is not None and drift > 10):
        dist_rec = round(dist * 0.85, 1)   # recovery run

    # Pace adjustment (seconds/km)
    pace_rec = pace
    if legs == "dolenti":
        pace_rec += 20
    elif ef_trend == "improving" and (drift is None or drift < 5):
        pace_rec -= 5

    print("\n" + "═" * 52)
    print("  PROSSIMA SESSIONE CONSIGLIATA")
    print("═" * 52)
    print(f"  Riposo:   {rest} {'giorno' if rest == 1 else 'giorni'}")
    print(f"  Distanza: {dist_rec:.1f} km")
    print(f"  Passo:    {format_pace(pace_rec)}/km")
    if ef_trend:
        arrow = "↑" if ef_trend == "improving" else "↓"
        print(f"  EF trend: {arrow} {ef_trend}")
    if legs == "dolenti":
        print("  ⚠  Gambe dolenti → carico ridotto, +1 giorno riposo")
    elif legs == "fresche":
        print("  ✓  Gambe fresche → puoi spingere")
    if drift is not None and drift > 8:
        print(f"  ⚠  Drift elevato ({drift:.1f}%) → fatica accumulata")
    elif drift is not None and drift < 3:
        print(f"  ✓  Drift basso ({drift:.1f}%) → ottima forma aerobica")
    print("═" * 52)

# ─── Single-file analysis ─────────────────────────────────────────────────────

def analyze(filepath):
    print(f"\n{'═'*60}")
    print(f"  {os.path.basename(filepath)}")
    print(f"{'═'*60}")

    rows = load_csv(filepath)
    if not rows:
        print("  File vuoto."); return None

    pts = extract_points(rows, filename_start(filepath))
    if len(pts) < 5:
        print("  Troppo pochi punti."); return None

    pts = compute_distances(pts)

    start_ts  = pts[0]["ts"]
    end_ts    = pts[-1]["ts"]
    dur_s     = (end_ts - start_ts).total_seconds()
    total_km  = pts[-1]["dist_m"] / 1000

    if total_km < 0.5:
        print("  Distanza < 0.5 km, skip."); return None

    hr_vals  = [p["hr"] for p in pts if p["hr"] is not None]
    avg_hr   = sum(hr_vals) / len(hr_vals) if hr_vals else None
    elev     = elevation_gain(pts)
    splits   = km_splits(pts)

    # Sprint detection
    is_sprint, sprint_idx = detect_sprint(splits)
    sprint_tail_s = 0
    if is_sprint and sprint_idx is not None:
        sprint_tail_s = int(splits[sprint_idx]["dur_s"])

    drift = cardiac_drift(pts, exclude_tail_s=sprint_tail_s)
    pace_s = dur_s / total_km  # sec/km
    tss    = compute_tss(dur_s, avg_hr)
    ef     = compute_ef(total_km, dur_s, avg_hr)

    # Weather
    first_gps_pt = next((p for p in pts if p.get("lat") and p.get("lon")), None)
    weather = fetch_weather(first_gps_pt["lat"], first_gps_pt["lon"], start_ts) if first_gps_pt and FETCH_WEATHER else None

    # ── Print session summary ─────────────────────────────────────────────
    print(f"\n  Data:       {start_ts.strftime('%d/%m/%Y %H:%M')}")
    print(f"  Distanza:   {total_km:.2f} km")
    print(f"  Durata:     {format_hms(dur_s)}")
    print(f"  Passo:      {format_pace(pace_s)}/km")
    if avg_hr:
        print(f"  FC media:   {avg_hr:.0f} bpm")
    if ef:
        status = "✓" if ef >= EF_BASELINE else "○"
        print(f"  EF:         {ef:.3f} m/batt {status}")
    if drift is not None:
        note = "↑ deriva" if drift > 8 else ("↓ fresco" if drift < 3 else "~")
        suffix = " (escluso sprint)" if is_sprint else ""
        print(f"  Drift:      {drift:.1f}% {note}{suffix}")
    if tss:
        print(f"  TSS:        {tss:.0f}")
    print(f"  Dislivello: +{elev:.0f} m")

    if weather:
        w = weather
        print(f"  Meteo:      {w['temp_c']:.1f}°C  {w['umidita_pct']:.0f}% umidità  {w['vento_kmh']:.0f} km/h vento")
        if w["umidita_pct"] > 75:
            print("  ⚠  Umidità >75%: termoregolazione compromessa")
        if w["temp_c"] > 22:
            print(f"  ⚠  Caldo ({w['temp_c']:.0f}°C): atteso rallentamento ~{int((w['temp_c']-18)*12)} sec/km")

    if is_sprint and sprint_idx is not None:
        sk = splits[sprint_idx]
        print(f"\n  ⚡ Sprint finale rilevato (km {sk['km']}): {format_pace(sk['pace_s'])}/km")

    # ── Per-km table ──────────────────────────────────────────────────────
    print(f"\n  {'km':>4}  {'passo':>7}  {'FC':>5}  {'d+':>5}")
    print(f"  {'─'*4}  {'─'*7}  {'─'*5}  {'─'*5}")
    for i, s in enumerate(splits):
        km_label = str(s["km"])
        pace_str = format_pace(s["pace_s"])
        hr_str   = f"{s['avg_hr']:.0f}" if s.get("avg_hr") else "  —"
        el_str   = f"+{s['elev_gain']:.0f}m" if s["elev_gain"] > 0.5 else "   —"
        flag     = " ⚡" if is_sprint and i == sprint_idx else ""
        print(f"  {km_label:>4}  {pace_str:>7}  {hr_str:>5}  {el_str:>5}{flag}")

    return {
        "data":         start_ts.strftime("%d/%m/%Y"),
        "orario":       start_ts.strftime("%H:%M"),
        "dist_km":      round(total_km, 2),
        "durata_hms":   hms_csv(dur_s),
        "passo_minkm":  pace_csv(pace_s),
        "_pace_s":      pace_s,
        "fc_media_bpm": round(avg_hr) if avg_hr else "",
        "ef_m_batt":    ef if ef is not None else "",
        "drift_pct":    drift if drift is not None else "",
        "tss":          round(tss) if tss is not None else "",
        "temp_c":       weather["temp_c"]      if weather else "",
        "umidita_pct":  weather["umidita_pct"] if weather else "",
        "vento_kmh":    weather["vento_kmh"]   if weather else "",
        "gambe":        "",
    }

# ─── File discovery ───────────────────────────────────────────────────────────

def find_run_files(directory="."):
    pat = re.compile(r"^\d{4}\.\d{2}\.\d{2} \d{2}\.\d{2}-RUNNING\.csv$")
    return sorted(f for f in os.listdir(directory) if pat.match(f))

# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    global FETCH_WEATHER, HISTORY_FILE
    args = sys.argv[1:]
    if "--no-weather" in args:
        FETCH_WEATHER = False
        args = [a for a in args if a != "--no-weather"]
    data_dir = os.environ.get("RUNNING_COACH_DATA", ".")
    if "--data" in args:
        i = args.index("--data")
        if i + 1 >= len(args):
            sys.exit("--data richiede una cartella")
        data_dir = args[i + 1]
        del args[i:i + 2]
    if not os.path.isdir(data_dir):
        sys.exit(f"Cartella dati non trovata: {data_dir}")
    HISTORY_FILE = os.path.join(data_dir, HISTORY_FILE)
    if args:
        files = args
    else:
        files = [os.path.join(data_dir, f) for f in find_run_files(data_dir)]

    if not files:
        print("Nessun file RUNNING trovato.")
        print("Uso: python parse_run_csv.py [--data CARTELLA] [--no-weather] [file.csv ...]")
        sys.exit(1)

    history  = load_history()
    sessions = []

    for fp in files:
        sess = analyze(fp)
        if sess:
            sessions.append(sess)

    if not sessions:
        print("\nNessuna sessione analizzata.")
        return

    print(f"\n{len(sessions)} sessione/i analizzata/e.")

    sessions.sort(key=session_key)
    for sess in sessions:
        history = upsert(history, sess)
    history.sort(key=session_key)

    last = sessions[-1]
    latest = is_latest(history, last)
    if latest:
        legs = get_leg_condition()
        last["gambe"] = legs

    save_history(history)
    print(f"\n  ✓ Storico aggiornato: {HISTORY_FILE} ({len(history)} sessioni)")

    if latest:
        recommendations(history, last, legs)
    else:
        newest = history[-1]
        print(f"\n  Nessun consiglio: l'ultima sessione dello storico è {newest['data']} {newest['orario']}.")


if __name__ == "__main__":
    main()
