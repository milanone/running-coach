#!/usr/bin/env python3
"""run_metrics.py - advanced running metrics from the per-second points (standard library only).

Everything here is an ESTIMATE built from heart rate, speed, cadence, altitude and the run history, in the spirit of what
a Garmin watch reports (training load, VO2max, race predictions, training effect...). The vendors' own algorithms are
proprietary, so the numbers are not expected to match theirs; they are meant to be read as trends. Each formula says what
it is and where it is weak. The athlete's parameters (maximum and resting heart rate, body mass) live in `athlete.json`
in the data folder, never in the repository.
"""
import bisect
import json
import math
import os
import statistics
from datetime import date, datetime, timedelta

import parse_run_csv as core

ATHLETE_FILE = "athlete.json"
CACHE_FILE = "run_metrics_cache.json"
CACHE_VERSION = 6                        # bump when a formula changes: the cached per-run metrics are recomputed
DEFAULT_ATHLETE = {
    "hr_max": 190,                       # bpm
    "hr_rest": 60,                       # bpm
    "mass_kg": 70,
    "cadence_factor": None,              # 1 or 2: the CSV cadence is per foot when ~88 (x2 = steps/min); None = decide from the data
    "hr_ref": None,                      # heart rate used for "pace at a fixed heart rate"; None = 60% of the heart-rate reserve
    "hr_max_estimated": None,            # True when hr_max is a formula or a guess, not measured: the VO2max is then also given for hr_max +- 15
    "height_cm": None,                   # with the mass gives the body mass index, which the median heart-rate recovery of the Gauges tab needs
    "age": None,                         # years: with the sex, places the VO2max against the Cooper Institute's norms (Gauges tab)
    "sex": None,                         # "M" or "F"
}
CLIP_SECONDS = 30                        # a heart rate at the run's maximum for this long is a clipped recording, not a heart rate
HRMAX_MARGIN = 15                        # bpm: Firstbeat reports about 7-9% of VO2max error for a maximum heart rate off by this much
MOVING_MS = 3 / 3.6                      # below 3 km/h the athlete is counted as stopped
ZONE_EDGES = (0.5, 0.6, 0.7, 0.8, 0.9)   # fractions of the maximum heart rate (Garmin's default zones)
RIEGEL_EXPONENT = 1.06
TE_SCALE = 110.0                         # Edwards TRIMP at which the training-effect scale reaches 63% of 5 (see aerobic_te)
BEST_DISTANCES = (("1 km", 1000.0), ("1 mile", 1609.344), ("3 km", 3000.0), ("5 km", 5000.0),
                  ("10 km", 10000.0), ("Half marathon", 21097.5))
RACES = (("5 km", 5000.0), ("10 km", 10000.0), ("Half marathon", 21097.5), ("Marathon", 42195.0))
LOW_WEEKLY_TSS = 100                     # below this load in 7 days a high acute / chronic ratio is "ramping up", not strain
POWER_COEFF = 1.04                       # W per kg per m/s on the flat: a rough running-power model, not a measured power


# ─── Athlete ─────────────────────────────────────────────────────────────────

def read_athlete(data_dir):
    """The parameters of `athlete.json` over the defaults; a missing or unreadable file gives the defaults."""
    athlete = dict(DEFAULT_ATHLETE)
    try:
        with open(os.path.join(data_dir, ATHLETE_FILE), encoding="utf-8-sig") as f:
            athlete.update({k: v for k, v in json.load(f).items() if k in athlete})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    sex = str(athlete["sex"]).strip().upper()[:1] if athlete["sex"] else None
    athlete["sex"] = sex if sex in ("M", "F") else None
    height = athlete["height_cm"]
    athlete["height_cm"] = height if isinstance(height, (int, float)) and not isinstance(height, bool) and 100 <= height <= 230 else None
    age = athlete["age"]
    athlete["age"] = age if isinstance(age, (int, float)) and not isinstance(age, bool) and 10 <= age <= 110 else None
    return athlete


def write_athlete(data_dir, values):
    """Save parameters into `athlete.json`, keeping the keys it already has (and any that this program does not know). A value of None
    removes its key. Returns the path."""
    path = os.path.join(data_dir, ATHLETE_FILE)
    try:
        with open(path, encoding="utf-8-sig") as f:
            current = json.load(f)
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    for k, v in values.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    os.replace(tmp, path)
    return path


def fmt_time(seconds):
    """m:ss, or h:mm:ss from one hour."""
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


# ─── Per-second series ───────────────────────────────────────────────────────

def series(pts):
    """Parallel lists from the points: t (s from the start), d (cumulative m, never decreasing), v (m/s; the CSV speed, else the
    distance change), hr, cad, alt, dt (s since the previous sample, capped) and moving (speed above MOVING_MS)."""
    t0 = pts[0]["ts"]
    t = [(p["ts"] - t0).total_seconds() for p in pts]
    d, top = [], 0.0
    for p in pts:
        top = max(top, p.get("dist_m") or 0.0)
        d.append(top)
    v = []
    for i, p in enumerate(pts):
        sp = p.get("speed")
        if sp is None and i and t[i] > t[i - 1]:
            sp = (d[i] - d[i - 1]) / (t[i] - t[i - 1])
        v.append(sp)
    dt = [0.0] + [min(5.0, max(0.0, t[i] - t[i - 1])) for i in range(1, len(t))]
    hr = [p.get("hr") for p in pts]
    clipped = _clipped_plateau(hr, t)
    for i in clipped:
        hr[i] = None
    return {"t": t, "d": d, "v": v, "hr": hr, "clipped_s": len(clipped), "cad": [p.get("cadence") for p in pts],
            "alt": [p["alt_raw"] if p.get("alt_raw") is not None else p.get("alt") for p in pts], "dt": dt,
            "moving": [x is not None and x >= MOVING_MS for x in v]}


def _mean(xs):
    return sum(xs) / len(xs)


def _clipped_plateau(hr, t):
    """Indices of a heart rate stuck for CLIP_SECONDS or more at exactly the maximum of the run: a real heart rate at its peak keeps
    moving by a beat or two, so this is a ceiling of the recording chain (seen in intervals.icu exports, where a maximum heart rate set too
    low for the sport cuts the real values). Those samples are left out: they would pass for a steady heart rate against a speed that keeps rising."""
    vals = [h for h in hr if h is not None]
    if not vals or min(vals) == max(vals):
        return []                                                  # no heart rate, or a constant one: no peak to be clipped
    top, out, i = max(vals), [], 0
    while i < len(hr):
        if hr[i] == top:
            j = i
            while j + 1 < len(hr) and hr[j + 1] == top:
                j += 1
            if t[j] - t[i] >= CLIP_SECONDS:
                out.extend(range(i, j + 1))
            i = j + 1
        else:
            i += 1
    return out


# ─── Heart-rate zones, TRIMP, training effect ────────────────────────────────

def zone_seconds(S, athlete):
    """Seconds in zone 0 (below 50% of the maximum heart rate) and zones 1-5 (50-60, 60-70, 70-80, 80-90, 90-100%)."""
    zones = [0.0] * 6
    for hr, dt in zip(S["hr"], S["dt"]):
        if hr is not None:
            zones[sum(hr / athlete["hr_max"] >= e for e in ZONE_EDGES)] += dt
    return zones


def edwards_trimp(zones):
    """Edwards' TRIMP: minutes in each zone times the zone number (1-5). No sex-specific coefficients, unlike Banister's."""
    return sum(zones[z] / 60.0 * z for z in range(1, 6))


def aerobic_te(trimp):
    """A 0-5 'aerobic training effect' from the TRIMP: 5 * (1 - exp(-TRIMP / 110)). INDICATIVE ONLY, and not Firstbeat's method: theirs takes
    the PEAK EPOC reached in the session (modelled from the heart rate as %VO2max) against the athlete's activity class of the last month
    (Firstbeat white paper, 2012), neither of which is published as numbers. Only the 0-5 bands are theirs; my scale is not calibrated."""
    return 5.0 * (1.0 - math.exp(-trimp / TE_SCALE))


def recovery_hours(te):
    """Indicative recovery time, 4 * TE^2 hours (TE 2 = 16 h, 3 = 36 h, 4 = 64 h). Made up to be in the range Garmin shows."""
    return 4.0 * te * te


# ─── Best efforts and race predictions ───────────────────────────────────────

def best_efforts(S, distances=BEST_DISTANCES):
    """{label: (seconds, start second)} of the fastest stretch of each distance inside the run (elapsed time, stops included).
    Two pointers over the cumulative distance, the end of the stretch interpolated between two samples."""
    t, d = S["t"], S["d"]
    out = {}
    n = len(t)
    for label, dist in distances:
        if not d or d[-1] < dist:
            continue
        best, j = None, 0
        for i in range(n):
            j = max(j, i)
            while j < n and d[j] - d[i] < dist:
                j += 1
            if j >= n:
                break
            step = d[j] - d[j - 1]
            frac = (dist - (d[j - 1] - d[i])) / step if step > 0 else 1.0
            dur = t[j - 1] + frac * (t[j] - t[j - 1]) - t[i]
            if best is None or dur < best[0]:
                best = (dur, t[i])
        if best:
            out[label] = best
    return out


def riegel(seconds, dist_from, dist_to):
    """Riegel's prediction T2 = T1 * (D2 / D1)^1.06. Fed with training runs it is pessimistic: a race is a harder effort."""
    return seconds * (dist_to / dist_from) ** RIEGEL_EXPONENT


