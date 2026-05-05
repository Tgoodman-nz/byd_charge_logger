"""
BYD / PowerPal Correlation Script
==================================
Fetches BYD charge sessions and correlates them against PowerPal energy data
to calculate solar vs grid charging per session.

Usage:
  BYD data — choose one:
    --url https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN
    --sessions charge_sessions.csv          (local file)

  PowerPal data — choose one (in priority order):
    --powerpal powerpal_data.csv            (manual CSV export from the app)
    --powerpal-serial XXXXXXXX --powerpal-key <api_key>  (explicit API credentials)
    (nothing)                               (auto: loads from powerpal_ble.json if present)

  Run get_powerpal_key.py once to set up automatic API access (no CSV needed).

Examples:
    # Fully automatic — API credentials loaded from powerpal_ble.json
    python correlate.py --url "https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN"

    # Manual PowerPal CSV export
    python correlate.py --url "..." --powerpal powerpal_data.csv

    # Explicit API credentials
    python correlate.py --url "..." --powerpal-serial XXXXXXXX --powerpal-key <api_key>

    # BYD sessions only (no solar breakdown)
    python correlate.py --sessions charge_sessions.csv

Output:
    correlation_report.csv  — full per-session breakdown
    (also prints a summary table and EV insights to the terminal)
"""

import argparse
import csv
import json
import ssl
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import urllib.request

# ── Tariff defaults — override with --import-rate and --feedin-rate ─────────
DEFAULT_IMPORT_RATE = 0.30   # $/kWh
DEFAULT_FEEDIN_RATE = 0.06   # $/kWh
# ────────────────────────────────────────────────────────────────────────────


def fetch_url(url: str) -> str:
    ctx = None
    if url.startswith("https://"):
        cafile = Path(__file__).parent / "cert.pem"
        ctx = ssl.create_default_context(cafile=str(cafile))
    with urllib.request.urlopen(url, timeout=15, context=ctx) as r:
        return r.read().decode("utf-8")


POWERPAL_CONFIG = Path(__file__).parent / "powerpal_ble.json"
POWERPAL_CHUNK_SECS = 30 * 24 * 3600  # 30-day chunks (API limit: 50k records ≈ 34 days)


def fetch_powerpal(serial: str, api_key: str, sessions: list) -> list:
    """Fetch PowerPal readings from the API for the date range covered by sessions."""
    from datetime import timedelta

    start_dt = min(s["start"] for s in sessions) - timedelta(hours=1)
    end_dt   = max(s["end"]   for s in sessions) + timedelta(hours=1)

    rows = []
    t = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    while t < end_ts:
        chunk_end = min(t + POWERPAL_CHUNK_SECS, end_ts)
        url = (f"https://readings.powerpal.net/api/v1/meter_reading/{serial}"
               f"?start={t}&end={chunk_end}")
        print(f"  Fetching PowerPal {datetime.fromtimestamp(t).date()}"
              f" → {datetime.fromtimestamp(chunk_end).date()} …")
        req = urllib.request.Request(url, headers={"Authorization": api_key})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        readings = data if isinstance(data, list) else data.get("meter_reading", [])
        for reading in readings:
            rows.append({
                "dt": datetime.fromtimestamp(reading["timestamp"]),
                "wh": float(reading.get("watt_hours", 0)),
            })
        t = chunk_end

    rows.sort(key=lambda r: r["dt"])
    if rows:
        print(f"  Fetched {len(rows):,} PowerPal readings  "
              f"({rows[0]['dt'].date()} → {rows[-1]['dt'].date()})")
    return rows


def load_powerpal(path: str) -> list[dict]:
    """Load PowerPal CSV export. Returns list of {dt: datetime, wh: float}."""
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime_local"], "%Y-%m-%d %H:%M:%S")
                wh = float(row["watt_hours"])
                rows.append({"dt": dt, "wh": wh})
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda r: r["dt"])
    print(f"  Loaded {len(rows):,} PowerPal readings  "
          f"({rows[0]['dt'].date()} → {rows[-1]['dt'].date()})")
    return rows


