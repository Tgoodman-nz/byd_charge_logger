"""
Home Energy Analysis
====================
Quarterly script — run after receiving a new electricity/gas bill, or after
every ~3 months of data. Correlates solar feed-in with usage across your bills
to assess electrification payback, battery viability, and true energy costs.

Answers:
  1. Current energy cost baseline (two years, EV included)
  2. Electrification payback — Goodbye Gas quote with rate inflation scenarios
  3. Battery analysis
  4. Solar upgrade analysis
  5. HVAC sizing commentary
  6. EV charging: solar vs grid per session

Usage:
  python analyse.py --elec ./elec_data --gas ./gas_data
  python analyse.py --elec ./elec_data --gas ./gas_data --byd http://VM:8080/sessions.csv?token=TOKEN

  Add new bill CSVs to elec_data / gas_data folders before each run.
"""

import argparse, csv, os, sys, urllib.request
from collections import defaultdict
from datetime import datetime, date, timedelta
from io import StringIO
from pathlib import Path

# ── Tariff & quote defaults ──────────────────────────────────────────────────
IMPORT_RATE   = 0.30    # $/kWh
FEEDIN_RATE   = 0.06    # $/kWh
GAS_RATE      = 0.025   # $/MJ
GAS_SUPPLY    = 0.85    # $/day standing charge
HEAT_PUMP_COP = 3.5
BATTERY_COST  = 12000
BATTERY_KWH   = 10.0
SOLAR_UPGRADE = 5000

# Electrification quotes (net of all rebates) — fill in your own quotes
# Get quotes from local installers; check available rebates in your state
GG_HOT_WATER  = 0      # heat pump hot water system (after rebates)
GG_COOKTOP    = 0      # induction cooktop (incl. any circuit upgrade)
GG_HEATING    = 0      # ducted heat pump (after rebates)
GG_BBQ        = 0      # BBQ conversion (optional)
GG_SERVICE    = 0      # gas disconnection / service fee
GG_TOTAL      = GG_HOT_WATER + GG_COOKTOP + GG_HEATING + GG_BBQ + GG_SERVICE

# House specs — fill in your own values
# Used for HVAC sizing commentary and heat load calculations
HOUSE_FLOOR_M2    = 0        # total floor area m²
HOUSE_ORIENTATION = "Unknown" # e.g. "North-facing", "West-facing"
CEILING_R         = 0        # ceiling insulation R-value
WALL_R            = 0        # wall insulation R-value
CALC_HEAT_LOAD_KW = 0        # design heating load kW (from HVAC quote)
CALC_COOL_LOAD_KW = 0        # design cooling load kW
CALC_HEATWAVE_KW  = 0        # peak cooling load kW (e.g. extreme summer day)

# Rate inflation scenarios for payback modelling
INFLATION_SCENARIOS = [
    ("Flat rates (conservative)",    0.00, 0.00, 0.00),  # elec, gas, feedin
    ("Moderate (elec+5%, gas+8%)",   0.05, 0.08, -0.05), # feedin declining
    ("High (elec+5%, gas+12%)",      0.05, 0.12, -0.10), # gas death spiral
]

SEP  = "─" * 70
SEP2 = "═" * 70

# ── Loaders ──────────────────────────────────────────────────────────────────

def load_elec_csvs(folder):
    days = {}
    for f in sorted(Path(folder).glob("*.csv")):
        with open(f) as fh:
            for row in csv.DictReader(fh):
                try:
                    d = datetime.strptime(row["READ DATE"], "%d %B %Y").date()
                    days[d] = {"import": float(row["CONSUMPTION(KWH)"]),
                               "export": float(row["SOLD TO GRID(KWH)"])}
                except: pass
    if days:
        print(f"  Loaded {len(days)} days electricity  ({min(days)!s} → {max(days)!s})")
    return days

def load_powerpal(path):
    daily = defaultdict(float)
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime_local"], "%Y-%m-%d %H:%M:%S")
                daily[dt.date()] += float(row["watt_hours"])
            except: pass
    result = {d: wh/1000 for d, wh in daily.items()}
    if result:
        dates = sorted(result)
        print(f"  Loaded PowerPal: {dates[0]} → {dates[-1]}")
    return result