def _vo2_of_speed(v_m_min):
    return -4.60 + 0.182258 * v_m_min + 0.000104 * v_m_min ** 2


def _fraction_of_vo2max(t_min):
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)


def vdot_of_race(dist_m, seconds):
    """Daniels-Gilbert VDOT of a race result (the VO2max that would explain it)."""
    t = seconds / 60.0
    return _vo2_of_speed(dist_m / t) / _fraction_of_vo2max(t)


def race_time_of_vdot(vdot, dist_m):
    """Seconds a runner of that VDOT takes over dist_m (bisection on the Daniels-Gilbert equations)."""
    lo, hi = dist_m / 700.0, dist_m / 60.0                  # minutes: absurdly fast, absurdly slow
    for _ in range(80):
        mid = (lo + hi) / 2
        if vdot_of_race(dist_m, mid * 60) > vdot:          # still too fast for this VDOT: it takes longer
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 60


# ─── VO2max, heart rate against speed ────────────────────────────────────────

def vo2max_estimate(S, athlete):
    """VO2max (ml/kg/min) from the heart rate at a steady speed, as the median over steady one-minute windows after the first
    five minutes: VO2 at that speed from the ACSM running equation (0.2 * m/min + 3.5, flat), divided by the fraction of the heart-rate
    reserve (taken equal to the fraction of the VO2 reserve): VO2max = (VO2 - 3.5) / HRR + 3.5. Needs three steady windows.
    Weak points: the ACSM equation is meant for speeds above ~8 km/h, the heart rate drifts up with time and heat (underestimating),
    and the maximum and resting rates must be right. Treat it as +-10% and look at the trend."""
    t, v, hr, moving = S["t"], S["v"], S["hr"], S["moving"]
    reserve = athlete["hr_max"] - athlete["hr_rest"]
    n, found = len(t), []
    k = bisect.bisect_left(t, 300.0)
    while k < n:
        j = bisect.bisect_left(t, t[k] + 60.0)
        if j >= n:
            break
        idx = range(k, j)
        vs = [v[i] for i in idx if v[i] is not None]
        hs = [hr[i] for i in idx if hr[i] is not None]
        if sum(moving[i] for i in idx) >= 0.9 * len(idx) and len(vs) >= 50 and len(hs) >= 50:
            mv, mh = _mean(vs), _mean(hs)
            if statistics.pstdev(hs) < 4 and statistics.pstdev(vs) / mv < 0.12:
                frac = (mh - athlete["hr_rest"]) / reserve
                if 0.5 <= frac <= 0.95:
                    found.append((0.2 * mv * 60 + 3.5 - 3.5) / frac + 3.5)
        k = max(k + 1, bisect.bisect_left(t, t[k] + 30.0))
    return statistics.median(found) if len(found) >= 3 else None


def _steady(S, warmup=600.0):
    """Indexes after the warm-up (at most a quarter of the run) where the athlete is moving."""
    start = min(warmup, 0.25 * S["t"][-1])
    return [i for i, ok in enumerate(S["moving"]) if ok and S["t"][i] >= start]


def pace_at_hr(S, hr_ref, tol=5, minimum=240):
    """Pace (s/km) at a fixed heart rate: the median speed while the heart rate is within hr_ref +- tol, after the warm-up.
    Lower is better and, unlike the average pace, it does not depend on how hard you chose to run that day."""
    sp = [S["v"][i] for i in _steady(S) if S["hr"][i] is not None and abs(S["hr"][i] - hr_ref) <= tol and S["v"][i]]
    return 1000.0 / statistics.median(sp) if len(sp) >= minimum else None


def hr_at_speed(S, v_ref, tol=0.4 / 3.6, minimum=240):
    """Median heart rate while the speed is within v_ref +- 0.4 km/h, after the warm-up (lower = fitter at that speed)."""
    hs = [S["hr"][i] for i in _steady(S) if S["hr"][i] is not None and S["v"][i] is not None and abs(S["v"][i] - v_ref) <= tol]
    return statistics.median(hs) if len(hs) >= minimum else None


def hr_speed_table(S, step=0.05):
    """The steady part of a run as {speed bin (step km/h): {heart rate: seconds}}, so that the heart rate at ANY reference speed can be found later
    without reading the run again (the reference speed is the median pace of the history and moves with every new run)."""
    table = {}
    for i in _steady(S):
        h, v = S["hr"][i], S["v"][i]
        if h is not None and v is not None:
            bins = table.setdefault(str(round(v * 3.6 / step)), {})
            key = str(int(round(h)))
            bins[key] = bins.get(key, 0) + 1
    return table


def hr_at_speed_from_table(table, v_ref, step=0.05, tol_kmh=0.4, minimum=240):
    """hr_at_speed from hr_speed_table (the bins make the limits of the +-0.4 km/h window fuzzy by half a bin)."""
    if not table or not v_ref:
        return None
    centre = v_ref * 3.6 / step
    reach = tol_kmh / step
    counts = {}
    for b, hrs in table.items():
        if abs(int(b) - centre) <= reach:
            for h, n in hrs.items():
                counts[int(h)] = counts.get(int(h), 0) + n
    total = sum(counts.values())
    if total < minimum:
        return None
    seen, half = 0, total / 2.0
    for h in sorted(counts):
        seen += counts[h]
        if seen >= half:
            return float(h)


def ef_window(S, start=360.0, end=1200.0):
    """Efficiency (m/beat) between minute 6 and 20: the same stretch of every run, for the performance-condition comparison."""
    idx = [i for i, ok in enumerate(S["moving"]) if ok and start <= S["t"][i] <= end and S["hr"][i] and S["v"][i]]
    if len(idx) < 240:
        return None
    return _mean([S["v"][i] for i in idx]) * 60.0 / _mean([S["hr"][i] for i in idx])


# ─── Cadence, stride, power ──────────────────────────────────────────────────

def cadence_stats(S, athlete):
    """(steps per minute, step length in m, factor applied to the CSV cadence). A CSV cadence under 120 is read as per foot (x2)."""
    idx = [i for i, ok in enumerate(S["moving"]) if ok and S["cad"][i] and S["cad"][i] > 0 and S["v"][i]]
    if len(idx) < 60:
        return None, None, None
    raw = statistics.median([S["cad"][i] for i in idx])
    factor = athlete["cadence_factor"] or (2 if raw < 120 else 1)
    step = statistics.median([S["v"][i] * 60.0 / (S["cad"][i] * factor) for i in idx])
    return raw * factor, step, factor


def power_estimate(S, athlete):
    """(W, W/kg): mean over the moving time of mass * speed * (1.04 + 9.81 * grade), the grade from the altitude smoothed over
    about 30 s. A ROUGH model, not a measured power (Stryd or a Garmin sensor measure the real thing). Minetti et al. 2002 (J Appl Physiol
    93:1039, doi 10.1152/japplphysiol.01177.2001) measured a METABOLIC cost of running of 3.40 J/kg/m on the level, independent of speed;
    1.04 is 0.31 of that, and the 0.31 (mechanical over metabolic) is my assumption, not a measured efficiency."""
    n = len(S["t"])
    alt = [a for a in S["alt"]]
    if n < 60 or any(a is None for a in alt):
        grade = [0.0] * n
    else:
        pre = [0.0]
        for a in alt:
            pre.append(pre[-1] + a)
        sm = [(pre[min(n, i + 8)] - pre[max(0, i - 7)]) / (min(n, i + 8) - max(0, i - 7)) for i in range(n)]
        grade = []
        for i in range(n):
            a, b = max(0, i - 15), min(n - 1, i + 15)
            run = S["d"][b] - S["d"][a]
            grade.append(max(-0.1, min(0.1, (sm[b] - sm[a]) / run)) if run >= 10 else 0.0)
    idx = [i for i, ok in enumerate(S["moving"]) if ok and S["v"][i]]
    if len(idx) < 60:
        return None, None
    mass = athlete["mass_kg"]
    watts = [max(0.3, POWER_COEFF + 9.81 * grade[i]) * mass * S["v"][i] for i in idx]
    w = _mean(watts)
    return w, w / mass


# ─── Recovery, rise time, pacing ─────────────────────────────────────────────

def hr_recovery(S):
    """Heart-rate drop in the 60 s after the last moving second (bpm), when the recording goes on for at least that long."""
    moving = [i for i, ok in enumerate(S["moving"]) if ok]
    if not moving:
        return None
    end = moving[-1]
    t = S["t"]
    after = bisect.bisect_left(t, t[end] + 60.0)
    if after >= len(t):
        return None
    now = [S["hr"][i] for i in range(max(0, end - 4), end + 1) if S["hr"][i] is not None]
    later = [S["hr"][i] for i in range(max(0, after - 2), min(len(t), after + 3)) if S["hr"][i] is not None]
    return _mean(now) - _mean(later) if now and later else None


def hr_rise_time(S):
    """Seconds for the heart rate to cover 63% of the way from its start value to the level of minutes 5-10 (a time constant of the
    start-up). Needs 11 minutes of data; the optical sensor lags the heart a little, so it is a trend, not a physiological constant."""
    t, hr = S["t"], S["hr"]
    if t[-1] < 660:
        return None
    first = [h for h, x in zip(hr, t) if h is not None and x <= 5]
    plateau = [h for h, x in zip(hr, t) if h is not None and 300 <= x <= 600]
    if not first or len(plateau) < 100:
        return None
    h0, hss = _mean(first), statistics.median(plateau)
    if hss - h0 < 15:
        return None
    target = h0 + 0.63 * (hss - h0)
    for i in range(len(t)):
        window = [h for h in hr[max(0, i - 2):i + 3] if h is not None]
        if window and _mean(window) >= target:
            return t[i]
    return None