def load_sessions(source: str) -> list[dict]:
    """Load BYD sessions from a URL or local file path."""
    if source.startswith("http"):
        print(f"  Fetching sessions from {source.split('?')[0]} …")
        content = fetch_url(source)
        reader = csv.DictReader(StringIO(content))
    else:
        reader = csv.DictReader(open(source, newline=""))

    sessions = []
    for row in reader:
        try:
            # Combine date + time into a datetime
            date = row["date_local"]
            start = datetime.strptime(f"{date} {row['start_time_local']}", "%Y-%m-%d %H:%M:%S")
            end   = datetime.strptime(f"{date} {row['end_time_local']}",   "%Y-%m-%d %H:%M:%S")

            # Handle sessions that cross midnight
            if end < start:
                from datetime import timedelta
                end += timedelta(days=1)

            sessions.append({
                "session_id":   row["session_id"],
                "date":         date,
                "start":        start,
                "end":          end,
                "duration_min": float(row["duration_minutes"]),
                "soc_start":    row["soc_start_pct"],
                "soc_end":      row["soc_end_pct"],
                "odo_start_km": row.get("odo_start_km", ""),
                "odo_end_km":   row.get("odo_end_km", ""),
                "km_driven":    row.get("km_driven_since_last_charge", ""),
                "kwh_estimated": float(row.get("kwh_charged_actual") or row.get("kwh_charged_estimated") or 0),
            })
        except (ValueError, KeyError) as e:
            print(f"  Warning: skipping session row — {e}")
            continue

    print(f"  Loaded {len(sessions)} BYD charge sessions")
    return sessions