def load_gas_csvs(folder):
    bills = []
    for f in sorted(Path(folder).glob("*.csv")):
        with open(f) as fh:
            for row in csv.DictReader(fh):
                try:
                    bills.append({"period": row["BILL PERIOD"].strip(),
                                  "mj": float(row["CONSUMPTION(MJ)"])})
                except: pass
    print(f"  Loaded {len(bills)} gas bill period(s)")
    return bills

def load_byd_sessions(source):
    if source.startswith("http"):
        with urllib.request.urlopen(source, timeout=15) as r:
            content = r.read().decode("utf-8")
        reader = csv.DictReader(StringIO(content))
    else:
        reader = csv.DictReader(open(source))
    sessions = []
    for row in reader:
        try:
            d     = row["date_local"]
            start = datetime.strptime(f"{d} {row['start_time_local']}", "%Y-%m-%d %H:%M:%S")
            end   = datetime.strptime(f"{d} {row['end_time_local']}",   "%Y-%m-%d %H:%M:%S")
            if end < start: end += timedelta(days=1)
            sessions.append({"session_id": row["session_id"], "date": start.date(),
                              "start": start, "end": end,
                              "kwh": float(row["kwh_charged_estimated"]),
                              "soc_start": row.get("soc_start_pct",""),
                              "soc_end":   row.get("soc_end_pct","")})
        except: pass
    print(f"  Loaded {len(sessions)} BYD charge sessions")
    return sessions

# ── Helpers ──────────────────────────────────────────────────────────────────

def annual_from_days(days):
    yr2025 = {d:v for d,v in days.items() if d.year == 2025}
    if len(yr2025) >= 350:
        pool, note = yr2025, "2025 full year"
    else:
        pool, note = days, f"all {len(days)} days annualised"
    n = len(pool)
    return (sum(v["import"] for v in pool.values())/n*365,
            sum(v["export"] for v in pool.values())/n*365, note)

def seasonal_avgs(days):
    s = defaultdict(list)
    for d, v in days.items():
        m = d.month
        if m in [12,1,2]:  s["summer"].append(v)
        elif m in [3,4,5]: s["autumn"].append(v)
        elif m in [6,7,8]: s["winter"].append(v)
        else:              s["spring"].append(v)
    return {k: {"imp": sum(x["import"] for x in v)/len(v),
                "exp": sum(x["export"] for x in v)/len(v),
                "n":   len(v)} for k,v in s.items()}

def gas_summary(bills):
    total_mj  = sum(b["mj"] for b in bills)
    pd = {"Dec":90,"Feb":59,"Apr":61,"Jun":61,"Aug":61,"Oct":61}
    total_days = sum(pd.get(b["period"].split()[0][:3], 60) for b in bills)
    summer_bills = [b for b in bills if b["period"].split()[0][:3] in ("Dec","Oct")]
    summer_days  = sum(pd.get(b["period"].split()[0][:3],60) for b in summer_bills)
    base_day = sum(b["mj"] for b in summer_bills) / summer_days if summer_days else 4.0
    heating_mj = max(total_mj - base_day * total_days, 0)
    return {"total_mj": total_mj, "total_days": total_days,
            "heating_mj": heating_mj, "baseline_mj": base_day * total_days,
            "annual_cost": total_mj * GAS_RATE + total_days * GAS_SUPPLY,
            "supply_annual": total_days * GAS_SUPPLY}

