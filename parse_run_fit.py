import sys
import os
from fitparse import FitFile
from math import radians, cos, sin, asin, sqrt

# --- CONFIGURAZIONE UTENTE ---
FC_MAX = 190
FC_REST = 65
LTHR = 170  
HRR = FC_MAX - FC_REST
# COEFF_RECUPERO = 1.2  # 1.2 riflette circa 2 giorni di riposo per sforzi medi
COEFF_RECUPERO = 1.2  
LOG_FILE = "running_history.csv" # Database locale per il semaforo salute

def haversine(lat1, lon1, lat2, lon2):
    deg = 180.0 / 2**31
    lat1, lon1, lat2, lon2 = map(radians, [lat1*deg, lon1*deg, lat2*deg, lon2*deg])
    r = 6371000 
    a = sin((lat2-lat1)/2)**2 + cos(lat1)*cos(lat2)*sin((lon2-lon1)/2)**2
    return 2 * asin(sqrt(a)) * r

def get_zone_key(hr):
    p = ((hr - FC_REST) / HRR) * 100
    if p < 50: return "Z0"
    if p < 60: return "Z1"
    if p < 70: return "Z2"
    if p < 80: return "Z3"
    if p < 90: return "Z4"
    return "Z5"

def get_training_effect(tss, drift_pct, avg_gradient, history_drifts):
    # Logica Semaforo Rosso (Prevenzione Overtraining)
    if len(history_drifts) >= 2 and all(d > 15 for d in history_drifts[-2:]) and drift_pct > 15:
        return "ROSSO: STOP / RIPOSO FORZATO", "Terzo allenamento consecutivo con deriva > 15%. Rischio infortunio o stress cardiaco elevato."
    
    if drift_pct > 15:
        if avg_gradient > 1.5:
            return "GIALLO: SFORZO COLLINARE", f"Deriva del {drift_pct:.1f}% giustificata dalla pendenza media."
        else:
            return "ARANCIONE: SFORZO INTENSO", f"Deriva elevata ({drift_pct:.1f}%). Il cuore sta compensando fatica o calore."
    
    return "VERDE: MANTENIMENTO", "Sessione in equilibrio. Il motore risponde bene."

