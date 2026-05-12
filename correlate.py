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
    correlation_report.csv   — full per-session breakdown (recreated each run)
    correlation_cache.csv    — persistent PowerPal solar/grid split per session (append-only)
    (also prints a summary table and EV insights to the terminal)

    The cache means PowerPal is only fetched for new sessions — old sessions keep their
    solar split even after they fall outside the PowerPal API lookback window.
    Use --recalculate to force a full re-fetch (e.g. after a PowerPal data correction).
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
DEFAULT_FEEDIN_RATE = 0.015  # $/kWh
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


_CORR_CACHE_FIELDS = [
    "session_id", "solar_kwh", "grid_kwh", "solar_pct",
    "powerpal_coverage", "powerpal_note",
]


def load_correlation_cache(path: Path) -> dict:
    """Load cached PowerPal correlations. Returns dict keyed by session_id."""
    cache = {}
    if not path.exists():
        return cache
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            sid = row.get("session_id")
            if sid:
                try:
                    cache[sid] = {
                        "solar_kwh":         float(row["solar_kwh"]),
                        "grid_kwh":          float(row["grid_kwh"]),
                        "solar_pct":         float(row["solar_pct"]),
                        "powerpal_coverage": row["powerpal_coverage"],
                        "powerpal_note":     row["powerpal_note"],
                    }
                except (KeyError, ValueError):
                    pass
    if cache:
        print(f"  Loaded {len(cache)} cached correlations from {path.name}")
    return cache


def save_correlation_cache(cache: dict, path: Path) -> None:
    """Write all correlation cache entries to disk."""
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CORR_CACHE_FIELDS)
        w.writeheader()
        for sid, c in cache.items():
            w.writerow({"session_id": sid, **c})


def fetch_powerpal(serial: str, api_key: str, sessions: list,
                   cache: dict | None = None) -> list:
    """Fetch PowerPal readings for home sessions not already in the correlation cache."""
    from datetime import timedelta

    if cache is None:
        cache = {}
    needs_fetch = [s for s in sessions
                   if (s.get("location", "H") or "H") != "A"
                   and s["session_id"] not in cache]
    if not needs_fetch:
        print("  All home sessions already cached — skipping PowerPal fetch")
        return []

    start_dt = min(s["start"] for s in needs_fetch) - timedelta(hours=1)
    end_dt   = max(s["end"]   for s in needs_fetch) + timedelta(hours=1)

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
                "date_local":   date,
                "start":        start,
                "end":          end,
                "duration_min": float(row["duration_minutes"]),
                "soc_start":    row["soc_start_pct"],
                "soc_end":      row["soc_end_pct"],
                "odo_start_km": row.get("odo_start_km", ""),
                "odo_end_km":   row.get("odo_end_km", ""),
                "km_driven":              row.get("km_driven_since_last_charge", ""),
                "km_driven_since_last_charge": row.get("km_driven_since_last_charge", ""),
                "kwh_estimated": float(row.get("kwh_charged_actual") or row.get("kwh_charged_estimated") or 0),
                "location":     row.get("location", "") or "H",
                "range_km":                    row.get("range_km", ""),
                "efficiency_kwh_per_100km":    row.get("efficiency_kwh_per_100km", ""),
                "lifetime_efficiency_kwh_per_100km": row.get("lifetime_efficiency_kwh_per_100km", ""),
            })
        except (ValueError, KeyError) as e:
            print(f"  Warning: skipping session row — {e}")
            continue

    print(f"  Loaded {len(sessions)} BYD charge sessions")
    return sessions