def correlate_ev(sessions, pp_path):
    if not sessions or not pp_path: return []
    pp = {}
    with open(pp_path) as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime_local"], "%Y-%m-%d %H:%M:%S").replace(second=0)
                pp[dt] = float(row["watt_hours"])
            except: pass
    results = []
    for s in sessions:
        cur = s["start"].replace(second=0); end = s["end"].replace(second=0)
        gwh = matched = total = 0
        while cur <= end:
            total += 1
            if cur in pp: gwh += pp[cur]; matched += 1
            cur += timedelta(minutes=1)
        swh = s["kwh"]*1000; gwh = min(gwh, swh); solwh = max(swh-gwh, 0)
        gkwh = round(gwh/1000,3); skwh = round(solwh/1000,3)
        results.append({**s, "solar_kwh": skwh, "grid_kwh": gkwh,
            "solar_pct":  round(skwh/s["kwh"]*100,1) if s["kwh"] else 0,
            "total_cost": round(skwh*FEEDIN_RATE + gkwh*IMPORT_RATE, 2),
            "saving":     round(s["kwh"]*IMPORT_RATE-(skwh*FEEDIN_RATE+gkwh*IMPORT_RATE), 2),
            "coverage":   f"{matched/total*100:.0f}%" if total else "0%"})
    return results

def npv_payback(annual_saving_yr1, elec_inflation, gas_inflation,
                feedin_change, quote_cost, years=20):
    """
    Calculate payback year accounting for rate changes over time.
    Returns (payback_years, savings_over_20yr)
    """
    cumulative = 0.0
    payback_yr = None
    ann_imp_saving = annual_saving_yr1 * 0.65   # approx split: 65% from gas/elec rate diff
    ann_feedin_loss = annual_saving_yr1 * 0.05  # small feedin component

    for yr in range(1, years+1):
        # Savings grow as gas rises faster than electricity
        yr_saving = (ann_imp_saving * (1 + elec_inflation) ** yr +
                     ann_feedin_loss * (1 + feedin_change) ** yr +
                     annual_saving_yr1 * 0.30 * (1 + gas_inflation) ** yr)
        cumulative += yr_saving
        if payback_yr is None and cumulative >= quote_cost:
            payback_yr = yr + (cumulative - quote_cost) / yr_saving
    return payback_yr, cumulative

# ── Report sections ───────────────────────────────────────────────────────────

