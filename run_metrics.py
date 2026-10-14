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
CACHE_VERSION = 3                        # bump when a formula changes: the cached per-run metrics are recomputed
DEFAULT_ATHLETE = {
    "hr_max": 190,                       # bpm
    "hr_rest": 60,                       # bpm
    "mass_kg": 70,
    "cadence_factor": None,              # 1 or 2: the CSV cadence is per foot when ~88 (x2 = steps/min); None = decide from the data
    "hr_ref": None,                      # heart rate used for "pace at a fixed heart rate"; None = 60% of the heart-rate reserve
    "hr_max_estimated": None,            # True when hr_max is a formula or a guess, not measured: the VO2max is then also given for hr_max +- 15
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

def run_metrics(pts, athlete, hr_ref=None, speed_ref=None):
    """All the per-run metrics as a flat dict (None where the data does not allow them). `speed_ref` (m/s) is the speed for
    'heart rate at a fixed speed'; the caller takes it from the history (the median pace)."""
    S = series(pts)
    hr_ref = hr_ref or athlete["hr_ref"] or athlete["hr_rest"] + 0.6 * (athlete["hr_max"] - athlete["hr_rest"])
    zones = zone_seconds(S, athlete)
    trimp = edwards_trimp(zones)
    te = aerobic_te(trimp)
    spm, step, factor = cadence_stats(S, athlete)
    watts, wkg = power_estimate(S, athlete)
    pace_hr = pace_at_hr(S, hr_ref)
    hrs = [h for h in S["hr"] if h is not None]
    guess = athlete.get("hr_max_estimated")
    return {
        "vo2max_if_hrmax_low": vo2max_estimate(S, dict(athlete, hr_max=athlete["hr_max"] - HRMAX_MARGIN)) if guess else None,
        "vo2max_if_hrmax_high": vo2max_estimate(S, dict(athlete, hr_max=athlete["hr_max"] + HRMAX_MARGIN)) if guess else None,
        "moving_s": sum(dt for dt, ok in zip(S["dt"], S["moving"]) if ok),
        "elapsed_s": S["t"][-1],
        "zone_s": zones, "trimp": trimp, "te": te, "recovery_h": recovery_hours(te),
        "vo2max": vo2max_estimate(S, athlete),
        "hr_ref": hr_ref, "pace_at_hr": pace_hr,
        "hr_at_speed": hr_at_speed(S, speed_ref) if speed_ref else None,
        "ef_window": ef_window(S),
        "spm": spm, "step_m": step, "cadence_factor": factor,
        "power_w": watts, "power_wkg": wkg,
        "hrr60": hr_recovery(S), "hr_rise_s": hr_rise_time(S),
        "split_balance_pct": split_balance(S), "pace_cv_pct": pace_variability(S),
        "hr_max_seen": max(hrs) if hrs else None, "hr_clipped_s": S["clipped_s"],
        "best": {k: [v[0], v[1]] for k, v in best_efforts(S).items()},
        "efforts": find_efforts(S),
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


def compute_all(data_dir, history, athlete, progress=None):
    """Per-run metrics for every history row that has a CSV, newest last, with the performance condition and the load added.
    The per-run results are cached in `run_metrics_cache.json` (keyed by file, modification time, athlete and reference speed)."""
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
    merge_stamp = 0.0
    try:
        merge_stamp = os.path.getmtime(os.path.join(data_dir, core.MERGE_FILE))
    except OSError:
        pass
    rows, dirty = [], False
    ordered = sorted(history, key=core.session_key)
    for n, row in enumerate(ordered):
        path = core.session_file(data_dir, row)
        if not path:
            continue
        fit = core.fit_path(path)                                       # the heart rate comes from the FIT when there is one
        per_run = {k: v for k, v in athlete.items() if k not in ("age", "sex")}               # these two only place the VO2max in the gauges
        stamp = [os.path.getmtime(path), merge_stamp, per_run, speed_ref and round(speed_ref, 3), fit and os.path.getmtime(fit)]
        name = os.path.basename(path)
        hit = entries.get(name)
        if hit and hit.get("stamp") == json.loads(json.dumps(stamp)):
            m = hit["metrics"]
        else:
            try:
                m = run_metrics(core.analyze_file(path, fetch=False)["points"], athlete, speed_ref=speed_ref)
            except (ValueError, OSError):
                continue
            entries[name] = {"stamp": stamp, "metrics": m}
            dirty = True
        m = dict(m)
        m.update(date=core.session_key(row)[0], time=core.session_key(row)[1], file=name,
                 tss=core.safe_float(row.get("tss")), dist_km=core.safe_float(row.get("dist_km")))
        rows.append(m)
        if progress:
            progress(n + 1, len(ordered))
    if dirty:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except OSError:
            pass
    # performance condition: the efficiency of minutes 6-20 against the median of the previous ten runs, in %
    for i, m in enumerate(rows):
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
        base = last.get("_pace_s") or core.parse_pace(last.get("pace_minkm")) or 390
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
HRR_BANDS = [
    (0, 12, "bad", "Slow", "In Cole's 1999 study of patients a drop of 12 bpm or less in the first minute after exercise was linked to a higher risk of death."),
    (12, 60, "good", "Not abnormal", "The heart rate fell faster than that threshold. A faster fall generally goes with better fitness."),
]
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
    pct = avg_hr / athlete["hr_max"] * 100 if avg_hr else None
    return [
        _gauge("load_ratio", "Load ratio (now)", ratio, f"{ratio:.2f}" if ratio is not None else "-", 0.0, 3.0, LOAD_RATIO_BANDS,
               "The training load of the last 7 days divided by the weekly average of the last 28 days. It says whether you are asking the body "
               "for more than it is used to. Needs about four weeks of history. Shown for today, not for the run chosen above.",
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
               "program uses its own 6% and 8% thresholds.", "secondary"),
        _gauge("intensity", "Average heart rate (% of max)", pct, f"{pct:.0f} %" if pct is not None else "-", 40, 100, INTENSITY_BANDS,
               "The average heart rate of the run as a percentage of your maximum, in the program's zones (Garmin's default percentages). "
               "It tells how hard the run was on average, not whether it was good.",
               "Zone percentages: Garmin's defaults, as the program uses them (from memory, not re-read).", "mine"),
        _gauge("te", "Training effect (indicative)", m.get("te"), f"{m['te']:.1f}" if m.get("te") is not None else "-", 0.0, 5.0, TE_BANDS,
               "How much the run trains the aerobic system, from 0 to 5, computed from the time in the heart-rate zones. The 0-5 reading is "
               "Firstbeat's; this value is NOT their calculation and is not calibrated against a watch.",
               "Firstbeat white paper 'EPOC based training effect assessment' (the bands); the value is the program's own.", "sourced"),
        vo2_gauge(summ, athlete),
        _gauge("condition", "Performance condition", m.get("perf_pct"), f"{m['perf_pct']:+.1f} %" if m.get("perf_pct") is not None else "-", -20, 20,
               CONDITION_BANDS,
               "The efficiency (speed per heartbeat) of minutes 6-20 of the run against the median of your previous ten runs. Heat, sleep, "
               "hills and caffeine all move it.",
               "A scale of this program, shaped like Garmin's -20 to +20; the bands at +-5 and +-10 are rules of thumb.", "mine"),
        _gauge("hrr", "Heart-rate recovery, 60 s", m.get("hrr60"), f"{m['hrr60']:.0f} bpm" if m.get("hrr60") is not None else "-", 0, 60,
               HRR_BANDS,
               "How many beats per minute the heart rate dropped in the minute after the run ended. The study measured it right after a "
               "maximal treadmill test, and your run usually ends at an easy effort and a few steps of walking, so read it as a trend.",
               "Cole et al., N Engl J Med 1999;341:1351-7 (PubMed 10536127): 12 bpm or less is abnormal. Only the lower limit is a published one.",
               "sourced"),
    ]


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
        lines.append(f"Performance condition {m['perf_pct']:+.1f}% (efficiency of minutes 6-20 against your last runs)")
    if m["hrr60"] is not None:
        lines.append(f"HR recovery in 60 s: {m['hrr60']:.0f} bpm")
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