def correlate(sessions: list[dict], powerpal: list[dict],
              import_rate: float, feedin_rate: float) -> list[dict]:
    """For each session, sum PowerPal grid import during the window."""

    # Build a fast lookup: index PowerPal by minute
    pp_by_minute = {}
    for row in powerpal:
        key = row["dt"].replace(second=0)
        pp_by_minute[key] = row["wh"]

    results = []
    prev_soc_end = None
    for s in sessions:
        # Walk every minute in the session window
        from datetime import timedelta
        current = s["start"].replace(second=0)
        end_min = s["end"].replace(second=0)

        grid_wh = 0.0
        minutes_matched = 0
        minutes_total   = 0

        while current <= end_min:
            minutes_total += 1
            if current in pp_by_minute:
                grid_wh += pp_by_minute[current]
                minutes_matched += 1
            current += timedelta(minutes=1)

        session_wh  = s["kwh_estimated"] * 1000
        grid_wh     = min(grid_wh, session_wh)          # can't exceed session total
        solar_wh    = max(session_wh - grid_wh, 0)

        grid_kwh    = round(grid_wh  / 1000, 3)
        solar_kwh   = round(solar_wh / 1000, 3)
        total_kwh   = s["kwh_estimated"]

        solar_pct   = round(solar_kwh / total_kwh * 100, 1) if total_kwh > 0 else 0

        # Cost calculation
        # Solar cost = opportunity cost (what you'd have earned exporting instead)
        # Grid cost  = what you paid to import
        solar_cost  = round(solar_kwh * feedin_rate, 2)
        grid_cost   = round(grid_kwh  * import_rate, 2)
        total_cost  = round(solar_cost + grid_cost, 2)
        all_grid_cost = round(total_kwh * import_rate, 2)
        saving      = round(all_grid_cost - total_cost, 2)

        coverage    = round(minutes_matched / minutes_total * 100, 0) if minutes_total else 0

        # Estimated range and efficiency: km driven divided by % used on that leg
        est_range_km = None
        km_per_pct   = None
        try:
            km  = float(s["km_driven"]) if s.get("km_driven") else None
            soc = float(s["soc_start"]) if s.get("soc_start") else None
            if km and soc is not None and prev_soc_end is not None:
                soc_drop = prev_soc_end - soc
                if soc_drop >= 2:
                    est_range_km = round(km / soc_drop * 100)
                    km_per_pct   = round(km / soc_drop, 1)
                elif km > 0:
                    est_range_km = "NA"  # trip too short for reliable reading
                    km_per_pct   = "NA"
        except (ValueError, TypeError):
            pass
        try:
            prev_soc_end = float(s["soc_end"]) if s.get("soc_end") else prev_soc_end
        except (ValueError, TypeError):
            pass

        results.append({
            "session_id":         s["session_id"],
            "date":               s["date"],
            "start_local":        s["start"].strftime("%H:%M"),
            "end_local":          s["end"].strftime("%H:%M"),
            "duration_min":       s["duration_min"],
            "soc_start":          s["soc_start"],
            "soc_end":            s["soc_end"],
            "odo_end_km":         s.get("odo_end_km", ""),
            "km_driven":          s.get("km_driven", ""),
            "est_range_km":       est_range_km,
            "km_per_pct":         km_per_pct,
            "total_kwh":          total_kwh,
            "solar_kwh":          solar_kwh,
            "grid_kwh":           grid_kwh,
            "solar_pct":          solar_pct,
            "solar_cost":         solar_cost,
            "grid_cost":          grid_cost,
            "total_cost":         total_cost,
            "saving_vs_grid":     saving,
            "powerpal_coverage":  f"{coverage:.0f}%",
            "note": "" if coverage >= 80 else "⚠ low PowerPal coverage for this window",
        })

    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        print("\nNo sessions to display.")
        return

    W = 149
    print("\n" + "─" * W)
    print(f"{'ID':<7} {'Date':<12} {'Start':>6} {'End':>6} {'Odo km':>8} {'SOC%':>9} {'km drv':>7} {'~Range':>7} {'km/%':>6} {'kWh':>6} "
          f"{'Solar':>7} {'Grid':>7} {'Solar%':>7} {'Cost $':>7} {'Saving $':>9} {'Coverage':>9}")
    print("─" * W)

    total_kwh = total_solar = total_grid = total_cost = total_saving = 0.0

    for r in results:
        try:
            odo_str = f"{float(r['odo_end_km']):.0f}" if r.get("odo_end_km") else "—"
        except (ValueError, TypeError):
            odo_str = "—"
        try:
            soc_str = f"{int(float(r['soc_start']))}→{int(float(r['soc_end']))}%"
        except (ValueError, TypeError):
            soc_str = "—"
        try:
            km_str = f"{float(r['km_driven']):.0f}" if r.get("km_driven") else "—"
        except (ValueError, TypeError):
            km_str = "—"
        est_range = r.get("est_range_km")
        rng_str = "NA" if est_range == "NA" else (f"{est_range} km" if est_range else "—")
        kmpct_str = f"{r['km_per_pct']}" if r.get("km_per_pct") else "—"
        print(f"{r['session_id']:<7} {r['date']:<12} {r['start_local']:>6} {r['end_local']:>6} "
              f"{odo_str:>8} {soc_str:>9} {km_str:>7} {rng_str:>7} {kmpct_str:>6} "
              f"{r['total_kwh']:>6.2f} {r['solar_kwh']:>7.2f} {r['grid_kwh']:>7.2f} "
              f"{r['solar_pct']:>6.1f}% ${r['total_cost']:>6.2f} ${r['saving_vs_grid']:>8.2f}"
              f"  {r['powerpal_coverage']:>6}"
              + (f"  {r['note']}" if r["note"] else ""))
        total_kwh    += r["total_kwh"]
        total_solar  += r["solar_kwh"]
        total_grid   += r["grid_kwh"]
        total_cost   += r["total_cost"]
        total_saving += r["saving_vs_grid"]

    print("─" * W)
    avg_solar_pct = round(total_solar / total_kwh * 100, 1) if total_kwh else 0
    print(f"{'TOTAL':<7} {'':<12} {'':>6} {'':>6} "
          f"{'':>8} {'':>9} {'':>7} {'':>7} {'':>6} "
          f"{total_kwh:>6.2f} {total_solar:>7.2f} {total_grid:>7.2f} "
          f"{avg_solar_pct:>6.1f}% ${total_cost:>6.2f} ${total_saving:>8.2f}")
    print("─" * W)
    print("  NA = trip too short to calculate (requires ≥2% SOC drop between sessions)")
    print(f"\n  {len(results)} sessions  |  "
          f"Total charged: {total_kwh:.1f} kWh  |  "
          f"Solar: {total_solar:.1f} kWh ({avg_solar_pct}%)  |  "
          f"Total cost: ${total_cost:.2f}  |  "
          f"Saved vs all-grid: ${total_saving:.2f}")


def save_report(results: list[dict], output_path: str) -> None:
    if not results:
        return
    fields = list(results[0].keys())
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    print(f"\n  Report saved to: {output_path}")