def report_header():
    print(); print(SEP2)
    print("  HOME ENERGY ANALYSIS REPORT")
    print(f"  {HOUSE_FLOOR_M2:.0f}m²  |  {HOUSE_ORIENTATION}")
    print(f"  Generated: {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(SEP2)

def report_baseline(elec_days, gas_bills):
    print(f"\n{'CURRENT ENERGY BASELINE':^70}"); print(SEP)

    ann_imp, ann_exp, note = annual_from_days(elec_days)
    seasons = seasonal_avgs(elec_days)
    net_elec = ann_imp * IMPORT_RATE - ann_exp * FEEDIN_RATE

    print(f"  Basis: {note}  |  EV included throughout")
    print()
    print(f"  {'Season':<10} {'Imp/day':>9} {'Exp/day':>9} {'Days':>6}")
    print(f"  {'─'*38}")
    for name in ["summer","autumn","winter","spring"]:
        if name in seasons:
            s = seasons[name]
            print(f"  {name.capitalize():<10} {s['imp']:>7.1f} kWh {s['exp']:>7.1f} kWh {s['n']:>6}")
    print(f"  {'─'*38}")
    print(f"  {'Annual':<10} {ann_imp:>7.0f} kWh {ann_exp:>7.0f} kWh")
    print()
    print(f"  Electricity import:   ${ann_imp*IMPORT_RATE:>7.0f}/yr")
    print(f"  Solar export income: -${ann_exp*FEEDIN_RATE:>7.0f}/yr  (at {FEEDIN_RATE*100:.0f}c — declining)")
    print(f"  Net electricity:      ${net_elec:>7.0f}/yr")

    if gas_bills:
        g = gas_summary(gas_bills)
        print(f"  Gas (usage+supply):   ${g['annual_cost']:>7.0f}/yr")
        total = net_elec + g["annual_cost"]
        print(f"  {'─'*35}")
        print(f"  Total energy:         ${total:>7.0f}/yr")
        print(f"  (EV running cost included — ~$1,000-1,300/yr of the electricity total)")

    return {"ann_imp": ann_imp, "ann_exp": ann_exp,
            "net_elec": net_elec, "seasons": seasons}

def report_electrification(baseline, gas_bills):
    print(f"\n{'ELECTRIFICATION ANALYSIS':^70}"); print(SEP)
    print(f"  Quotes entered in GG_* constants at top of script (net of all rebates)")

    if not gas_bills: print("  No gas data."); return None

    g = gas_summary(gas_bills)
    heating_kwh = g["heating_mj"] / 3.6 / HEAT_PUMP_COP
    seasons     = baseline["seasons"]
    ann_imp     = baseline["ann_imp"]
    ann_exp     = baseline["ann_exp"]

    heat_dist = {"summer":0.02,"autumn":0.12,"winter":0.52,"spring":0.34}
    solar_avail = {
        "summer": seasons.get("summer",{}).get("exp",11.9) * 90,
        "autumn": seasons.get("autumn",{}).get("exp",7.0)  * 91,
        "winter": seasons.get("winter",{}).get("exp",4.0)  * 92,
        "spring": seasons.get("spring",{}).get("exp",9.4)  * 92,
    }

    total_sol = total_grid = 0
    for s, frac in heat_dist.items():
        hkwh  = heating_kwh * frac
        avail = solar_avail[s]
        used  = min(hkwh, avail * 0.70)
        total_sol  += used
        total_grid += hkwh - used

    ac_load_kwh      = (seasons.get("summer",{}).get("imp",37.8) -
                        (seasons.get("spring",{}).get("imp",21.4)+
                         seasons.get("autumn",{}).get("imp",26.9))/2) * 90
    ac_load_kwh      = max(ac_load_kwh, 0)
    ac_saving_kwh    = ac_load_kwh * 0.40
    ac_saving_dollars = ac_saving_kwh * IMPORT_RATE

    new_imp  = ann_imp + total_grid - ac_saving_kwh
    new_exp  = max(ann_exp - total_sol, 0)
    new_elec = new_imp * IMPORT_RATE - new_exp * FEEDIN_RATE
    gas_disc = g["supply_annual"]

    current_total = baseline["net_elec"] + g["annual_cost"]
    annual_saving = current_total - new_elec + gas_disc

    print(f"\n  Post-electrification model:")
    print(f"    Heating electricity:  {heating_kwh:.0f} kWh/yr  "
          f"(solar {total_sol:.0f} kWh, grid {total_grid:.0f} kWh)")
    print(f"    AC efficiency gain:  -{ac_saving_kwh:.0f} kWh/yr  (Mitsubishi/Daikin + AirTouch)")
    print(f"    New annual import:    {new_imp:.0f} kWh/yr  (was {ann_imp:.0f})")
    print(f"    New annual export:    {new_exp:.0f} kWh/yr  (was {ann_exp:.0f})")
    print()
    print(f"  {'─'*50}")
    print(f"  Current total energy:        ${current_total:>7.0f}/yr")
    print(f"  Post-elec electricity:       ${new_elec:>7.0f}/yr")
    print(f"  Gas disconnection saving:    ${gas_disc:>7.0f}/yr")
    print(f"  Net annual saving (yr 1):    ${annual_saving:>7.0f}/yr")
    print()
    print(f"  Quote breakdown (net of rebates — pending refresh):")
    print(f"    Hot water (Emerald 320):   ${GG_HOT_WATER:>6,}")
    print(f"    Induction cooktop (Smeg):  ${GG_COOKTOP:>6,}")
    print(f"    Heating (ducted):          ${GG_HEATING:>6,}")
    print(f"    BBQ + service:             ${GG_BBQ+GG_SERVICE:>6,}")
    print(f"    {'─'*32}")
    print(f"    Total:                     ${GG_TOTAL:>6,}")

    # ── Rate inflation payback scenarios ────────────────────────────────────
    print(f"\n  PAYBACK UNDER DIFFERENT RATE SCENARIOS:")
    print(f"  {'─'*66}")
    print(f"  {'Scenario':<35} {'Yr 1 saving':>12} {'Payback':>9} {'20yr saving':>12}")
    print(f"  {'─'*66}")

    for label, elec_inf, gas_inf, feedin_chg in INFLATION_SCENARIOS:
        pb, total_saved = npv_payback(annual_saving, elec_inf, gas_inf,
                                      feedin_chg, GG_TOTAL)
        pb_str = f"{pb:.1f} yrs" if pb else ">20 yrs"
        print(f"  {label:<35} ${annual_saving:>10.0f} {pb_str:>9} ${total_saved:>10.0f}")

    print(f"  {'─'*66}")
    print(f"  Note: Gas prices historically rise 8-12%/yr as network costs spread")
    print(f"  across fewer customers (electrification 'death spiral').")
    print(f"  Feed-in tariff trending toward 0c — some retailers already there.")

    return {"new_imp": new_imp, "new_exp": new_exp, "annual_saving": annual_saving}

def report_hvac_sizing():
    print(f"\n{'HVAC SIZING ANALYSIS':^70}"); print(SEP)
    print(f"  House: {HOUSE_FLOOR_M2:.0f}m²  |  {HOUSE_ORIENTATION}  |  "
          f"R{CEILING_R} ceiling / R{WALL_R} walls")
    print()
    if CALC_HEAT_LOAD_KW and CALC_COOL_LOAD_KW:
        print(f"  Heat load calculation results:")
        print(f"    Design heating load:    {CALC_HEAT_LOAD_KW:.1f} kW")
        print(f"    Design cooling load:    {CALC_COOL_LOAD_KW:.1f} kW")
        if CALC_HEATWAVE_KW:
            print(f"    Peak heatwave load:     {CALC_HEATWAVE_KW:.1f} kW")
    else:
        print(f"  Fill in CALC_HEAT_LOAD_KW and CALC_COOL_LOAD_KW at top of script")

def report_battery(baseline, post_elec):
    print(f"\n{'BATTERY ANALYSIS':^70}"); print(SEP)
    configs = [("Current", baseline["ann_imp"], baseline["ann_exp"])]
    if post_elec:
        configs.append(("Post-electrification", post_elec["new_imp"], post_elec["new_exp"]))
    for label, imp, exp in configs:
        cap  = min(exp, BATTERY_KWH * 365 * 0.80)
        save = cap * (IMPORT_RATE - FEEDIN_RATE)
        pb   = BATTERY_COST / save if save else 999
        print(f"  {label}: export {exp:.0f} kWh/yr → battery saves ${save:.0f}/yr → {pb:.1f} yr payback")
    print()
    print(f"  Verdict: Not yet. Revisit when feed-in reaches 0c or after electrification")
    print(f"  increases your evening grid demand (heat pump + no solar = battery earns more).")

def report_solar(baseline):
    print(f"\n{'SOLAR UPGRADE ANALYSIS':^70}"); print(SEP)
    ratio = baseline["ann_exp"] / baseline["ann_imp"]
    print(f"  Import: {baseline['ann_imp']:.0f} kWh/yr  Export: {baseline['ann_exp']:.0f} kWh/yr  "
          f"Ratio: {ratio:.2f}")
    print(f"  Sequence: Electrify first → Battery → Solar upgrade last.")
    print(f"  More panels now just creates more cheap exports at {FEEDIN_RATE*100:.0f}c/kWh.")

def report_ev(ev_results):
    if not ev_results: return
    print(f"\n{'EV CHARGING — SESSION BREAKDOWN':^70}"); print(SEP)
    print(f"  {'ID':<7} {'Date':<12} {'Start':>5} {'End':>5} "
          f"{'kWh':>5} {'Solar':>6} {'Grid':>5} {'Sol%':>5} {'Cost':>7} {'Saving':>8} {'Cover':>6}")
    print(f"  {'─'*68}")
    tot = defaultdict(float)
    for r in ev_results:
        print(f"  {r['session_id']:<7} {r['date']!s:<12} "
              f"{r['start'].strftime('%H:%M'):>5} {r['end'].strftime('%H:%M'):>5} "
              f"{r['kwh']:>5.1f} {r['solar_kwh']:>6.1f} {r['grid_kwh']:>5.1f} "
              f"{r['solar_pct']:>4.0f}% ${r['total_cost']:>5.2f} ${r['saving']:>6.2f}  {r['coverage']:>5}")
        for k in ["kwh","solar_kwh","grid_kwh","total_cost","saving"]:
            tot[k] += r[k]
    print(f"  {'─'*68}")
    sp = tot["solar_kwh"]/tot["kwh"]*100 if tot["kwh"] else 0
    print(f"  {'TOTAL':<7} {'':<25} "
          f"{tot['kwh']:>5.1f} {tot['solar_kwh']:>6.1f} {tot['grid_kwh']:>5.1f} "
          f"{sp:>4.0f}% ${tot['total_cost']:>5.2f} ${tot['saving']:>6.2f}")

def save_csv(elec_days, ev_results, output):
    rows = []
    for d in sorted(elec_days):
        rows.append({"type":"electricity_daily","date":d,
                     "import_kwh":elec_days[d]["import"],
                     "export_kwh":elec_days[d]["export"],"notes":""})
    for r in ev_results:
        rows.append({"type":"ev_session","date":r["date"],
                     "import_kwh":r["grid_kwh"],"export_kwh":"",
                     "notes":f"{r['session_id']} cost=${r['total_cost']} save=${r['saving']}"})
    if rows:
        with open(output,"w",newline="") as f:
            w = csv.DictWriter(f, fieldnames=["type","date","import_kwh","export_kwh","notes"])
            w.writeheader(); w.writerows(rows)
        print(f"\n  Data saved → {output}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Home energy analysis — your home")
    p.add_argument("--elec");     p.add_argument("--powerpal")
    p.add_argument("--gas");      p.add_argument("--byd")
    p.add_argument("--output", default="energy_report.csv")
    p.add_argument("--import-rate", type=float, default=IMPORT_RATE)
    p.add_argument("--feedin-rate", type=float, default=FEEDIN_RATE)
    p.add_argument("--gas-rate",    type=float, default=GAS_RATE)
    args = p.parse_args()

    m = sys.modules[__name__]
    m.IMPORT_RATE = args.import_rate
    m.FEEDIN_RATE = args.feedin_rate
    m.GAS_RATE    = args.gas_rate

    report_header()
    print("\nLoading data …")
    elec_days = load_elec_csvs(args.elec)    if args.elec     else {}
    pp_path   = args.powerpal                if args.powerpal else None
    gas_bills = load_gas_csvs(args.gas)      if args.gas      else []
    sessions  = load_byd_sessions(args.byd)  if args.byd      else []

    if not any([elec_days, gas_bills, sessions]):
        print("No data loaded."); sys.exit(1)

    ev_results = correlate_ev(sessions, pp_path) if sessions and pp_path else []

    baseline  = report_baseline(elec_days, gas_bills)  if elec_days else {}
    post_elec = None
    if baseline and gas_bills:
        post_elec = report_electrification(baseline, gas_bills)
    report_hvac_sizing()
    if baseline:
        report_battery(baseline, post_elec)
        report_solar(baseline)
    if ev_results:
        report_ev(ev_results)

    print(); print(SEP2)
    print(f"  Tariff assumptions: import {m.IMPORT_RATE*100:.1f}c | "
          f"feed-in {m.FEEDIN_RATE*100:.1f}c | gas {m.GAS_RATE*100:.1f}c/MJ | "
          f"supply ${GAS_SUPPLY:.2f}/day")
    print(f"  Quote: ${GG_TOTAL:,} net (6mo old — refresh pending)")
    print(f"  House: {HOUSE_FLOOR_M2:.0f}m² {HOUSE_ORIENTATION} R{CEILING_R}/R{WALL_R} "
          f"design load {CALC_HEAT_LOAD_KW}kW heat / {CALC_COOL_LOAD_KW}kW cool")
    print(SEP2); print()

    if elec_days:
        save_csv(elec_days, ev_results, args.output)

if __name__ == "__main__":
    main()
