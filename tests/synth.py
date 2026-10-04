"""Synthetic intervals.icu-style CSV generator (Samsung quirks: doubled distance, GPS lockup)."""
import datetime as dt


def write_run(path, km=6.0, base_speed=2.5, sprint=False, gps_lockup_s=20, hr_start=138.0, icu_format=False, dist_factor=None):
    if dist_factor is None:
        dist_factor = 1
    t0 = dt.datetime(2026, 5, 7, 7, 0, 0)
    rows = ["time,cadence,heartrate,distance,altitude,lat,lng,velocity_smooth,fixed_altitude" if icu_format
            else "time,lat,lon,altitude,distance,heart_rate"]
    lat, lon, d, t = 45.0, 9.0, 0.0, 0
    sprint_from = (km - 1) * 1000
    while d < km * 1000:
        in_sprint = sprint and d >= sprint_from
        v = base_speed * 1.25 if in_sprint else base_speed
        hr = 165 if in_sprint else hr_start + t / 600
        d += v
        t += 1
        lat += v / 111320
        gps = t > gps_lockup_s
        ts = (t0 + dt.timedelta(seconds=t)).isoformat() + "Z"
        alt = 100 + (t % 7) * 0.3
        dist = d * dist_factor if dist_factor else ''
        if icu_format:
            rows.append(f"{t},0,{hr:.0f},{dist},,{lat if gps else ''},{lon if gps else ''},{v},{alt}")
        else:
            rows.append(f"{ts},{lat if gps else ''},{lon if gps else ''},{alt},{dist},{hr:.0f}")
    with open(path, "w") as f:
        f.write("\n".join(rows))