def split_balance(S):
    """(second half minus first half) / first half of the moving time, in % (positive = slower second half), halves by distance."""
    d, t, moving = S["d"], S["t"], S["moving"]
    if not d or d[-1] < 1000:
        return None
    clock, cum = 0.0, []
    for i in range(len(t)):
        clock += S["dt"][i] if moving[i] else 0.0
        cum.append(clock)
    half = bisect.bisect_left(d, d[-1] / 2)
    first, second = cum[half], cum[-1] - cum[half]
    return (second - first) / first * 100.0 if first > 0 else None


def pace_variability(S):
    """Coefficient of variation (%) of the full-kilometre times; needs three kilometres."""
    d, t = S["d"], S["t"]
    times, prev, km = [], 0.0, 1
    for i in range(len(t)):
        if d[i] >= km * 1000:
            times.append(t[i] - prev)
            prev, km = t[i], km + 1
    return statistics.pstdev(times) / _mean(times) * 100.0 if len(times) >= 3 else None


def find_efforts(S, rise_kmh=1.0, min_s=60):
    """Stretches at least `min_s` seconds long whose 30-second-smoothed speed is `rise_kmh` above the run's median: surges, intervals,
    the final sprint. [{start, dur, dist, pace, hr}], times in seconds from the start."""
    n = len(S["t"])
    v = [x if x is not None else 0.0 for x in S["v"]]
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    sm = [(pre[min(n, i + 16)] - pre[max(0, i - 15)]) / (min(n, i + 16) - max(0, i - 15)) for i in range(n)]
    moving = [x for x, ok in zip(sm, S["moving"]) if ok]
    if len(moving) < 120:
        return []
    limit = statistics.median(moving) + rise_kmh / 3.6
    out, i = [], 0
    while i < n:
        if sm[i] > limit:
            j = i
            while j + 1 < n and sm[j + 1] > limit:
                j += 1
            dur = S["t"][j] - S["t"][i]
            if dur >= min_s:
                dist = S["d"][j] - S["d"][i]
                hs = [h for h in S["hr"][i:j + 1] if h is not None]
                out.append({"start": S["t"][i], "dur": dur, "dist": dist, "pace": dur / dist * 1000 if dist > 0 else None,
                            "hr": _mean(hs) if hs else None})
            i = j + 1
        else:
            i += 1
    return out


# ─── One run ─────────────────────────────────────────────────────────────────

def rep_details(S, reps):
    """The repetitions of an interval workout with what the per-second series adds: pace, heart rate, and the heart-rate drop from the peak to
    60 s after the end of each (None when the next repetition starts before that or the recording ends)."""
    t, hr = S["t"], S["hr"]

    def hr_around(sec, half):
        i, j = bisect.bisect_left(t, sec - half), bisect.bisect_right(t, sec + half)
        vals = [h for h in hr[i:j] if h is not None]
        return _mean(vals) if vals else None

    def hr_peak(sec):
        """The highest 3-s average heart rate from 3 s before the end of a repetition to 8 s after it (the heart rate lags the effort)."""
        i, j = bisect.bisect_left(t, sec - 3), bisect.bisect_right(t, sec + 8)
        vals = [h for h in hr[i:j] if h is not None]
        return max(sum(vals[k:k + 3]) / len(vals[k:k + 3]) for k in range(len(vals))) if vals else None

    out = []
    for k, r in enumerate(reps):
        nxt = reps[k + 1]["start"] if k + 1 < len(reps) else None
        end_hr, later_hr = hr_peak(r["end"]), hr_around(r["end"] + 60, 2)
        ok = end_hr is not None and later_hr is not None and (nxt is None or nxt >= r["end"] + 62) and t[-1] >= r["end"] + 62
        out.append({"start": r["start"], "dur": r["dur"], "dist": r["dist"], "pace": r["dur"] / r["dist"] * 1000 if r["dist"] > 0 else None,
                    "hr": r["hr"], "hr_max": r["hr_max"], "drop60": end_hr - later_hr if ok else None})
    return out


def run_metrics(pts, athlete, hr_ref=None, speed_ref=None):
    """All the per-run metrics as a flat dict (None where the data does not allow them). `speed_ref` (m/s) is the speed for
    'heart rate at a fixed speed'; the caller takes it from the history (the median pace)."""
    S = series(pts)
    reps = core.detect_intervals(pts)
    steady = not reps                        # repetitions have no steady state: VO2max, pace at a heart rate, efficiency and drift mean nothing
    hr_ref = hr_ref or athlete["hr_ref"] or athlete["hr_rest"] + 0.6 * (athlete["hr_max"] - athlete["hr_rest"])
    zones = zone_seconds(S, athlete)
    trimp = edwards_trimp(zones)
    te = aerobic_te(trimp)
    spm, step, factor = cadence_stats(S, athlete)
    watts, wkg = power_estimate(S, athlete)
    pace_hr = pace_at_hr(S, hr_ref) if steady else None
    hrs = [h for h in S["hr"] if h is not None]
    guess = athlete.get("hr_max_estimated") and steady
    details = rep_details(S, reps)
    drops = [r["drop60"] for r in details if r["drop60"] is not None]
    rep_hr = [(r["hr"], r["dur"]) for r in details if r["hr"]]
    rep_dist = sum(r["dist"] for r in details)
    rep_time = sum(r["dur"] for r in details)
    return {
        "vo2max_if_hrmax_low": vo2max_estimate(S, dict(athlete, hr_max=athlete["hr_max"] - HRMAX_MARGIN)) if guess else None,
        "vo2max_if_hrmax_high": vo2max_estimate(S, dict(athlete, hr_max=athlete["hr_max"] + HRMAX_MARGIN)) if guess else None,
        "moving_s": sum(dt for dt, ok in zip(S["dt"], S["moving"]) if ok),
        "elapsed_s": S["t"][-1],
        "zone_s": zones, "trimp": trimp, "te": te, "recovery_h": recovery_hours(te),
        "vo2max": vo2max_estimate(S, athlete) if steady else None,
        "hr_ref": hr_ref, "pace_at_hr": pace_hr,
        "hr_at_speed": hr_at_speed(S, speed_ref) if speed_ref and steady else None,
        "hr_speed_table": hr_speed_table(S) if steady else {},
        "ef_window": ef_window(S) if steady else None,
        "spm": spm, "step_m": step, "cadence_factor": factor,
        "power_w": watts, "power_wkg": wkg,
        "hrr60": _mean(drops) if drops else hr_recovery(S), "hrr60_n": len(drops), "hr_rise_s": hr_rise_time(S) if steady else None,
        "ef_reps": rep_dist / rep_time * 60 / (sum(h * d for h, d in rep_hr) / sum(d for _, d in rep_hr)) if rep_hr and rep_time > 0 else None,
        "split_balance_pct": split_balance(S) if steady else None, "pace_cv_pct": pace_variability(S) if steady else None,
        "hr_max_seen": max(hrs) if hrs else None, "hr_clipped_s": S["clipped_s"],
        "best": {k: [v[0], v[1]] for k, v in best_efforts(S).items()},
        "efforts": find_efforts(S) if steady else [],
        "reps": details,
    }


# ─── Training load from the history ──────────────────────────────────────────

def training_load(history, today=None):
    """Daily Fitness (CTL, 42-day exponential average of the TSS), Fatigue (ATL, 7-day) and Form (TSB = CTL - ATL) from the history,
    one entry per day from the first run to `today`: [{date, tss, ctl, atl, tsb, acute, chronic}]. The exponential averages start from
    zero, so the first weeks understate the fitness; the acute / chronic ratio is meaningful only after 28 days of history."""
    daily = {}
    for r in history:
        tss = core.safe_float(r.get("tss"))
        if tss is not None:
            day = core.session_key(r)[0]
            daily[day] = daily.get(day, 0.0) + tss
    if not daily:
        return []
    first = date.fromisoformat(min(daily))
    last = max(date.fromisoformat(max(daily)), today or date.fromisoformat(max(daily)))
    ctl = atl = 0.0
    out, seen = [], []
    for k in range((last - first).days + 1):
        day = (first + timedelta(days=k)).isoformat()
        tss = daily.get(day, 0.0)
        seen.append(tss)
        ctl += (tss - ctl) / 42.0
        atl += (tss - atl) / 7.0
        # acute / chronic as Garmin states it: load of the last 7 days over the weekly average of the last 28. With one run a week
        # the 7-day ATL jumps right after each run, so the ratio of the two exponential averages swings between 0.4 and 1.5.
        out.append({"date": day, "tss": tss, "ctl": ctl, "atl": atl, "tsb": ctl - atl,
                    "acute": sum(seen[-7:]), "chronic": sum(seen[-28:]) / 4.0})
    return out