def correlate(sessions: list[dict], powerpal: list[dict],
              import_rate: float, feedin_rate: float,
              cache: dict | None = None) -> tuple[list[dict], dict]:
    """Correlate sessions with PowerPal data, using the cache for already-processed sessions.

    Returns (results, updated_cache). Costs are always recalculated from current rates so
    changing --import-rate / --feedin-rate never requires --recalculate.
    Sessions with 0% PowerPal coverage are not cached (data gap — will retry next run).
    """
    if cache is None:
        cache = {}

    # Build a fast lookup: index PowerPal by minute
    pp_by_minute = {}
    for row in powerpal:
        key = row["dt"].replace(second=0)
        pp_by_minute[key] = row["wh"]

    results = []
    prev_soc_end = None
    for s in sessions:
        from datetime import timedelta
        total_kwh  = s["kwh_estimated"]
        session_wh = total_kwh * 1000
        location   = s.get("location", "H") or "H"
        session_id = s["session_id"]

        if location == "A":
            # Away charge — all grid, no PowerPal needed
            solar_kwh = 0.0
            grid_kwh  = total_kwh
            solar_pct = 0.0
            coverage  = "Away"
            note      = "Away charge"
        elif session_id in cache:
            # Use previously stored correlation
            c         = cache[session_id]
            solar_kwh = c["solar_kwh"]
            grid_kwh  = c["grid_kwh"]
            solar_pct = c["solar_pct"]
            coverage  = c["powerpal_coverage"]
            note      = c["powerpal_note"]
        else:
            # Home charge — match against PowerPal data
            current = s["start"].replace(second=0)
            end_min = s["end"].replace(second=0)

            grid_wh         = 0.0
            minutes_matched = 0
            minutes_total   = 0

            while current <= end_min:
                minutes_total += 1
                if current in pp_by_minute:
                    grid_wh += pp_by_minute[current]
                    minutes_matched += 1
                current += timedelta(minutes=1)

            grid_wh   = min(grid_wh, session_wh)
            solar_wh  = max(session_wh - grid_wh, 0)
            grid_kwh  = round(grid_wh  / 1000, 3)
            solar_kwh = round(solar_wh / 1000, 3)
            solar_pct = round(solar_kwh / total_kwh * 100, 1) if total_kwh > 0 else 0

            cov_pct  = round(minutes_matched / minutes_total * 100, 0) if minutes_total else 0
            coverage = f"{cov_pct:.0f}%"
            note     = "" if cov_pct >= 80 else "⚠ low PowerPal coverage for this window"

            # Only cache if PowerPal actually had data for this window
            if minutes_matched > 0:
                cache[session_id] = {
                    "solar_kwh":         solar_kwh,
                    "grid_kwh":          grid_kwh,
                    "solar_pct":         solar_pct,
                    "powerpal_coverage": coverage,
                    "powerpal_note":     note,
                }

        # Costs always recalculated from current rates
        solar_cost    = round(solar_kwh * feedin_rate, 2)
        grid_cost     = round(grid_kwh  * import_rate, 2)
        total_cost    = round(solar_cost + grid_cost, 2)
        all_grid_cost = round(total_kwh  * import_rate, 2)
        saving        = round(all_grid_cost - total_cost, 2)

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
            "location":           location,
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
            "powerpal_coverage":  coverage,
            "note":               note,
        })

    return results, cache