def print_ev_insights(sessions: list[dict], results: list[dict],
                      import_rate: float, feedin_rate: float) -> None:
    """
    Print a summary answering the key EV questions:
    1. How much power does the car use?
    2. How efficient is it per km?
    3. Battery degradation indicators
    4. Solar vs grid split
    5. Cost per km
    6. Seasonal efficiency variation
    7. Charging behaviour patterns
    8. Savings vs petrol
    9. Real-world range
    """
    if not sessions:
        return

    SEP  = "─" * 68
    SEP2 = "═" * 68
    PETROL_RATE_PER_L  = 2.00   # $/litre — update with current price
    PETROL_L_PER_100KM = 8.5    # typical petrol car litres/100km

    print(f"\n{'EV INSIGHTS SUMMARY':^68}")
    print(SEP2)

    # ── 1. Total power used ─────────────────────────────────────────────────
    total_kwh    = sum(s.get("kwh_estimated", 0) for s in sessions)
    total_km     = sum(float(s["km_driven_since_last_charge"])
                       for s in sessions
                       if s.get("km_driven_since_last_charge") and
                       str(s["km_driven_since_last_charge"]).replace(".","").isdigit())
    n_sessions   = len(sessions)

    print(f"\n  1. POWER USAGE")
    print(f"  {'─'*40}")
    print(f"  Total sessions logged:       {n_sessions}")
    print(f"  Total kWh charged:           {total_kwh:.1f} kWh")
    print(f"  Average per session:         {total_kwh/n_sessions:.1f} kWh")

    # ── 2. Efficiency per km ────────────────────────────────────────────────
    print(f"\n  2. EFFICIENCY (kWh/100km)")
    print(f"  {'─'*40}")
    eff_sessions = [s for s in sessions
                    if s.get("efficiency_kwh_per_100km") and
                    str(s["efficiency_kwh_per_100km"]).replace(".","").isdigit()]
    if eff_sessions:
        efficiencies = [float(s["efficiency_kwh_per_100km"]) for s in eff_sessions]
        avg_eff = sum(efficiencies) / len(efficiencies)
        print(f"  Average efficiency:          {avg_eff:.1f} kWh/100km")
        print(f"  Best session:                {min(efficiencies):.1f} kWh/100km")
        print(f"  Worst session:               {max(efficiencies):.1f} kWh/100km")
        # Latest lifetime from BYD
        lifetime_effs = [s["lifetime_efficiency_kwh_per_100km"]
                         for s in sessions
                         if s.get("lifetime_efficiency_kwh_per_100km")]
        if lifetime_effs:
            print(f"  BYD lifetime average:        {lifetime_effs[-1]} kWh/100km")
    else:
        print(f"  Not enough data yet — need km_driven to calculate")

    # ── 3. Battery degradation indicators ───────────────────────────────────
    print(f"\n  3. BATTERY HEALTH INDICATORS")
    print(f"  {'─'*40}")
    range_at_100 = [(s["date_local"], float(s["range_km"]))
                    for s in sessions
                    if s.get("range_km") and s.get("soc_end_pct") and
                    str(s.get("soc_end_pct","0")).replace(".","").isdigit() and
                    float(str(s.get("soc_end_pct",0))) >= 95]
    if len(range_at_100) >= 2:
        first_range = range_at_100[0]
        last_range  = range_at_100[-1]
        print(f"  Range at ~100% SOC (first):  {first_range[1]:.0f} km  ({first_range[0]})")
        print(f"  Range at ~100% SOC (latest): {last_range[1]:.0f} km  ({last_range[0]})")
        delta = last_range[1] - first_range[1]
        print(f"  Change:                      {delta:+.0f} km  "
              f"({'degradation detected' if delta < -10 else 'within normal variation'})")
    else:
        print(f"  Need more full-charge sessions to assess degradation")
        print(f"  (Tip: check range_km column when SOC reaches ~100%)")

    # ── 4. Solar vs grid ────────────────────────────────────────────────────
    print(f"\n  4. SOLAR vs GRID")
    print(f"  {'─'*40}")
    if results:
        total_solar  = sum(r["solar_kwh"] for r in results)
        total_grid   = sum(r["grid_kwh"]  for r in results)
        total_r_kwh  = sum(r["total_kwh"] for r in results)
        solar_pct    = total_solar / total_r_kwh * 100 if total_r_kwh else 0
        print(f"  Solar charged:               {total_solar:.1f} kWh  ({solar_pct:.0f}%)")
        print(f"  Grid charged:                {total_grid:.1f} kWh  ({100-solar_pct:.0f}%)")
        print(f"  (Sessions without PowerPal data not included)")
    else:
        print(f"  Run get_powerpal_key.py first to enable solar vs grid split")

    # ── 5. Cost per km ──────────────────────────────────────────────────────
    print(f"\n  5. COST PER KM")
    print(f"  {'─'*40}")
    if results and total_km > 0:
        total_cost   = sum(r["total_cost"] for r in results)
        cost_per_km  = total_cost / total_km * 100  # cents
        print(f"  Total charging cost:         ${total_cost:.2f}")
        print(f"  Total km driven:             {total_km:.0f} km")
        print(f"  Cost per km:                 {cost_per_km:.1f}c/km")
    elif total_km > 0 and total_kwh > 0:
        # Estimate without solar split
        est_cost_per_km = (total_kwh / total_km) * import_rate * 100
        print(f"  Estimated cost per km:       {est_cost_per_km:.1f}c/km  (at {import_rate*100:.0f}c/kWh grid rate)")
        print(f"  (Run get_powerpal_key.py first for solar-adjusted cost)")
    else:
        print(f"  Need km_driven data — will populate after first full session cycle")

    # ── 6. Seasonal efficiency ──────────────────────────────────────────────
    print(f"\n  6. SEASONAL EFFICIENCY")
    print(f"  {'─'*40}")
    seasonal = {"Summer":{}, "Autumn":{}, "Winter":{}, "Spring":{}}
    for s in eff_sessions:
        try:
            month = int(s["date_local"].split("-")[1])
            eff   = float(s["efficiency_kwh_per_100km"])
            if month in [12,1,2]:   season = "Summer"
            elif month in [3,4,5]:  season = "Autumn"
            elif month in [6,7,8]:  season = "Winter"
            else:                   season = "Spring"
            seasonal[season].setdefault("effs", []).append(eff)
        except Exception:
            pass
    for season, data in seasonal.items():
        effs = data.get("effs", [])
        if effs:
            print(f"  {season:<10} avg {sum(effs)/len(effs):.1f} kWh/100km  ({len(effs)} sessions)")
        else:
            print(f"  {season:<10} no data yet")

    # ── 7. Charging behaviour ───────────────────────────────────────────────
    print(f"\n  7. CHARGING BEHAVIOUR")
    print(f"  {'─'*40}")
    day_sessions   = [s for s in sessions
                      if s.get("start") and
                      6 <= s["start"].hour < 20]
    night_sessions = [s for s in sessions if s not in day_sessions]
    soc_starts = [float(s["soc_start_pct"]) for s in sessions
                  if s.get("soc_start_pct") and
                  str(s["soc_start_pct"]).replace(".","").isdigit()]
    print(f"  Day charges (6am-8pm):       {len(day_sessions)}  "
          f"({len(day_sessions)/n_sessions*100:.0f}% — likely solar)")
    print(f"  Night charges (8pm-6am):     {len(night_sessions)}  "
          f"({len(night_sessions)/n_sessions*100:.0f}% — likely grid)")
    if soc_starts:
        print(f"  Avg SOC at plug-in:          {sum(soc_starts)/len(soc_starts):.0f}%")
        print(f"  Lowest SOC at plug-in:       {min(soc_starts):.0f}%")

    # ── 8. Savings vs petrol ────────────────────────────────────────────────
    print(f"\n  8. SAVINGS vs PETROL")
    print(f"  {'─'*40}")
    if total_km > 0:
        petrol_cost  = total_km / 100 * PETROL_L_PER_100KM * PETROL_RATE_PER_L
        if results:
            ev_cost  = sum(r["total_cost"] for r in results)
        else:
            ev_cost  = total_kwh * import_rate
        saving       = petrol_cost - ev_cost
        print(f"  km driven:                   {total_km:.0f} km")
        print(f"  Petrol equivalent cost:      ${petrol_cost:.2f}  "
              f"({PETROL_L_PER_100KM}L/100km @ ${PETROL_RATE_PER_L}/L)")
        print(f"  EV actual cost:              ${ev_cost:.2f}")
        print(f"  Saving vs petrol:            ${saving:.2f}")
        print(f"  (Update PETROL_RATE_PER_L and PETROL_L_PER_100KM at top of script)")
    else:
        print(f"  Need km_driven data to calculate")

    # ── 9. Real-world range ─────────────────────────────────────────────────
    print(f"\n  9. REAL-WORLD RANGE")
    print(f"  {'─'*40}")
    km_per_charge = [float(s["km_driven_since_last_charge"])
                     for s in sessions
                     if s.get("km_driven_since_last_charge") and
                     str(s["km_driven_since_last_charge"]).replace(".","").isdigit() and
                     float(s["km_driven_since_last_charge"]) > 0]
    if km_per_charge:
        print(f"  Average km between charges:  {sum(km_per_charge)/len(km_per_charge):.0f} km")
        print(f"  Longest between charges:     {max(km_per_charge):.0f} km")
        print(f"  Shortest between charges:    {min(km_per_charge):.0f} km")
        print(f"  (BYD Seal rated range: 510 km — compare to your actual usage)")
    else:
        print(f"  Need more sessions to calculate")

    print(f"\n{SEP2}\n")