def training_status(load):
    """(label, explanation) from the load ratio ATL / CTL and the change of CTL over 28 days. A heuristic modelled on the labels
    Garmin shows (Productive, Maintaining, Recovery, Unproductive, Strained, Overreaching, Detraining), not on its algorithm."""
    if len(load) < 29:
        return "Not enough history", "28 days of history are needed"
    now, ago = load[-1], load[-29]
    since = 0
    for e in reversed(load):
        if e["tss"] > 0:
            break
        since += 1
    ratio = now["acute"] / now["chronic"] if now["chronic"] > 0 else None
    change = (now["ctl"] - ago["ctl"]) / ago["ctl"] if ago["ctl"] > 1 else None
    if since >= 14:
        return "Detraining", f"no run for {since} days"
    if ratio is None:
        return "Not enough history", "no load yet"
    if ratio > 1.4 and now["acute"] < LOW_WEEKLY_TSS:
        return "Ramping up", f"load ratio {ratio:.2f}, but only {now['acute']:.0f} TSS in 7 days"
    if ratio >= 2.0:
        return "Overreaching", f"load ratio {ratio:.2f} (very high: 2.0 or more in Garmin's table)"
    if ratio > 1.4:
        return "Strained", f"load ratio {ratio:.2f} (high: above the 0.8-1.4 optimal range of Garmin's table)"
    if ratio < 0.8:
        if change is not None and change < -0.15:
            return "Detraining", f"load ratio {ratio:.2f} and fitness down {-change * 100:.0f}% in 28 days"
        return "Recovery", f"load ratio {ratio:.2f} (< 0.8)"
    if change is None:
        return "Maintaining", f"load ratio {ratio:.2f}"
    if change > 0.05:
        return "Productive", f"load ratio {ratio:.2f}, fitness up {change * 100:.0f}% in 28 days"
    if change < -0.05:
        return "Unproductive", f"load ratio {ratio:.2f}, fitness down {-change * 100:.0f}% in 28 days"
    return "Maintaining", f"load ratio {ratio:.2f}, fitness steady (+-5% in 28 days)"


# ─── Whole history ───────────────────────────────────────────────────────────

def median_speed(history):
    """Median of the history's average speeds (m/s), the reference speed for 'heart rate at a fixed speed'."""
    sp = [1000.0 / s for s in (core.parse_pace(r.get("pace_minkm")) for r in history) if s]
    return statistics.median(sp) if sp else None


def _compute_one(path, athlete):
    """The metrics of one run, or None if the file cannot be used."""
    try:
        return run_metrics(core.analyze_file(path, fetch=False)["points"], athlete)
    except (ValueError, OSError):
        return None


def compute_all(data_dir, history, athlete, progress=None):
    """Per-run metrics for every history row that has a CSV, newest last, with the performance condition and the load added.
    The per-run results are kept in `run_metrics_cache.json`, keyed by the file's modification time (and those of the recordings merged into
    it, and of its FIT file) and the athlete's parameters. Not by anything of the other runs (the reference speed, the merge file): those change
    with every new run or merge and would make the whole history be read again; the heart rate at the reference speed is worked out from a
    small table kept with each run. Only the runs that are missing from the file or changed are read (the first time, or after a change of
    formula: about 0.04 s a run); `progress(done, total)` is called as they finish."""
    speed_ref = median_speed(history)
    cache_path = os.path.join(data_dir, CACHE_FILE)
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("version") != CACHE_VERSION:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    cache["version"] = CACHE_VERSION
    entries = cache.setdefault("runs", {})
    merged = core.merged_members(data_dir)
    per_run = {k: v for k, v in athlete.items() if k not in ("age", "sex", "height_cm")}      # these only place a value in the gauges
    plan = []                                                                               # [row, name, path, stamp, metrics or None]
    for row in sorted(history, key=core.session_key):
        path = core.session_file(data_dir, row)
        if not path:
            continue
        fit = core.fit_path(path)
        name = os.path.basename(path)
        members = [os.path.join(data_dir, x) for x in merged.get(name, [])]
        stamp = [os.path.getmtime(path), [os.path.getmtime(x) for x in members if os.path.exists(x)], per_run, fit and os.path.getmtime(fit)]
        hit = entries.get(name)
        plan.append([row, name, path, stamp, hit["metrics"] if hit and hit.get("stamp") == json.loads(json.dumps(stamp)) else None])
    misses = [p for p in plan if p[4] is None]
    total, ready = len(plan), len(plan) - len(misses)
    if progress:
        progress(ready, total)
    for k, p in enumerate(misses, 1):
        p[4] = _compute_one(p[2], athlete)
        if p[4] is not None:
            entries[p[1]] = {"stamp": p[3], "metrics": p[4]}
        if progress:
            progress(ready + k, total)
    rows = []
    for row, name, path, stamp, m in plan:
        if m is None:
            continue
        m = dict(m)
        m["hr_at_speed"] = hr_at_speed_from_table(m.get("hr_speed_table"), speed_ref)
        m.update(date=core.session_key(row)[0], time=core.session_key(row)[1], file=name,
                 tss=core.safe_float(row.get("tss")), dist_km=core.safe_float(row.get("dist_km")))
        rows.append(m)
    if misses:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except OSError:
            pass
    # performance condition: the efficiency of minutes 6-20 against the median of the previous ten runs, in %
    for i, m in enumerate(rows):
        if m.get("reps"):                         # the efficiency of the repetitions against that of the earlier interval workouts
            before = [x["ef_reps"] for x in rows[:i] if x.get("reps") and x.get("ef_reps")][-10:]
            m["perf_n"] = len(before)
            m["perf_pct"] = (m["ef_reps"] / statistics.median(before) - 1) * 100 if m.get("ef_reps") and len(before) >= 3 else None
            continue
        before = [x["ef_window"] for x in rows[max(0, i - 10):i] if x["ef_window"]]
        m["perf_pct"] = (m["ef_window"] / statistics.median(before) - 1) * 100 if m["ef_window"] and len(before) >= 3 else None
    load = {e["date"]: e for e in training_load(history)}
    for m in rows:
        e = load.get(m["date"])
        m["ctl"], m["atl"], m["tsb"] = (e["ctl"], e["atl"], e["tsb"]) if e else (None, None, None)
    return rows


def summary(rows, history, athlete, today=None):
    """The current picture from compute_all's rows: load, status, VO2max, race predictions, best efforts."""
    load = training_load(history, today or date.today())
    status = training_status(load)
    vo2 = [r["vo2max"] for r in rows if r["vo2max"]]
    out = {"load": load[-1] if load else None, "status": status, "athlete": athlete, "runs": len(rows)}
    if vo2:
        recent = vo2[-5:]
        out["vo2max"] = statistics.median(recent)
        out["vo2max_range"] = (min(recent), max(recent))
        out["vdot_races"] = {label: race_time_of_vdot(out["vo2max"], dist) for label, dist in RACES}
        low = [r["vo2max_if_hrmax_low"] for r in rows if r.get("vo2max_if_hrmax_low")][-5:]
        high = [r["vo2max_if_hrmax_high"] for r in rows if r.get("vo2max_if_hrmax_high")][-5:]
        if athlete.get("hr_max_estimated") and low and high:
            out["vo2max_hrmax_range"] = (statistics.median(low), statistics.median(high))
    best = {}                                           # all-time best effort per distance, with the day
    for r in rows:
        for label, (secs, _) in r["best"].items():
            if label not in best or secs < best[label][0]:
                best[label] = (secs, r["date"])
    out["best"] = best
    horizon = (today or date.today()) - timedelta(days=90)
    recent_best = {}
    for r in rows:
        if date.fromisoformat(r["date"]) >= horizon:
            for label, (secs, _) in r["best"].items():
                if label not in recent_best or secs < recent_best[label][0]:
                    recent_best[label] = (secs, r["date"])
    for label, dist in reversed(BEST_DISTANCES):        # the longest distance of 3 km or more that was run in the last 90 days
        if dist >= 3000 and label in recent_best:
            secs, day = recent_best[label]
            out["riegel_from"] = (label, secs, day)
            out["riegel_races"] = {name: riegel(secs, dist, d) for name, d in RACES}
            break
    return out


# ─── Next session ────────────────────────────────────────────────────────────

def next_session(history, rows, summ, today=None):
    """Advice for the next run: the core rules (TSS, drift, EF trend: rest days, distance, pace) refined with the new metrics:
    a high acute / chronic load ratio (with a real load behind it) asks for at least two rest days, the easy pace is the pace at a fixed
    heart rate of the last three runs instead of the last run's average, and the trend of that pace is reported. Heuristics, not
    validated training science. `rows` is compute_all's result and `summ` summary()'s (both may be empty)."""
    hist = sorted(history, key=core.session_key)
    if not hist:
        return None
    last = hist[-1]
    rec = core.compute_recommendations(history, last)
    today = today or date.today()
    since = (today - date.fromisoformat(core.session_key(last)[0])).days
    rest, notes, pace = rec["rest"], list(rec["notes"]), rec["pace_s"]
    load = summ.get("load") if summ else None
    if load and load["chronic"] > 0 and summ["status"][0] != "Not enough history":
        ratio = load["acute"] / load["chronic"]
        if ratio > 1.4 and load["acute"] >= LOW_WEEKLY_TSS:
            rest = max(rest, 2)
            notes.append(("\u26a0", f"Load ratio {ratio:.2f}: the last 7 days are well above your 4-week average"))
        elif ratio < 0.8:
            notes.append(("\u2022", f"Load ratio {ratio:.2f}: the last 7 days are below your 4-week average, there is room to run more"))
    easy = [r["pace_at_hr"] for r in rows if r.get("pace_at_hr")]
    if easy:
        base = rec["base_pace_s"]
        pace = statistics.median(easy[-3:]) + (rec["pace_s"] - base)           # the core's +-5 s adjustment is kept
        hr = rows[-1]["hr_ref"]
        notes.append(("\u2022", f"Easy pace {core.format_pace(statistics.median(easy[-3:]))}/km: your pace at {hr:.0f} bpm in the last 3 runs"))
        if len(easy) >= 6:
            diff = statistics.median(easy[-3:]) - statistics.median(easy[-6:-3])
            if diff <= -5:
                notes.append(("\u2713", f"Pace at {hr:.0f} bpm is {-diff:.0f} s/km faster than in the 3 runs before"))
            elif diff >= 5:
                notes.append(("\u26a0", f"Pace at {hr:.0f} bpm is {diff:.0f} s/km slower than in the 3 runs before"))
    return {"last": last, "since": since, "rest": rest, "ready_in": max(0, rest - since), "dist_km": rec["dist_km"], "pace_s": pace,
            "ef_trend": rec["ef_trend"], "notes": notes, "easy_pace": bool(easy)}