def print_summary(results: list[dict]) -> None:
    if not results:
        print("\nNo sessions to display.")
        return

    W = 151
    print("\n" + "─" * W)
    print(f"{'ID':<7} {'L':>1} {'Date':<12} {'Start':>6} {'End':>6} {'Odo km':>8} {'SOC%':>9} {'km drv':>7} {'~Range':>7} {'km/%':>6} {'kWh':>6} "
          f"{'Solar':>7} {'Grid':>7} {'Solar%':>7} {'Cost $':>7} {'Saving $':>9} {'Coverage':>9}")
    print("─" * W)

    total_kwh = total_solar = total_grid = total_cost = total_saving = total_km = 0.0

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
            km_val = float(r['km_driven']) if r.get("km_driven") else None
            km_str = f"{km_val:.0f}" if km_val is not None else "—"
        except (ValueError, TypeError):
            km_val = None
            km_str = "—"
        est_range = r.get("est_range_km")
        rng_str = "NA" if est_range == "NA" else (f"{est_range} km" if est_range else "—")
        kmpct_str = f"{r['km_per_pct']}" if r.get("km_per_pct") else "—"
        loc_str = r.get("location", "H") or "H"
        print(f"{r['session_id']:<7} {loc_str:>1} {r['date']:<12} {r['start_local']:>6} {r['end_local']:>6} "
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
        if km_val is not None:
            total_km += km_val

    print("─" * W)
    avg_solar_pct = round(total_solar / total_kwh * 100, 1) if total_kwh else 0
    total_km_str = f"{total_km:.0f}" if total_km > 0 else ""
    print(f"{'TOTAL':<7} {'':<1} {'':<12} {'':>6} {'':>6} "
          f"{'':>8} {'':>9} {total_km_str:>7} {'':>7} {'':>6} "
          f"{total_kwh:>6.2f} {total_solar:>7.2f} {total_grid:>7.2f} "
          f"{avg_solar_pct:>6.1f}% ${total_cost:>6.2f} ${total_saving:>8.2f}")
    print("─" * W)
    print("  NA = trip too short to calculate (requires ≥2% SOC drop between sessions)")
    km_str = f"  |  Total driven: {total_km:.0f} km" if total_km > 0 else ""
    print(f"\n  {len(results)} sessions  |  "
          f"Total charged: {total_kwh:.1f} kWh  |  "
          f"Solar: {total_solar:.1f} kWh ({avg_solar_pct}%)  |  "
          f"Total cost: ${total_cost:.2f}  |  "
          f"Saved vs all-grid: ${total_saving:.2f}{km_str}")


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
                      import_rate: float, feedin_rate: float = 0.0) -> None:
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
    # Uses cumulative totals (total kWh / total km) rather than averaging per-session
    # ratios, which are skewed by partial top-ups on a slow EVSE.
    print(f"\n  2. EFFICIENCY (kWh/100km)")
    print(f"  {'─'*40}")
    if total_km > 0 and total_kwh > 0:
        overall_eff = total_kwh / total_km * 100
        dates = [s["date_local"] for s in sessions if s.get("date_local")]
        day_str = ""
        if dates:
            span_days = (datetime.strptime(max(dates), "%Y-%m-%d") -
                         datetime.strptime(min(dates), "%Y-%m-%d")).days + 1
            day_str = f", {total_km/span_days:.1f} km/day over {span_days} days"
        print(f"  From logs:                   {overall_eff:.1f} kWh/100km"
              f"  ({total_kwh:.1f} kWh, {total_km:.0f} km{day_str})")
    else:
        print(f"  Not enough data yet — need km_driven to calculate")
    lifetime_effs = [s["lifetime_efficiency_kwh_per_100km"]
                     for s in sessions
                     if s.get("lifetime_efficiency_kwh_per_100km")]
    if lifetime_effs:
        print(f"  From car (all-time):         {lifetime_effs[-1]} kWh/100km")
        if total_km > 0 and total_kwh > 0:
            try:
                gap = overall_eff - float(lifetime_effs[-1])
                print(f"  Gap:                         {gap:+.1f} kWh/100km  "
                      f"(logs include charging losses; car measures battery output)")
            except (ValueError, TypeError):
                pass

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
    seasonal = {s: {"kwh": 0.0, "solar_kwh": 0.0, "cost": 0.0, "km": 0.0, "n": 0,
                    "min_date": None, "max_date": None}
                for s in ("Summer", "Autumn", "Winter", "Spring")}
    for r in results:
        try:
            kwh = float(r["total_kwh"])
            if kwh <= 0:
                continue
            d     = r["date"]
            month = int(d.split("-")[1])
            if month in [12,1,2]:   bucket = "Summer"
            elif month in [3,4,5]:  bucket = "Autumn"
            elif month in [6,7,8]:  bucket = "Winter"
            else:                   bucket = "Spring"
            seasonal[bucket]["kwh"]       += kwh
            seasonal[bucket]["solar_kwh"] += float(r["solar_kwh"])
            seasonal[bucket]["cost"]      += float(r["total_cost"])
            seasonal[bucket]["n"]         += 1
            if seasonal[bucket]["min_date"] is None or d < seasonal[bucket]["min_date"]:
                seasonal[bucket]["min_date"] = d
            if seasonal[bucket]["max_date"] is None or d > seasonal[bucket]["max_date"]:
                seasonal[bucket]["max_date"] = d
            km = float(r["km_driven"]) if r.get("km_driven") else 0
            if km > 0:
                seasonal[bucket]["km"] += km
        except Exception:
            pass
    for season, data in seasonal.items():
        if data["n"] == 0:
            print(f"  {season:<10} no data yet")
            continue
        solar_pct = data["solar_kwh"] / data["kwh"] * 100 if data["kwh"] > 0 else 0
        line = f"  {season:<10} {data['kwh']:.1f} kWh  ({data['n']} sessions, {solar_pct:.0f}% solar)"
        if data["km"] > 0 and data["min_date"] and data["max_date"]:
            eff         = data["kwh"] / data["km"] * 100
            cost_per_km = data["cost"] / data["km"]
            span_days   = (datetime.strptime(data["max_date"], "%Y-%m-%d") -
                           datetime.strptime(data["min_date"], "%Y-%m-%d")).days + 1
            km_per_day  = data["km"] / span_days
            line += f"   {eff:.1f} kWh/100km  ${cost_per_km:.2f}/km  ({data['km']:.0f} km, {km_per_day:.1f} km/day)"
        print(line)

    # ── 7. Charging behaviour ───────────────────────────────────────────────
    print(f"\n  7. CHARGING BEHAVIOUR")
    print(f"  {'─'*40}")
    home_sessions = [s for s in sessions if (s.get("location") or "H") == "H"]
    away_sessions = [s for s in sessions if (s.get("location") or "H") == "A"]
    if away_sessions:
        print(f"  Home charges:                {len(home_sessions)}  "
              f"({len(home_sessions)/n_sessions*100:.0f}%)")
        print(f"  Away charges:                {len(away_sessions)}  "
              f"({len(away_sessions)/n_sessions*100:.0f}%)")
    day_sessions   = [s for s in sessions
                      if s.get("start") and
                      6 <= s["start"].hour < 20]
    night_sessions = [s for s in sessions if s not in day_sessions]
    soc_starts = [float(s["soc_start"]) for s in sessions
                  if s.get("soc_start") and
                  str(s["soc_start"]).replace(".","").isdigit()]
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