def analyze_fit():
    if len(sys.argv) < 2:
        print("Uso: python parse_run_fit.py 'file.fit'")
        return

    file_path = sys.argv[1]
    try:
        fitfile = FitFile(file_path)
    except Exception as e:
        print(f"Errore: {e}")
        return
    
    records = []
    total_dist, total_ascent, total_descent = 0.0, 0.0, 0.0
    last_pos, last_alt = None, None
    
    for m in fitfile.get_messages('record'):
        r = {
            'ts': m.get_value('timestamp'),
            'lat': m.get_value('position_lat'),
            'lon': m.get_value('position_long'),
            'hr': m.get_value('heart_rate'),
            'alt': m.get_value('enhanced_altitude') or m.get_value('altitude')
        }
        if r['lat'] is not None and r['lon'] is not None:
            if last_pos:
                d = haversine(last_pos[0], last_pos[1], r['lat'], r['lon'])
                if d < 100: total_dist += d
            last_pos = (r['lat'], r['lon'])
        if r['alt'] is not None:
            if last_alt is not None:
                diff = r['alt'] - last_alt
                if diff > 0.5: total_ascent += diff
                elif diff < -0.5: total_descent += abs(diff)
            last_alt = r['alt']
        r['cum_dist'] = total_dist
        records.append(r)

    if not records: return

    duration_sec = (records[-1]['ts'] - records[0]['ts']).total_seconds()
    dist_km = total_dist / 1000.0
    pace_sec = duration_sec / dist_km if dist_km > 0 else 0
    hrs = [r['hr'] for r in records if r['hr']]
    avg_hr = sum(hrs)/len(hrs) if hrs else 0
    
    # Calcolo Drift e Efficiency Factor
    q_size = len(hrs) // 4
    drift_val = (sum(hrs[-q_size:]) / q_size) - (sum(hrs[:q_size]) / q_size) if q_size > 0 else 0
    drift_pct = (drift_val / avg_hr) * 100 if avg_hr > 0 else 0
    
    speed_m_min = (total_dist / (duration_sec / 60))
    ef_factor = speed_m_min / avg_hr if avg_hr > 0 else 0

    tss = (duration_sec * avg_hr * (avg_hr/LTHR)) / (LTHR * 36)
    recupero_h = int(tss * COEFF_RECUPERO)

    # Lettura storico per semaforo
    history_drifts = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            history_drifts = [float(line.split(",")[1]) for line in f.readlines()[-5:]]
    
    # Scrittura sessione corrente
    with open(LOG_FILE, "a") as f:
        f.write(f"{records[0]['ts']},{drift_pct:.2f},{ef_factor:.2f}\n")

    te_label, te_desc = get_training_effect(tss, drift_pct, (total_ascent/total_dist*100 if total_dist>0 else 0), history_drifts)

    print("="*105)
    print(f" SESSIONE: {records[0]['ts'].strftime('%d/%m/%Y %H:%M')}")
    print("="*105)
    print(f" STATO SALUTE  : {te_label}")
    print(f" DIAGNOSI      : {te_desc}")
    print("-" * 55)
    print(f" METRICHE AVANZATE:")
    print(f" Efficiency Factor : {ef_factor:.2f} (Metri per battito)")
    print(f" Deriva Cardiaca   : {drift_pct:.1f}%")
    print(f" TSS               : {int(tss)} | Recupero Stimato: ~{recupero_h}h")
    print("-" * 55)
    print(f" DATI GENERALI:")
    print(f" Distanza  : {dist_km:.2f} km")
    print(f" Durata    : {int(duration_sec//60)}m {int(duration_sec%60)}s")
    print(f" Passo     : {int(pace_sec//60)}:{int(pace_sec%60):02d} /km")
    print(f" FC media  : {int(avg_hr)} bpm")
    print(f" Dislivello: +{total_ascent:.1f}m / -{total_descent:.1f}m")

    print("\n DETTAGLIO KM (Passo | FC | Pendenza):")
    current_km, km_ts, last_dist_check = 1, records[0]['ts'], 0.0
    first_alt = next((r['alt'] for r in records if r['alt'] is not None), 0)
    last_alt_km = first_alt
    
    for r in records:
        if r['cum_dist'] >= current_km * 1000:
            dt = (r['ts'] - km_ts).total_seconds()
            p_min, p_sec = divmod(int(dt), 60)
            diff_quota = (r['alt'] - last_alt_km) if r['alt'] is not None else 0
            print(f"  Km {current_km:<2}  | Passo {p_min}:{p_sec:02d} | FC {r['hr'] or '--'} bpm | Pendenza: {diff_quota:+.1f}m")
            last_dist_check = current_km * 1000
            last_alt_km = r['alt'] if r['alt'] is not None else last_alt_km
            current_km += 1
            km_ts = r['ts']

    remaining_dist = total_dist - last_dist_check
    if remaining_dist > 10:
        dt_rem = (records[-1]['ts'] - km_ts).total_seconds()
        pace_rem_raw = dt_rem / (remaining_dist / 1000.0)
        p_min, p_sec = divmod(int(pace_rem_raw), 60)
        diff_quota_rem = (records[-1]['alt'] - last_alt_km) if records[-1]['alt'] is not None else 0
        print(f"  Km {current_km:<2}* | Passo {p_min}:{p_sec:02d} | FC {records[-1]['hr'] or '--'} bpm | Pendenza: {diff_quota_rem:+.1f}m ({int(remaining_dist)}m parziali)")

    print("="*105)

if __name__ == "__main__":
    analyze_fit()