def next_session_lines(adv):
    if not adv:
        return ["NEXT SESSION: no session in the history."]
    last = adv["last"]
    ready = "ready now" if adv["ready_in"] == 0 else f"ready in {adv['ready_in']} {'day' if adv['ready_in'] == 1 else 'days'}"
    ef = {"improving": "   EF trend \u2191 improving", "declining": "   EF trend \u2193 declining"}.get(adv["ef_trend"], "")
    out = [f"NEXT SESSION (after the run of {last['date']} {last['time']}, {last['dist_km']} km, {adv['since']} {'day' if adv['since'] == 1 else 'days'} ago)",
           f"Rest {adv['rest']} {'day' if adv['rest'] == 1 else 'days'} ({ready})   Distance: {adv['dist_km']:.1f} km   "
           f"Pace: {core.format_pace(adv['pace_s'])}/km{ef}"]
    out += [f"{i}  {t}" for i, t in adv["notes"]]
    return out


# ─── Gauges: values with a reference range ──────────────────────────────────

# VO2max by age decade and sex, the Cooper Institute's percentiles as the Garmin manuals reprint them ("VO2 Max. Standard Ratings",
# reprinted with permission from The Cooper Institute; two manuals read and identical): the lower limits of Fair (40th percentile), Good (60th),
# Excellent (80th) and Superior (95th). Below the first value is Poor. The manuals do not state the unit; it is ml/kg/min.
VO2MAX_NORMS = {
    "M": {20: (41.7, 45.4, 51.1, 55.4), 30: (40.5, 44.0, 48.3, 54.0), 40: (38.5, 42.4, 46.4, 52.5),
          50: (35.6, 39.2, 43.4, 48.9), 60: (32.3, 35.5, 39.5, 45.7), 70: (29.4, 32.3, 36.7, 42.1)},
    "F": {20: (36.1, 39.5, 43.9, 49.6), 30: (34.4, 37.8, 42.4, 47.4), 40: (33.0, 36.3, 39.7, 45.3),
          50: (30.1, 33.0, 36.7, 41.1), 60: (27.5, 30.0, 33.0, 37.8), 70: (25.9, 28.1, 30.9, 36.7)},
}