# ── AEMO spot price / Amber wholesale estimate ──────────────────────────────

from aemo import spot_prices_for_window  # noqa: E402

AMBER_CACHE_HDRS = [
    "session_id", "avg_spot_c_kwh", "min_spot_c_kwh", "max_spot_c_kwh",
    "negative_price_minutes", "amber_energy_cost", "amber_network_cost",
    "amber_total_cost", "fixed_total_cost", "amber_saving",
]


def _load_amber_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, newline="") as f:
            return {r["session_id"]: r for r in csv.DictReader(f)}
    except Exception:
        return {}


def calculate_amber_costs(sessions: list[dict], region: str, network_rate: float,
                           import_rate: float, utc_offset: int,
                           cache_path: Path, cache_dir: Path) -> dict:
    from datetime import timedelta
    NEM_DELTA = timedelta(hours=10 - utc_offset)  # convert local time → NEM time (UTC+10)
    cache     = _load_amber_cache(cache_path)
    new_rows  = []

    to_process = [s for s in sessions if s["session_id"] not in cache]
    if to_process:
        print(f"\n  Calculating Amber wholesale costs for {len(to_process)} session(s) …")

    for s in to_process:
        start_nem = s["start"] + NEM_DELTA
        end_nem   = s["end"]   + NEM_DELTA
        kwh       = s["kwh_estimated"]

        prices = spot_prices_for_window(start_nem, end_nem, region, cache_dir)
        if not prices:
            print(f"  Warning: no AEMO prices found for {s['session_id']} "
                  f"({start_nem} NEM) — skipping")
            continue

        avg_rrp  = sum(p["rrp"] for p in prices) / len(prices)  # $/MWh
        min_rrp  = min(p["rrp"] for p in prices)
        max_rrp  = max(p["rrp"] for p in prices)
        neg_mins = sum(5 for p in prices if p["rrp"] < 0)

        energy_cost  = round(kwh * avg_rrp / 1000, 2)
        network_cost = round(kwh * network_rate, 2)
        amber_total  = round(energy_cost + network_cost, 2)
        fixed_total  = round(kwh * import_rate, 2)
        saving       = round(fixed_total - amber_total, 2)

        row = {
            "session_id":             s["session_id"],
            "avg_spot_c_kwh":         round(avg_rrp / 10, 2),
            "min_spot_c_kwh":         round(min_rrp / 10, 2),
            "max_spot_c_kwh":         round(max_rrp / 10, 2),
            "negative_price_minutes": neg_mins,
            "amber_energy_cost":      energy_cost,
            "amber_network_cost":     network_cost,
            "amber_total_cost":       amber_total,
            "fixed_total_cost":       fixed_total,
            "amber_saving":           saving,
        }
        cache[s["session_id"]] = row
        new_rows.append(row)

    if new_rows:
        write_header = not cache_path.exists()
        with open(cache_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=AMBER_CACHE_HDRS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
        print(f"  Amber cache updated ({len(new_rows)} new row(s)): {cache_path}")

    return cache


def print_amber_summary(amber: dict, sessions: list[dict],
                         region: str, network_rate: float, subscription: float) -> None:
    rows = [s for s in sessions if s["session_id"] in amber]
    if not rows:
        return

    W = 95
    print(f"\n{'─' * W}")
    print(f"  AMBER WHOLESALE ESTIMATE  —  region: {region}  "
          f"network: {network_rate * 100:.1f}c/kWh  "
          f"subscription: ${subscription:.0f}/mo")
    print(f"  AEMO 5-min dispatch prices. Amber bills 30-min trading price — estimate only.")
    print(f"{'─' * W}")
    print(f"{'ID':<7} {'Date':<12} {'kWh':>5} {'Avg c/kWh':>10} {'Min':>7} {'Max':>7} "
          f"{'Neg min':>7} {'Fixed $':>8} {'Amber $':>8} {'Saving $':>9}")
    print(f"{'─' * W}")

    tot_kwh = tot_fixed = tot_amber = tot_saving = 0.0
    tot_neg = 0

    for s in rows:
        a       = amber[s["session_id"]]
        kwh     = s["kwh_estimated"]
        avg_c   = float(a["avg_spot_c_kwh"])
        min_c   = float(a["min_spot_c_kwh"])
        max_c   = float(a["max_spot_c_kwh"])
        neg_m   = int(a["negative_price_minutes"])
        fixed   = float(a["fixed_total_cost"])
        cost    = float(a["amber_total_cost"])
        saving  = float(a["amber_saving"])
        neg_str = f"{neg_m}m" if neg_m > 0 else "—"
        flag    = " ★" if neg_m > 0 else ""
        print(f"{s['session_id']:<7} {s['date']:<12} {kwh:>5.2f} {avg_c:>9.1f}c "
              f"{min_c:>6.1f}c {max_c:>6.1f}c {neg_str:>7} "
              f"${fixed:>7.2f} ${cost:>7.2f} ${saving:>8.2f}{flag}")
        tot_kwh   += kwh
        tot_fixed += fixed
        tot_amber += cost
        tot_saving += saving
        tot_neg   += neg_m

    print(f"{'─' * W}")
    print(f"{'TOTAL':<7} {'':<12} {tot_kwh:>5.2f} {'':>10} {'':>7} {'':>7} "
          f"{tot_neg:>5}m {'':>2} ${tot_fixed:>7.2f} ${tot_amber:>7.2f} ${tot_saving:>8.2f}")
    print(f"{'─' * W}")

    print(f"\n  ★ = session had negative-price period (Amber credits you during these minutes)")
    print(f"  Amber ${subscription:.0f}/mo service charge excluded — covers the whole house, not just the car.")
    verdict = f"CHEAPER by ${tot_saving:.2f}" if tot_saving > 0 else f"MORE EXPENSIVE by ${abs(tot_saving):.2f}"
    print(f"  Vs fixed rate (excl. service charge): Amber would have been {verdict}")


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
    # Amber / AEMO wholesale comparison
    parser.add_argument("--region",          choices=["QLD", "NSW", "VIC", "SA", "TAS"],
                        help="NEM region — enables Amber wholesale cost estimate")
    parser.add_argument("--amber-network-rate", type=float, default=0.09, metavar="RATE",
                        help="Network/distribution rate $/kWh added on top of spot (default 0.09)")
    parser.add_argument("--amber-subscription", type=float, default=18.00, metavar="DOLLARS",
                        help="Amber monthly subscription $ shown in summary (default 18.00)")
    parser.add_argument("--amber-cache",     default="amber_cache.csv", metavar="FILE",
                        help="Incremental cache for Amber costs (default amber_cache.csv)")
    parser.add_argument("--aemo-cache-dir",  default="aemo_cache", metavar="DIR",
                        help="Directory for cached AEMO monthly price files (default ./aemo_cache)")
    parser.add_argument("--utc-offset",      type=int, default=10, metavar="HOURS",
                        help="Your UTC offset: 10 for AEST, 11 for AEDT (default 10)")
    parser.add_argument("--correlation-cache", default="correlation_cache.csv", metavar="FILE",
                        help="Persistent PowerPal correlation cache (default correlation_cache.csv)")
    parser.add_argument("--recalculate",     action="store_true",
                        help="Ignore cache and re-fetch PowerPal for all sessions")
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

    cache_path = Path(args.correlation_cache)
    cache = {} if args.recalculate else load_correlation_cache(cache_path)

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
        results, cache = correlate(sessions, powerpal, args.import_rate, args.feedin_rate, cache)
        save_correlation_cache(cache, cache_path)
        print_summary(results)
        save_report(results, args.output)
    elif serial and api_key:
        powerpal = fetch_powerpal(serial, api_key, sessions, cache)
        print(f"\nCorrelating (import={args.import_rate} $/kWh, feedin={args.feedin_rate} $/kWh) …")
        results, cache = correlate(sessions, powerpal, args.import_rate, args.feedin_rate, cache)
        save_correlation_cache(cache, cache_path)
        print_summary(results)
        save_report(results, args.output)
    elif cache:
        print(f"\nCorrelating from cache (import={args.import_rate} $/kWh, feedin={args.feedin_rate} $/kWh) …")
        results, _ = correlate(sessions, [], args.import_rate, args.feedin_rate, cache)
        print_summary(results)
        save_report(results, args.output)
    else:
        print("\n  No PowerPal data — skipping solar/grid correlation")
        print("  Run get_powerpal_key.py for automatic API access, or pass --powerpal <csv>")

    # Always print EV insights
    print_ev_insights(sessions, results, args.import_rate, args.feedin_rate)

    # Amber wholesale estimate (only if --region provided)
    if args.region:
        amber = calculate_amber_costs(
            sessions, args.region, args.amber_network_rate,
            args.import_rate, args.utc_offset,
            Path(args.amber_cache), Path(args.aemo_cache_dir),
        )
        print_amber_summary(amber, sessions, args.region,
                            args.amber_network_rate, args.amber_subscription)


if __name__ == "__main__":
    main()