def main() -> None:
    parser = argparse.ArgumentParser(description="Correlate BYD charge sessions with PowerPal data")
    parser.add_argument("--powerpal",        help="Path to PowerPal CSV export (alternative to API)")
    parser.add_argument("--powerpal-serial", help="PowerPal serial number (default: from powerpal_ble.json)")
    parser.add_argument("--powerpal-key",    help="PowerPal API key (default: from powerpal_ble.json)")
    parser.add_argument("--url",             help="URL to BYD sessions CSV (http://...)")
    parser.add_argument("--sessions",        help="Local path to BYD sessions CSV (alternative to --url)")
    parser.add_argument("--output",          default="correlation_report.csv", help="Output CSV path")
    parser.add_argument("--import-rate",     type=float, default=DEFAULT_IMPORT_RATE,
                        help=f"Grid import rate $/kWh (default {DEFAULT_IMPORT_RATE})")
    parser.add_argument("--feedin-rate",     type=float, default=DEFAULT_FEEDIN_RATE,
                        help=f"Solar feed-in tariff $/kWh (default {DEFAULT_FEEDIN_RATE})")
    args = parser.parse_args()

    if not args.url and not args.sessions:
        print("Error: provide either --url or --sessions")
        sys.exit(1)

    print("\nBYD Charge Session Report")
    print("══════════════════════════")

    print("\nLoading data …")
    sessions = load_sessions(args.url or args.sessions)

    if not sessions:
        print("No sessions found. Is the BYD logger running?")
        sys.exit(0)

    # Resolve PowerPal credentials from args or powerpal_ble.json
    serial  = args.powerpal_serial
    api_key = args.powerpal_key
    if (not serial or not api_key) and POWERPAL_CONFIG.exists():
        cfg     = json.loads(POWERPAL_CONFIG.read_text())
        serial  = serial  or cfg.get("serial")
        api_key = api_key or cfg.get("api_key")

    results = []
    if args.powerpal:
        powerpal = load_powerpal(args.powerpal)
        print(f"\nCorrelating (import={args.import_rate} $/kWh, feedin={args.feedin_rate} $/kWh) …")
        results = correlate(sessions, powerpal, args.import_rate, args.feedin_rate)
        print_summary(results)
        save_report(results, args.output)
    elif serial and api_key:
        powerpal = fetch_powerpal(serial, api_key, sessions)
        if powerpal:
            print(f"\nCorrelating (import={args.import_rate} $/kWh, feedin={args.feedin_rate} $/kWh) …")
            results = correlate(sessions, powerpal, args.import_rate, args.feedin_rate)
            print_summary(results)
            save_report(results, args.output)
    else:
        print("\n  No PowerPal data — skipping solar/grid correlation")
        print("  Run get_powerpal_key.py for automatic API access, or pass --powerpal <csv>")

    # Always print EV insights
    print_ev_insights(sessions, results, args.import_rate, args.feedin_rate)


if __name__ == "__main__":
    main()