def vo2max_norm(age, sex):
    """(fair, good, excellent, superior) lower limits for this age and sex, or None outside 20-79 or without a sex."""
    if age is None or sex not in VO2MAX_NORMS or not 20 <= age < 80:
        return None
    return VO2MAX_NORMS[sex][int(age) // 10 * 10]


# A band is (from, to, tone, label, advice). The tones are good / ok / warn / bad / info; the GUI turns them into colours.
LOAD_RATIO_BANDS = [
    (0.0, 0.8, "info", "Low", "You are running less than your usual: fine for a recovery week, detraining if it lasts."),
    (0.8, 1.4, "good", "Optimal", "The last week is in line with your last month: the load builds at a pace the body is used to."),
    (1.4, 2.0, "warn", "High", "You ramped up faster than usual: keep the next runs easy and add rest."),
    (2.0, 3.0, "bad", "Very high", "A big jump over your usual load: the time when overuse injuries show up. Rest, then restart gently."),
]
FORM_BANDS = [
    (-40, -30, "bad", "Heavy fatigue", "Much more load than you are adapted to: risk of overreaching. Ease off."),
    (-30, -10, "good", "Building", "Tired from training, as expected in a block that builds fitness."),
    (-10, 10, "ok", "Neutral", "Neither tired nor fresh: the usual state of regular training."),
    (10, 25, "good", "Fresh", "Rested after a lighter spell: the range aimed at on race day."),
    (25, 40, "warn", "Too rested", "Well below your usual training for a while: fitness is slipping."),
]
DRIFT_BANDS = [
    (-5, 5, "good", "Steady", "The heart rate stayed level at a steady pace: good aerobic endurance for this duration and effort."),
    (5, 10, "ok", "Moderate", "The heart rate rose along the run: heat, dehydration, fatigue or a pace a bit high for the day."),
    (10, 15, "bad", "High", "A strong rise: the effort was above your aerobic limit that day, or you were tired or too hot."),
]
TE_BANDS = [
    (0.0, 1.0, "info", "No effect", "Too short or too easy to train anything: a recovery run."),
    (1.0, 2.0, "ok", "Minor", "A light stimulus: helps recovery, does not build much."),
    (2.0, 3.0, "good", "Maintaining", "Keeps your fitness where it is."),
    (3.0, 4.0, "good", "Improving", "A solid stimulus: this kind of run raises your fitness."),
    (4.0, 5.0, "good", "Highly improving", "A hard session: needs a proper recovery before the next."),
]
HRR_WALKING_LIMIT = 12.0     # bpm: Cole et al., N Engl J Med 1999;341:1351-7, with a cool-down period after the exercise
HRR_LYING_LIMIT = 18.0       # bpm: the limit usually used when the person lies down at once (cited in Jou et al. 2025, discussion), without an age


def hrr_limits(athlete):
    """(limit with a cool-down walk, limit lying down at once) for the heart-rate drop in the first minute after exercise, in bpm. The second is,
    with an age, the 5th percentile of apparently healthy people in Jou et al., Eur J Prev Cardiol 2025;32:32-42 (cycle test with no cool-down, lying down
    at once; 2242 healthy of 9917 people aged 18-85): 36.4 + 0.26 * age - 0.0051 * age^2 - 0.19 * resting heart rate (Table 1, the same for men and
    women), and never below the first. The authors say that lying down gives a bigger drop than the other protocols, so these equations are less
    relevant for a recovery on foot, which is why both limits are kept."""
    age = athlete.get("age")
    if age is None:
        return HRR_WALKING_LIMIT, HRR_LYING_LIMIT
    age = min(85.0, max(18.0, float(age)))
    return HRR_WALKING_LIMIT, max(HRR_WALKING_LIMIT, 36.4 + 0.26 * age - 0.0051 * age * age - 0.19 * athlete["hr_rest"])


def hrr_median(athlete):
    """The median (50th percentile) of the first-minute heart-rate drop for this age, resting heart rate and body mass index, from the same table of Jou
    et al. 2025: 69.1 - 0.0033 * age^2 - 0.21 * resting heart rate - 0.34 * BMI (no linear age term). None without an age or a height."""
    age, height, mass = athlete.get("age"), athlete.get("height_cm"), athlete.get("mass_kg")
    if age is None or not height or not mass:
        return None
    age = min(85.0, max(18.0, float(age)))
    bmi = mass / (height / 100.0) ** 2
    return 69.1 - 0.0033 * age * age - 0.21 * athlete["hr_rest"] - 0.34 * bmi


def hrr_bands(walking, lying, median=None):
    low = (0, walking, "bad", "Below the normal range",
           f"Below {walking:.0f} bpm, the limit even for a recovery with a walk: a blunted recovery, linked in studies to a higher risk of death. It can also "
           "come from fatigue, heat or illness: look at more than one run.")
    mid = (walking, lying, "warn", "Borderline",
           f"Under the {lying:.0f} bpm of a recovery lying down at once, above the {walking:.0f} bpm of one with a cool-down walk: normal if you walked or "
           "stood, low if you lay down.")
    high = (lying, 60, "good", "Normal range",
            "Above the limit for a recovery lying down at once, so normal for any recovery. No band for good or excellent is published: a bigger drop "
            "generally goes with better fitness, so compare it with your own earlier runs.")
    bands = [low, mid, high] if lying > walking else [low, (walking, 60, "good", "Normal range", high[4])]
    if median is not None and median > bands[-1][0] + 1:             # the typical value splits the normal range in two
        top = bands.pop()
        bands += [(top[0], median, "good", "Normal range", top[4]),
                  (median, 60, "great", "Above the median", "Above the median of healthy people of your age, resting heart rate and body mass index: a faster "
                                                            "recovery than the typical one. No band for excellent is published, so compare it with your own earlier runs.")]
    return bands


INTENSITY_BANDS = [
    (40, 50, "info", "Very light", "Below zone 1: walking or a very easy jog."),
    (50, 60, "ok", "Zone 1", "Warm-up or recovery effort."),
    (60, 70, "good", "Zone 2", "Easy aerobic running: where most of your runs should be."),
    (70, 80, "ok", "Zone 3", "Moderate: a steady effort, harder than an easy run."),
    (80, 90, "warn", "Zone 4", "Hard: near the threshold. Fine now and then, not for every run."),
    (90, 100, "bad", "Zone 5", "Maximal effort."),
]
CONDITION_BANDS = [
    (-20, -10, "bad", "Much below usual", "Much less efficient than in your last runs: tired, ill, or very hot."),
    (-10, -5, "warn", "Below usual", "Less efficient than usual: heat, poor sleep or fatigue are the usual reasons."),
    (-5, 5, "good", "Usual", "As efficient as in your last runs."),
    (5, 20, "good", "Better than usual", "More efficient than in your last runs: fresh, or the fitness is rising."),
]


def _gauge(key, title, value, text, lo, hi, bands, why, source, level, extra=""):
    """One gauge: the value on the scale lo..hi, the band it falls in, and what to make of it. `level` says how far the reference
    range can be trusted: 'sourced', 'secondary' (the source was not read in full) or 'mine' (a rule of thumb of this program)."""
    band = None
    if value is not None:
        band = next((b for b in bands if b[0] <= value < b[1]), bands[-1] if value >= bands[-1][1] else bands[0])
    return {"key": key, "title": title, "value": value, "text": text, "lo": lo, "hi": hi, "bands": bands,
            "tone": band[2] if band else "none", "label": band[3] if band else "No data",
            "advice": (band[4] + (" " + extra if extra else "")) if band else extra, "why": why, "source": source, "level": level}


def vo2_gauge(summ, athlete):
    """The VO2max against the Cooper Institute's norms for the athlete's age and sex (athlete.json: `age`, `sex`)."""
    vo2 = (summ or {}).get("vo2max")
    rng = (summ or {}).get("vo2max_range")
    norm = vo2max_norm(athlete.get("age"), athlete.get("sex"))
    why = ("The VO2max estimated from your heart rate and speed (median of the last five runs), against the VO2max of people of your age and "
           "sex. It is an estimate that is off by 5-10% or more, and your runs are slower than the equation was made for, so read it as a "
           "trend and a rough position, not as a lab result.")
    source = ("Cooper Institute percentiles, as reprinted in the Garmin manuals ('VO2 Max. Standard Ratings', two manuals read, identical); "
              "the original tables were not read.")
    if norm is None:
        why_not = ("Needs your age (20-79) and sex: set them with the Athlete button at the top." if vo2 else "")
        flat = [(20, 60, "info", "", "")]
        return _gauge("vo2max", "VO2max (estimate)", None, f"{vo2:.1f}" if vo2 else "-", 20, 60, flat, why, source, "secondary",
                      why_not or "Not enough steady running yet to estimate it.")
    fair, good, excellent, superior = norm
    hi = round(superior + 8)
    bands = [
        (20, fair, "bad", "Poor", "Below the 40th percentile of people of your age and sex."),
        (fair, good, "warn", "Fair", "Between the 40th and the 60th percentile of people of your age and sex."),
        (good, excellent, "good", "Good", "Between the 60th and the 80th percentile: better than most of your age group."),
        (excellent, superior, "great", "Excellent", "Between the 80th and the 95th percentile."),
        (superior, hi, "great", "Superior", "Above the 95th percentile."),
    ]
    extra = ""
    if vo2 and rng:
        extra = (f"Your last runs gave {rng[0]:.0f}-{rng[1]:.0f}; with an error of that size the true band could be one step either side.")
    return _gauge("vo2max", "VO2max (estimate)", vo2, f"{vo2:.1f}" if vo2 else "-", 20, hi, bands, why, source, "secondary", extra)


def gauges(m, session, summ):
    """The values that have a reference range, as gauge descriptions for the GUI. `m` is the run's metrics (compute_all's row), `session`
    its history row, `summ` summary()'s result (the load and the athlete). Any of them can be None: the gauge then says there is no data."""
    m, session = m or {}, session or {}
    athlete = (summ or {}).get("athlete") or DEFAULT_ATHLETE
    load = (summ or {}).get("load")
    ready = bool(load and load["chronic"] > 0 and summ["status"][0] != "Not enough history")
    ratio = load["acute"] / load["chronic"] if ready else None
    low_base = ""
    if ratio is not None and ratio > 1.4 and load["acute"] < LOW_WEEKLY_TSS:
        low_base = f"With only {load['acute']:.0f} TSS in 7 days the base is small, so this is a ramp-up from little, not a spike."
    drift = core.safe_float(session.get("drift_pct"))
    avg_hr = core.safe_float(session.get("avg_hr_bpm"))
    reps = m.get("reps") or []
    hrr_walk, hrr_lie = hrr_limits(athlete)
    hrr_med = hrr_median(athlete)
    if athlete.get("age") is not None:
        hrr_lie_text = (f"{hrr_lie:.0f} bpm at your age and resting heart rate (the 5th percentile of 2,242 healthy people in a Swedish study of 9,917, Jou et al. 2025)"
                        if hrr_lie > hrr_walk else f"{hrr_walk:.0f} bpm too at your age")
    else:
        hrr_lie_text = f"{hrr_lie:.0f} bpm (set your age with the Athlete button to get one for your age and resting heart rate)"
    hrr_why = (f"How the heart rate recovers depends on how you recover. The published limit is {hrr_walk:.0f} bpm with a slow cool-down walk (Cole et al. 1999) and, "
               f"lying down at once, {hrr_lie_text}. Lying down gives a bigger drop than the other protocols, so on foot a value between the two limits is still normal."
               + (f" The typical (median) drop of healthy people like you is {hrr_med:.0f} bpm lying down." if hrr_med else
                  " Give your height with the Athlete button to also see the typical (median) drop for your age."))
    hrr_source = ("Cole et al., N Engl J Med 1999;341:1351-7 (PubMed 10536127): 12 bpm, with a cool-down. Jou et al., Eur J Prev Cardiol 2025;32:32-42, "
                  "doi 10.1093/eurjpc/zwae308 (full text read): 5th percentile lying down = 36.4 + 0.26 age - 0.0051 age^2 - 0.19 resting heart rate, the same for "
                  "men and women; the median is 69.1 - 0.0033 age^2 - 0.21 resting heart rate - 0.34 body mass index (needs your height).")
    not_steady = "Not for an interval workout: the heart rate rises and falls by design."
    if reps:
        n_before = m.get("perf_n", 0)
        condition_extra = ("Efficiency of the repetitions against your earlier interval workouts." if m.get("perf_pct") is not None else
                           f"Needs at least 3 earlier interval workouts to compare with (there are {n_before}).")
        hrr_extra = f"It is the mean of the {m['hrr60_n']} recoveries after a repetition: the drop from the peak at the end of the effort to 60 s later (after a repetition you walk, and the published limits differ with how people recover)." if m.get("hrr60_n") else ""
    if reps:                                           # the average of the whole run would count the recoveries
        drift = None
        avg_hr = sum(r["hr"] * r["dur"] for r in reps if r["hr"]) / sum(r["dur"] for r in reps if r["hr"]) if any(r["hr"] for r in reps) else None
    pct = avg_hr / athlete["hr_max"] * 100 if avg_hr else None
    return [
        _gauge("load_ratio", "Load ratio (now)", ratio, f"{ratio:.2f}" if ratio is not None else "-", 0.0, 3.0, LOAD_RATIO_BANDS,
               "The training load of the last 7 days divided by the weekly average of the last 28 days. It says whether you are asking the body "
               "for more than it is used to. Needs about four weeks of history. Shown for today.",
               "Garmin manual, Training Status (load ratio table); the bands are joined at 1.4 and 2.0.", "sourced", low_base),
        _gauge("form", "Form, TSB (now)", load["tsb"] if load else None, f"{load['tsb']:+.0f}" if load else "-", -40, 40, FORM_BANDS,
               "Fitness (the 42-day average of the load) minus fatigue (the 7-day average). Negative means more tired than trained for, "
               "positive means rested. It is not a performance predictor. Shown for today.",
               "Friel, TrainingPeaks blog 'Applying the numbers, part 3' (race day +15 to +25) and a third-party table for the other bands.",
               "secondary"),
        _gauge("drift", "Heart-rate drift", drift, f"{drift:+.1f} %" if drift is not None else "-", -5, 15, DRIFT_BANDS,
               "How much the heart rate rose from the first to the second half of the run (warm-up and final sprint left out). At a steady pace "
               "it is the same as aerobic decoupling; if you sped up or slowed down it also mixes in the pace.",
               "TrainingPeaks, 'Aerobic endurance and decoupling': under 5% strong, 5-10% moderate, over 10% limited. The advice of this "
               "program uses its own 6% and 8% thresholds.", "secondary", not_steady if reps else ""),
        _gauge("intensity", "Average heart rate (% of max)", pct, f"{pct:.0f} %" if pct is not None else "-", 40, 100, INTENSITY_BANDS,
               "The average heart rate of the run as a percentage of your maximum, in the program's zones (Garmin's default percentages). "
               "It tells how hard the run was on average, not whether it was good.",
               "Zone percentages: Garmin's defaults, as the program uses them (from memory, not re-read).", "mine",
               "Over the repetitions only: the recoveries are left out." if reps else ""),
        _gauge("te", "Training effect (indicative)", m.get("te"), f"{m['te']:.1f}" if m.get("te") is not None else "-", 0.0, 5.0, TE_BANDS,
               "How much the run trains the aerobic system, from 0 to 5, computed from the time in the heart-rate zones. The 0-5 reading is "
               "Firstbeat's; this value is NOT their calculation and is not calibrated against a watch.",
               "Firstbeat white paper 'EPOC based training effect assessment' (the bands); the value is the program's own.", "sourced"),
        vo2_gauge(summ, athlete),
        _gauge("condition", "Performance condition", m.get("perf_pct"), f"{m['perf_pct']:+.1f} %" if m.get("perf_pct") is not None else "-", -20, 20,
               CONDITION_BANDS,
               "The efficiency (speed per heartbeat) of minutes 6-20 of the run against the median of your previous ten runs; for an interval workout, "
               "of the repetitions against your earlier interval workouts (only comparable between workouts of a similar structure). Heat, sleep, "
               "hills and caffeine all move it.",
               "A scale of this program, shaped like Garmin's -20 to +20; the bands at +-5 and +-10 are rules of thumb.", "mine",
               condition_extra if reps else ""),
        _gauge("hrr", "Heart-rate recovery, 60 s", m.get("hrr60"), f"{m['hrr60']:.0f} bpm" if m.get("hrr60") is not None else "-", 0, 60,
               hrr_bands(hrr_walk, hrr_lie, hrr_med),
               "How many beats per minute the heart rate dropped in the minute after the effort (after the run; in an interval workout, after each "
               "repetition). " + hrr_why,
               hrr_source, "sourced", hrr_extra if reps else ""),
    ]


# ─── Structured view of the Fitness tab ──────────────────────────────────────

STATUS_TONE = {"Productive": "good", "Maintaining": "ok", "Recovery": "info", "Unproductive": "warn", "Ramping up": "warn",
               "Strained": "bad", "Overreaching": "bad", "Detraining": "warn", "Not enough history": "info"}


def _days(n):
    return f"{n} {'day' if n == 1 else 'days'}"


def dashboard(adv, summ):
    """What the top of the Fitness tab shows, as plain data: {next, load, vo2, races, best, footer}. Each card is {headline, tone, sub, rows [(label,
    value)], notes}; None where there is nothing to show. `adv` is next_session()'s result, `summ` summary()'s."""
    out = {"next": None, "load": None, "vo2": None, "races": None, "best": [], "footer": ""}
    if adv:
        last = adv["last"]
        ready = adv["ready_in"] == 0
        trend = {"improving": "↑ improving", "declining": "↓ declining"}.get(adv["ef_trend"], "-")
        kind = "interval workout" if last.get("kind") == "intervals" else "run"
        out["next"] = {"headline": "Ready now" if ready else f"Ready in {_days(adv['ready_in'])}", "tone": "good" if ready else "warn",
                       "sub": f"Rest {_days(adv['rest'])} after the {kind} of {last['date']} {last['time']} ({last['dist_km']} km, {_days(adv['since'])} ago)",
                       "rows": [("Distance", f"{adv['dist_km']:.1f} km"), ("Pace", f"{core.format_pace(adv['pace_s'])} /km"), ("Efficiency trend", trend)],
                       "notes": list(adv["notes"])}
    if not summ:
        return out
    a = summ["athlete"]
    L = summ["load"]
    status, reason = summ["status"]
    rows = []
    if L:
        ratio = f"{L['acute'] / L['chronic']:.2f}" if L["chronic"] > 0 else "-"
        rows = [("Load ratio", ratio), ("Fitness (CTL)", f"{L['ctl']:.1f}"), ("Fatigue (ATL)", f"{L['atl']:.1f}"), ("Form (TSB)", f"{L['tsb']:+.1f}"),
                ("Load, last 7 days", f"{L['acute']:.0f} TSS")]
    out["load"] = {"headline": status, "tone": STATUS_TONE.get(status, "info"), "sub": reason, "rows": rows,
                   "notes": [("•", f"As of {L['date']}.")] if L else []}
    if "vo2max" in summ:
        lo, hi = summ["vo2max_range"]
        vrows, notes = [], [("•", "Estimated from heart rate and speed on steady stretches: read the trend, not the decimals.")]
        if "vo2max_hrmax_range" in summ:
            vrows.append((f"If the max HR is {HRMAX_MARGIN} bpm off", f"{summ['vo2max_hrmax_range'][0]:.0f}-{summ['vo2max_hrmax_range'][1]:.0f}"))
            notes.append(("⚠", "The maximum heart rate is an estimate: the zones, the TRIMP and the training effect move with it."))
        out["vo2"] = {"headline": f"{summ['vo2max']:.1f}", "unit": "ml/kg/min", "tone": "info",
                      "sub": f"median of the last runs, range {lo:.1f}-{hi:.1f}", "rows": vrows, "notes": notes}
    else:
        out["vo2"] = {"headline": "-", "unit": "", "tone": "info", "sub": "not enough steady stretches yet", "rows": [], "notes": []}
    if "vdot_races" in summ or "riegel_races" in summ:
        vd, rg = summ.get("vdot_races", {}), summ.get("riegel_races", {})
        head = ("From the VO2max", "From your best " + summ["riegel_from"][0] if "riegel_from" in summ else "-")
        notes = []
        if vd:
            notes.append(("•", "VO2max column (Daniels VDOT): what a trained runner with this VO2max would run, optimistic for slow, non-racing runners."))
        if rg:
            label, secs, day = summ["riegel_from"]
            notes.append(("•", f"Best {label} column (Riegel): from {fmt_time(secs)} on {day}; slow, because training is not racing."))
        out["races"] = {"head": head, "rows": [(k, fmt_time(vd[k]) if k in vd else "-", fmt_time(rg[k]) if k in rg else "-") for k, _ in RACES], "notes": notes}
    out["best"] = [(k, fmt_time(v[0]), f"{v[1][8:]}/{v[1][5:7]}") for k, v in summ["best"].items()]
    out["footer"] = (f"Parameters from {ATHLETE_FILE}: max HR {a['hr_max']}{' (estimated)' if a.get('hr_max_estimated') else ''}, "
                     f"resting HR {a['hr_rest']}, mass {a['mass_kg']} kg. * indicative: not calibrated against a Garmin. "
                     "Every value is an estimate: read the trends, not the decimals.")
    return out


def run_panel(m, row=None, details=None):
    """What the summary of one run shows. `m`: the metrics of the run (compute_all's row), or None while they are being computed; `row`: its history
    row (the facts: duration, pace, heart rate, TSS, EF, drift, weather); `details`: {"alt": (min, max), "notes": [(icon, text)]} from reading the
    CSV (the altitude, a merge, a sprint, the weather warnings). Returns {title, subtitle, badge, facts [(label, value)], tiles [(label, value)],
    zones (minutes in Z1-Z5), reps [rows], notes, warning, has_metrics}."""
    details = details or {}
    dash = lambda v, f: "-" if v is None else f(v)
    notes = list(details.get("notes") or [])
    facts = []
    if row:
        temp, hum, wind = (core.safe_float(row.get(k)) for k in ("temp_c", "humidity_pct", "wind_kmh"))
        weather = "-" if temp is None else f"{temp:g} \u00b0C" + (f", {hum:g}%" if hum is not None else "") + (f", {wind:g} km/h" if wind is not None else "")
        alt = details.get("alt")
        facts = [("Duration", row.get("duration_hms") or "-"), ("Pace", f"{row['pace_minkm']} /km" if row.get("pace_minkm") else "-"),
                 ("Average HR", f"{row['avg_hr_bpm']} bpm" if row.get("avg_hr_bpm") else "-"), ("TSS", row.get("tss") or "-"),
                 ("EF", row.get("ef_m_beat") or "-"), ("Drift", f"{row['drift_pct']} %" if row.get("drift_pct") not in ("", None) else "-"),
                 ("Weather", weather), ("Altitude", f"{alt[0]:.0f} to {alt[1]:.0f} m" if alt else "-")]
    tiles, zones, reps, warning, badge = [], [0.0] * 5, [], "", ""
    if m:
        tiles = [("Training effect*", f"{m['te']:.1f}"), ("TRIMP", f"{m['trimp']:.0f}"), ("Recovery time*", f"{m['recovery_h']:.0f} h"),
                 ("VO2max", dash(m["vo2max"], lambda v: f"{v:.1f}")),
                 (f"Pace at {m['hr_ref']:.0f} bpm", dash(m["pace_at_hr"], lambda v: core.format_pace(v) + " /km")),
                 ("HR at the usual speed", dash(m["hr_at_speed"], lambda v: f"{v:.0f} bpm")),
                 ("Cadence", dash(m["spm"], lambda v: f"{v:.0f} /min")), ("Step length", dash(m["step_m"], lambda v: f"{v:.2f} m")),
                 ("Power*", dash(m["power_w"], lambda v: f"{v:.0f} W")),
                 ("HR recovery, 60 s", dash(m["hrr60"], lambda v: f"{v:.0f} bpm")),
                 ("Performance condition", dash(m.get("perf_pct"), lambda v: f"{v:+.1f} %"))]
        zones = [m["zone_s"][k] / 60.0 for k in range(1, 6)]
        reps = [(k, fmt_time(r["start"]), fmt_time(r["dur"]), f"{r['dist']:.0f}", core.format_pace(r["pace"]) if r["pace"] else "-",
                 f"{r['hr']:.0f}" if r["hr"] else "-", f"{r['hr_max']:.0f}" if r["hr_max"] else "-", "-" if r["drop60"] is None else f"{r['drop60']:.0f}")
                for k, r in enumerate(m.get("reps") or [], 1)]
        badge = "INTERVALS" if m.get("reps") else ""
        if m["moving_s"]:
            notes.append(("\u2022", f"Moving time {fmt_time(m['moving_s'])} of {fmt_time(m['elapsed_s'])}."))
        if m.get("reps"):
            work = sum(r["dur"] for r in m["reps"])
            notes.append(("\u2022", f"{len(m['reps'])} repetitions, {fmt_time(work)} of work ({work / m['elapsed_s'] * 100:.0f}% of the run). VO2max, pace at a heart rate, "
                                    "efficiency and drift need a steady effort and are not computed."))
            first, last = m["reps"][0], m["reps"][-1]
            if len(m["reps"]) >= 2 and first["pace"] and last["pace"] and first["hr"] and last["hr"]:
                notes.append(("\u2022", f"First to last repetition: pace {last['pace'] - first['pace']:+.0f} s/km, heart rate {last['hr'] - first['hr']:+.0f} bpm."))
            if m.get("hrr60_n"):
                notes.append(("\u2022", f"Heart-rate recovery: mean of the {m['hrr60_n']} recoveries after a repetition, from the peak."))
            if m.get("perf_pct") is None:
                notes.append(("\u2022", f"Performance condition needs at least 3 earlier interval workouts to compare with ({m.get('perf_n', 0)} so far)."))
        if m.get("split_balance_pct") is not None:
            notes.append(("\u2022", f"Second half {m['split_balance_pct']:+.1f}% vs the first" +
                          (f", km times vary {m['pace_cv_pct']:.1f}%." if m.get("pace_cv_pct") is not None else ".")))
        if m.get("hr_rise_s") is not None:
            notes.append(("\u2022", f"Heart-rate rise time (63%): {m['hr_rise_s']:.0f} s."))
        if m.get("best"):
            notes.append(("\u2022", "Best in this run: " + ", ".join(f"{k} {fmt_time(v[0])}" for k, v in m["best"].items()) + "."))
        for e in m.get("efforts") or []:
            notes.append(("\u2022", f"Effort at {fmt_time(e['start'])}: {fmt_time(e['dur'])}, {e['dist']:.0f} m" + (f", {core.format_pace(e['pace'])}/km" if e["pace"] else "")
                          + (f", HR {e['hr']:.0f}" if e["hr"] else "")))
        if m.get("hr_clipped_s"):
            warning = (f"\u26a0 The heart rate sits at its maximum for {m['hr_clipped_s']} s: a clipped recording, left out of the metrics (zones, TRIMP and training "
                       "effect are a minimum). Point the Health Sync folder at the FIT files to get the real values.")
        d = m["date"]
        title, subtitle = f"{d[8:]}/{d[5:7]}/{d[:4]}  {m['time']}", f"{m['dist_km'] or 0:.2f} km"
    elif row:
        title, subtitle = f"{row['date']}  {row['time']}", f"{row['dist_km']} km"
    else:
        return None
    return {"title": title, "subtitle": subtitle, "badge": badge, "facts": facts, "tiles": tiles, "zones": zones, "reps": reps, "notes": notes,
            "warning": warning, "has_metrics": bool(m)}


# ─── Text for the GUI ────────────────────────────────────────────────────────

def run_lines(m):
    """A few lines about one run for the History detail."""
    lines = []
    if m["moving_s"]:
        lines.append(f"Moving time {fmt_time(m['moving_s'])} of {fmt_time(m['elapsed_s'])}")
    z = m["zone_s"]
    if m.get("hr_clipped_s"):
        lines.append(f"⚠ The heart rate sits at its maximum for {m['hr_clipped_s']} s: a clipped recording, left out of the metrics "
                     "(zones, TRIMP and training effect are a minimum). Point the Health Sync folder at the FIT files to get the real values.")
    if m.get("reps"):
        reps = m["reps"]
        work = sum(r["dur"] for r in reps)
        lines.append(f"INTERVAL WORKOUT: {len(reps)} repetitions, {fmt_time(work)} of work in {fmt_time(m['elapsed_s'])} ({work / m['elapsed_s'] * 100:.0f}%). "
                     "VO2max, pace at a heart rate, efficiency and drift are not computed: they need a steady effort.")
        for i, r in enumerate(reps, 1):
            lines.append(f"  Rep {i} at {fmt_time(r['start'])}: {fmt_time(r['dur'])}, {r['dist']:.0f} m" +
                         (f", {core.format_pace(r['pace'])}/km" if r["pace"] else "") + (f", HR {r['hr']:.0f} (max {r['hr_max']:.0f})" if r["hr"] else "") +
                         (f", HR down {r['drop60']:.0f} bpm in the next 60 s" if r["drop60"] is not None else ""))
        if len(reps) >= 2 and reps[0]["pace"] and reps[-1]["pace"] and reps[0]["hr"] and reps[-1]["hr"]:
            lines.append(f"  First to last: pace {reps[-1]['pace'] - reps[0]['pace']:+.0f} s/km, heart rate {reps[-1]['hr'] - reps[0]['hr']:+.0f} bpm")
    lines.append("Zones (min): " + "  ".join(f"Z{i} {z[i] / 60:.0f}" for i in range(1, 6)) + f"   TRIMP {m['trimp']:.0f}")
    lines.append(f"Training effect ~{m['te']:.1f} (indicative), recovery ~{m['recovery_h']:.0f} h (indicative)")
    if m["vo2max"]:
        lines.append(f"VO2max estimate {m['vo2max']:.1f} ml/kg/min")
    if m["pace_at_hr"]:
        lines.append(f"Pace at {m['hr_ref']:.0f} bpm (+-5): {core.format_pace(m['pace_at_hr'])}/km")
    if m["hr_at_speed"]:
        lines.append(f"HR at the usual speed: {m['hr_at_speed']:.0f} bpm")
    if m["spm"]:
        lines.append(f"Cadence {m['spm']:.0f} steps/min (CSV x{m['cadence_factor']}), step {m['step_m']:.2f} m")
    if m["power_w"]:
        lines.append(f"Power ~{m['power_w']:.0f} W ({m['power_wkg']:.2f} W/kg), a rough model")
    if m.get("perf_pct") is not None:
        lines.append(f"Performance condition {m['perf_pct']:+.1f}% (" + ("efficiency of the repetitions against your earlier interval workouts)"
                     if m.get("reps") else "efficiency of minutes 6-20 against your last runs)"))
    elif m.get("reps"):
        lines.append(f"Performance condition: needs at least 3 earlier interval workouts to compare the repetitions with ({m.get('perf_n', 0)} so far)")
    if m["hrr60"] is not None:
        lines.append(f"HR recovery in 60 s: {m['hrr60']:.0f} bpm" + (f" (mean of {m['hrr60_n']} recoveries after a repetition, from the peak)" if m.get("hrr60_n") else ""))
    if m["hr_rise_s"] is not None:
        lines.append(f"HR rise time (63%): {m['hr_rise_s']:.0f} s")
    if m["split_balance_pct"] is not None:
        lines.append(f"Second half {m['split_balance_pct']:+.1f}% vs the first" +
                     (f", km times vary {m['pace_cv_pct']:.1f}%" if m["pace_cv_pct"] is not None else ""))
    for label, (secs, _) in m["best"].items():
        lines.append(f"Best {label}: {fmt_time(secs)} ({core.format_pace(secs / dict(BEST_DISTANCES)[label] * 1000)}/km)")
    for e in m["efforts"]:
        lines.append(f"Effort at {fmt_time(e['start'])}: {fmt_time(e['dur'])}, {e['dist']:.0f} m" +
                     (f", {core.format_pace(e['pace'])}/km" if e["pace"] else "") + (f", HR {e['hr']:.0f}" if e["hr"] else ""))
    return lines


def summary_lines(s):
    """The text of the Fitness tab."""
    a = s["athlete"]
    out = []
    if s["load"]:
        L = s["load"]
        ratio = f"{L['acute'] / L['chronic']:.2f}" if L["chronic"] > 0 else "-"
        out.append(f"Training load on {L['date']}:  Fitness (CTL) {L['ctl']:.1f}   Fatigue (ATL) {L['atl']:.1f}   "
                   f"Form (TSB) {L['tsb']:+.1f}   load ratio {ratio}")
    out.append(f"Training status: {s['status'][0]} ({s['status'][1]})")
    if "vo2max" in s:
        lo, hi = s["vo2max_range"]
        out.append(f"VO2max estimate: {s['vo2max']:.1f} ml/kg/min (median of the last runs, range {lo:.1f}-{hi:.1f})")
        if "vo2max_hrmax_range" in s:
            a_lo, a_hi = s["vo2max_hrmax_range"]
            out.append(f"   the maximum heart rate is an estimate: if it is {HRMAX_MARGIN} bpm off, the VO2max is {a_lo:.1f} to {a_hi:.1f}, "
                       "and the zones, the TRIMP and the training effect move with it")
        out.append("Race predictions from the VO2max (Daniels VDOT: what a trained runner with this VO2max would run, optimistic for slow, "
                   "non-racing runners): " +
                   "   ".join(f"{k} {fmt_time(v)}" for k, v in s["vdot_races"].items()))
    else:
        out.append("VO2max estimate: not enough steady stretches yet")
    if "riegel_from" in s:
        label, secs, day = s["riegel_from"]
        out.append(f"Race predictions from your best {label} of the last 90 days, {fmt_time(secs)} on {day} (Riegel; slow, because "
                   "training is not racing): " + "   ".join(f"{k} {fmt_time(v)}" for k, v in s["riegel_races"].items()))
    if s["best"]:
        out.append("Best efforts: " + "   ".join(f"{k} {fmt_time(v[0])} ({v[1][8:]}/{v[1][5:7]})" for k, v in s["best"].items()))
    out.append(f"Parameters from {ATHLETE_FILE}: max HR {a['hr_max']}{' (estimated)' if a.get('hr_max_estimated') else ''}, "
               f"resting HR {a['hr_rest']}, mass {a['mass_kg']} kg.   "
               "Every value here is an estimate: read the trends, not the decimals.")
    return out
