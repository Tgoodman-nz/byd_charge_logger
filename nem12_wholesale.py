"""
nem12_wholesale.py — NEM12 interval meter data vs AEMO wholesale cost comparison
==================================================================================
Reads a NEM12 CSV from your retailer (e.g. Energy Australia) and calculates what
you would have paid on an AEMO spot pass-through plan (e.g. Amber) vs your current
fixed tariff, across the full history in the file. Optionally adds a TOU plan
(e.g. OVO) as a third comparison using --tou-peak-rate.

Usage:
    python nem12_wholesale.py data.csv --region VIC
    python nem12_wholesale.py data.csv --region VIC --fixed-rate 0.32 --network-rate 0.10

    # Three-way comparison — EA flat vs OVO TOU vs Amber wholesale:
    python nem12_wholesale.py data.csv --region VIC --bill-csv bills.csv \\
        --tou-peak-rate 0.2938 --tou-offpeak-rate 0.045 \\
        --tou-supply-rate 0.86 --tou-feedin-rate 0.01

Arguments:
    nem12_file        NEM12 CSV from your retailer
    --region          NEM region (required): QLD, NSW, VIC, SA, TAS
    --fixed-rate      Your current flat tariff $/kWh (default: 0.30)
    --network-rate    Network/distribution charge $/kWh added to spot on wholesale plan
                      (default: 0.09 — check your bill for the exact figure)
    --subscription    Monthly plan fee $ for wholesale retailer e.g. Amber (default: 18.00)
    --aemo-cache-dir  Directory for cached AEMO price files (default: aemo_cache)

    TOU comparison (optional — adds a third plan to all output tables):
    --tou-peak-rate      TOU peak rate $/kWh — enables TOU comparison (e.g. 0.2938 for OVO)
    --tou-offpeak-rate   TOU off-peak rate $/kWh (default: 0.045)
    --tou-offpeak-start  Off-peak window start hour 0–23 (default: 0 = midnight)
    --tou-offpeak-end    Off-peak window end hour 0–23 (default: 6 = 6am)
    --tou-supply-rate    TOU plan daily supply $/day excl. GST (default: same as --supply-rate)
    --tou-feedin-rate    TOU plan feed-in tariff $/kWh (default: 0.01)

Notes:
    - NEM12 data uses NEM time (UTC+10, no daylight saving). AEMO prices are also
      in NEM time so no conversion is needed.
    - This script covers grid import only. Solar export is a separate NEM12 register
      and is not included here.
    - Intervals with no AEMO price data (rare for historical data) fall back to the
      fixed rate for the wholesale cost estimate.
    - TOU cost estimates use your historical consumption pattern. If you would shift
      EV or other loads to off-peak, actual TOU savings would be higher than shown.
    - Supply charges are included in the bill comparison but excluded from energy-only totals.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from aemo import spot_prices_for_window

DEFAULT_FIXED_RATE   = 0.30
DEFAULT_NETWORK_RATE = 0.09
DEFAULT_SUBSCRIPTION = 18.00


def parse_nem12(path: str,
                register: str = "E1") -> tuple[str, int, list[tuple[datetime, float]]]:
    """Parse NEM12 CSV for a specific register. Returns (nmi, interval_minutes, intervals).

    Multi-register files (e.g. B1 solar export + E1 consumption in one file) are handled
    by tracking the active register from each 200 row and only collecting 300 rows that
    belong to the requested register.

    register: NEM12 suffix code to extract (default "E1" = grid consumption/import).
              Use "B1" for solar export/feed-in.

    Each interval is (end_datetime_nem, kwh). Intervals are sorted by time.
    The end_datetime of interval N = date + N * interval_minutes.
    """
    nmi             = ""
    interval_min    = 30
    by_dt: dict[datetime, float] = {}  # keyed by interval end-time; last record wins
    active_register = None  # register of the current 200 block
    active_interval = 30    # interval_min for the current 200 block

    with open(path, newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            rec = row[0].strip()

            if rec == "200":
                try:
                    nmi             = row[1].strip()
                    active_register = row[3].strip()  # NMISuffix column
                    active_interval = int(row[8].strip())
                except (IndexError, ValueError):
                    active_register = None
                # Use interval_min from the target register's 200 row
                if active_register == register:
                    interval_min = active_interval

            elif rec == "300" and active_register == register:
                try:
                    date = datetime.strptime(row[1].strip(), "%Y%m%d")
                except (IndexError, ValueError):
                    continue
                n = 1440 // active_interval
                for i in range(n):
                    try:
                        kwh = float(row[2 + i].strip())
                    except (IndexError, ValueError):
                        kwh = 0.0
                    end_dt = date + timedelta(minutes=(i + 1) * active_interval)
                    by_dt[end_dt] = kwh  # last record wins — handles correction records

    intervals = sorted(by_dt.items())
    return nmi, interval_min, intervals


def _bucket_end(dt: datetime, interval_min: int) -> datetime:
    """Map an AEMO 5-min SETTLEMENTDATE to its NEM12 interval-end bucket.

    SETTLEMENTDATE is the end of a 5-min interval. The NEM12 bucket is the
    nearest interval_min boundary at or after dt.
    """
    total_min = dt.hour * 60 + dt.minute
    if total_min == 0:
        return dt  # midnight = end of previous day's last interval
    ceil_min = ((total_min + interval_min - 1) // interval_min) * interval_min
    return dt.replace(hour=0, minute=0, second=0) + timedelta(minutes=ceil_min)


def build_spot_lookup(intervals: list[tuple[datetime, float]],
                      region: str, cache_dir: Path,
                      interval_min: int) -> dict[datetime, float]:
    """Fetch AEMO 5-min prices for the full data range, averaged into interval_min buckets.

    Returns {interval_end_dt: avg_rrp_$/MWh}.
    """
    if not intervals:
        return {}

    start_dt = intervals[0][0] - timedelta(hours=1)
    end_dt   = intervals[-1][0] + timedelta(hours=1)

    print(f"\nFetching AEMO {region} prices {start_dt.date()} → {end_dt.date()} …")
    raw = spot_prices_for_window(start_dt, end_dt, region, cache_dir)

    if not raw:
        print("  Warning: no AEMO price data returned")
        return {}

    print(f"  Got {len(raw):,} dispatch intervals")

    buckets: dict[datetime, list[float]] = defaultdict(list)
    for p in raw:
        buckets[_bucket_end(p["dt"], interval_min)].append(p["rrp"])

    return {dt: sum(rrps) / len(rrps) for dt, rrps in buckets.items()}


def months_in_range(start: datetime, end: datetime) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def load_bills(path: str) -> list[dict]:
    """Load bills.csv. Returns list of bill dicts sorted by period_start."""
    bills = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    start    = datetime.strptime(row["period_start"].strip(), "%Y-%m-%d").date()
                    end      = datetime.strptime(row["period_end"].strip(),   "%Y-%m-%d").date()
                    total    = float(row["total_cost_incl_gst"].strip())
                    paid_str   = row.get("actual_paid_incl_gst", "").strip()
                    relief_str = row.get("govt_relief", "").strip()
                    paid       = float(paid_str)   if paid_str   else None
                    relief     = float(relief_str) if relief_str else 0.0
                    bills.append({"start": start, "end": end, "total": total,
                                  "paid": paid, "relief": relief})
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        print(f"  Warning: bill CSV not found: {path}")
    return sorted(bills, key=lambda b: b["start"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare NEM12 interval meter data against AEMO wholesale prices")
    parser.add_argument("nem12_file",
                        help="NEM12 CSV from your retailer")
    parser.add_argument("--region",        required=True,
                        choices=["QLD", "NSW", "VIC", "SA", "TAS"],
                        help="NEM region")
    parser.add_argument("--fixed-rate",    type=float, default=DEFAULT_FIXED_RATE,
                        metavar="RATE",
                        help=f"Current flat tariff $/kWh (default {DEFAULT_FIXED_RATE})")
    parser.add_argument("--network-rate",  type=float, default=DEFAULT_NETWORK_RATE,
                        metavar="RATE",
                        help=f"Network/distribution $/kWh added to spot (default {DEFAULT_NETWORK_RATE})")
    parser.add_argument("--subscription",  type=float, default=DEFAULT_SUBSCRIPTION,
                        metavar="DOLLARS",
                        help=f"Monthly plan fee $ for wholesale retailer (default {DEFAULT_SUBSCRIPTION})")
    parser.add_argument("--aemo-cache-dir", default="aemo_cache", metavar="DIR",
                        help="Directory for cached AEMO data (default aemo_cache)")
    parser.add_argument("--register",       default="E1", metavar="SUFFIX",
                        help="NEM12 NMISuffix to analyse (default E1 = grid consumption). "
                             "Use B1 for solar export.")
    parser.add_argument("--feedin-rate",  type=float, default=0.015, metavar="RATE",
                        help="EA fixed feed-in tariff $/kWh for comparison (default 0.015)")
    parser.add_argument("--bill-csv",    default=None, metavar="FILE",
                        help="CSV of actual bills for comparison (e.g. bills.csv)")
    parser.add_argument("--supply-rate", type=float, default=1.68, metavar="RATE",
                        help="Daily supply charge $/day excl. GST added to Amber estimate "
                             "(default 1.68 — check your bill)")
    # TOU plan comparison (optional — enabled by providing --tou-peak-rate)
    parser.add_argument("--tou-peak-rate",     type=float, default=None,  metavar="RATE",
                        help="TOU plan peak rate $/kWh — enables TOU comparison (e.g. 0.2938 for OVO)")
    parser.add_argument("--tou-offpeak-rate",  type=float, default=0.045, metavar="RATE",
                        help="TOU plan off-peak rate $/kWh (default 0.045)")
    parser.add_argument("--tou-offpeak-start", type=int,   default=0,     metavar="HOUR",
                        help="Off-peak window start hour 0-23 (default 0 = midnight)")
    parser.add_argument("--tou-offpeak-end",   type=int,   default=6,     metavar="HOUR",
                        help="Off-peak window end hour 0-23 (default 6 = 6am)")
    parser.add_argument("--tou-supply-rate",   type=float, default=None,  metavar="RATE",
                        help="TOU plan daily supply $/day excl. GST (default: same as --supply-rate)")
    parser.add_argument("--tou-feedin-rate",   type=float, default=0.01,  metavar="RATE",
                        help="TOU plan feed-in tariff $/kWh (default 0.01)")
    args = parser.parse_args()

    if args.tou_supply_rate is None:
        args.tou_supply_rate = args.supply_rate
    tou_enabled = args.tou_peak_rate is not None

    print("\nNEM12 Wholesale Cost Comparison")
    print("═" * 56)

    print(f"\nParsing {args.nem12_file} (register {args.register}) …")
    nmi, interval_min, intervals = parse_nem12(args.nem12_file, register=args.register)

    if not intervals:
        print("No interval data found in NEM12 file.")
        sys.exit(1)

    total_kwh = sum(kwh for _, kwh in intervals)
    date_from = intervals[0][0].date()
    date_to   = (intervals[-1][0] - timedelta(minutes=interval_min)).date()
    n_days    = (date_to - date_from).days + 1
    n_months  = months_in_range(intervals[0][0], intervals[-1][0])

    print(f"  Meter:       {nmi}")
    print(f"  Period:      {date_from} → {date_to}  ({n_days} days, {n_months} months)")
    print(f"  Intervals:   {len(intervals):,}  ({interval_min}-min)")
    print(f"  Total usage: {total_kwh:.1f} kWh  "
          f"(avg {total_kwh/n_days:.2f} kWh/day,  {total_kwh/n_days*365:.0f} kWh/yr)")

    _, _, export_intervals = parse_nem12(args.nem12_file, register="B1")
    if export_intervals:
        total_export_kwh = sum(kwh for _, kwh in export_intervals)
        print(f"  Solar export:  {total_export_kwh:.1f} kWh (B1, {len(export_intervals):,} intervals)  "
              f"(avg {total_export_kwh/n_days:.2f} kWh/day,  {total_export_kwh/n_days*365:.0f} kWh/yr)")

    spot_lookup = build_spot_lookup(
        intervals, args.region, Path(args.aemo_cache_dir), interval_min)

    n_priced   = sum(1 for dt, _ in intervals if dt in spot_lookup)
    n_unpriced = len(intervals) - n_priced
    if n_unpriced:
        pct = n_unpriced / len(intervals) * 100
        print(f"  Warning: {n_unpriced:,} intervals ({pct:.1f}%) have no AEMO price — "
              f"falling back to fixed rate for those")

    bills    = load_bills(args.bill_csv) if args.bill_csv else []
    bill_acc = [{"kwh": 0.0, "wholesale": 0.0, "feedin_amber": 0.0,
                 "tou": 0.0, "feedin_tou": 0.0} for _ in bills]

    # ── Per-interval cost calculation ────────────────────────────────────────
    fixed_total     = 0.0
    wholesale_total = 0.0
    tou_total       = 0.0

    monthly: dict = defaultdict(lambda: {
        "kwh": 0.0, "fixed": 0.0, "wholesale": 0.0, "tou": 0.0,
        "weighted_spot": 0.0, "kwh_priced": 0.0,
        "feedin": 0.0, "feedin_tou": 0.0, "export_kwh": 0.0,
    })

    bands = [
        ("Negative",  float("-inf"),  0),
        ("0–5c",       0,   5),
        ("5–10c",      5,  10),
        ("10–15c",    10,  15),
        ("15–20c",    15,  20),
        ("20–30c",    20,  30),
        ("30–50c",    30,  50),
        (">50c",      50,  float("inf")),
    ]
    band_kwh  = defaultdict(float)
    band_cost = defaultdict(float)

    for end_dt, kwh in intervals:
        rrp_mwh = spot_lookup.get(end_dt)

        fixed_cost = kwh * args.fixed_rate

        if rrp_mwh is not None:
            wholesale_rate = (rrp_mwh / 1000) + args.network_rate
            wholesale_cost = kwh * wholesale_rate
            spot_c_kwh     = rrp_mwh / 10
        else:
            wholesale_cost = fixed_cost
            spot_c_kwh     = args.fixed_rate * 100

        if tou_enabled:
            start_hour = (end_dt - timedelta(minutes=interval_min)).hour
            if args.tou_offpeak_start < args.tou_offpeak_end:
                is_offpeak = args.tou_offpeak_start <= start_hour < args.tou_offpeak_end
            else:
                is_offpeak = start_hour >= args.tou_offpeak_start or start_hour < args.tou_offpeak_end
            tou_cost = kwh * (args.tou_offpeak_rate if is_offpeak else args.tou_peak_rate)
        else:
            tou_cost = 0.0

        fixed_total     += fixed_cost
        wholesale_total += wholesale_cost
        tou_total       += tou_cost

        if bills:
            d = (end_dt - timedelta(minutes=interval_min)).date()
            for bi, b in enumerate(bills):
                if b["start"] <= d <= b["end"]:
                    bill_acc[bi]["kwh"]       += kwh
                    bill_acc[bi]["wholesale"] += wholesale_cost
                    bill_acc[bi]["tou"]       += tou_cost
                    break

        mk = end_dt.strftime("%Y-%m")
        monthly[mk]["kwh"]       += kwh
        monthly[mk]["fixed"]     += fixed_cost
        monthly[mk]["wholesale"] += wholesale_cost
        monthly[mk]["tou"]       += tou_cost
        if rrp_mwh is not None:
            monthly[mk]["weighted_spot"] += spot_c_kwh * kwh
            monthly[mk]["kwh_priced"]    += kwh

        for name, lo, hi in bands:
            if lo <= spot_c_kwh < hi:
                band_kwh[name]  += kwh
                band_cost[name] += wholesale_cost
                break

    # ── Solar export (B1) feed-in ─────────────────────────────────────────────
    feedin_total_amber = 0.0
    feedin_total_tou   = 0.0
    for end_dt, kwh_exp in export_intervals:
        rrp_mwh = spot_lookup.get(end_dt)
        mk = end_dt.strftime("%Y-%m")
        monthly[mk]["export_kwh"] += kwh_exp
        if rrp_mwh is not None:
            feedin = kwh_exp * (rrp_mwh / 1000)
            feedin_total_amber       += feedin
            monthly[mk]["feedin"]    += feedin
        if tou_enabled:
            feedin_tou                   = kwh_exp * args.tou_feedin_rate
            feedin_total_tou             += feedin_tou
            monthly[mk]["feedin_tou"]    += feedin_tou
        if bills:
            d = (end_dt - timedelta(minutes=interval_min)).date()
            for bi, b in enumerate(bills):
                if b["start"] <= d <= b["end"]:
                    if rrp_mwh is not None:
                        bill_acc[bi]["feedin_amber"] += kwh_exp * (rrp_mwh / 1000)
                    if tou_enabled:
                        bill_acc[bi]["feedin_tou"]   += kwh_exp * args.tou_feedin_rate
                    break

    subscription_total   = n_months * args.subscription
    avg_spot_all         = sum(
        m["weighted_spot"] for m in monthly.values()
    ) / max(sum(m["kwh_priced"] for m in monthly.values()), 0.001)

    W = 56

    # ── Cost comparison ───────────────────────────────────────────────────────
    print(f"\n{'COST COMPARISON':^{W}}")
    print("═" * W)
    print(f"  Fixed rate ({args.fixed_rate*100:.1f}c/kWh):              ${fixed_total:>8.2f}")
    if tou_enabled:
        tou_net = tou_total - feedin_total_tou
        print(f"  TOU plan ({args.tou_peak_rate*100:.2f}c peak / {args.tou_offpeak_rate*100:.2f}c off-peak):")
        print(f"    Import cost:                   ${tou_total:>8.2f}")
        if export_intervals:
            print(f"    Feed-in ({args.tou_feedin_rate*100:.2f}c/kWh):         ${feedin_total_tou:>8.2f}")
        print(f"    Total (energy only):           ${tou_net:>8.2f}")
    print(f"  Wholesale (spot + {args.network_rate*100:.0f}c network):")
    print(f"    Import cost:                   ${wholesale_total:>8.2f}")
    if export_intervals:
        fi_label = "Feed-in credit" if feedin_total_amber >= 0 else "Feed-in cost  "
        print(f"    {fi_label} (B1 at spot): ${feedin_total_amber:>8.2f}")
    print(f"    Subscription ({n_months} × ${args.subscription:.0f}/mo):   ${subscription_total:>8.2f}")
    amber_net = wholesale_total - feedin_total_amber + subscription_total
    print(f"    Total:                         ${amber_net:>8.2f}")
    print("  " + "─" * (W - 2))

    if tou_enabled:
        tou_vs_fixed = fixed_total - tou_net
        print(f"  TOU vs fixed:    {'CHEAPER' if tou_vs_fixed >= 0 else 'MORE EXP.'} "
              f"by ${abs(tou_vs_fixed):.2f} over {n_months} months (energy only, excl. supply)")
    saving_incl_fi = fixed_total - amber_net
    if saving_incl_fi >= 0:
        print(f"  Wholesale CHEAPER by ${saving_incl_fi:.2f} over {n_months} months (incl. sub + feed-in)")
    else:
        print(f"  Wholesale MORE EXPENSIVE by ${abs(saving_incl_fi):.2f} over {n_months} months (incl. sub + feed-in)")

    print(f"  Consumption-weighted avg spot:  {avg_spot_all:.1f}c/kWh")

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    if tou_enabled:
        WM = 96
        print(f"\n{'MONTHLY BREAKDOWN':^{WM}}")
        print("═" * WM)
        print(f"  {'Month':<9} {'kWh':>6} {'Fixed':>8} {'TOU':>8} {'Wholesale':>10} "
              f"{'TOU-save':>9} {'Amb-save':>9} {'Avg spot':>9}")
        print("  " + "─" * (WM - 2))
        for mk in sorted(monthly):
            m       = monthly[mk]
            ea_fi   = m["export_kwh"] * args.feedin_rate
            tou_fi  = m["feedin_tou"]
            amb_fi  = m["feedin"]
            tou_sav = (m["fixed"] - ea_fi) - (m["tou"] - tou_fi)
            amb_sav = (m["fixed"] - ea_fi) - (m["wholesale"] - amb_fi)
            avg_s   = (m["weighted_spot"] / m["kwh_priced"]) if m["kwh_priced"] > 0 else 0
            ts_sign = "+" if tou_sav >= 0 else ""
            as_sign = "+" if amb_sav >= 0 else ""
            print(f"  {mk:<9} {m['kwh']:>6.1f} ${m['fixed']:>7.2f} ${m['tou']:>7.2f} ${m['wholesale']:>9.2f} "
                  f" {ts_sign}${tou_sav:>7.2f}  {as_sign}${amb_sav:>7.2f}  {avg_s:>6.1f}c")
        print("  " + "─" * (WM - 2))
        print(f"  *-save = saving vs fixed rate, energy only (excl. supply, subscription, GST)")
        print(f"  TOU uses historical consumption — overnight-shifted EV charging would improve TOU-save")
    else:
        WM = 82
        print(f"\n{'MONTHLY BREAKDOWN':^{WM}}")
        print("═" * WM)
        print(f"  {'Month':<9} {'kWh':>6} {'Fixed':>8} {'Wholesale':>10} "
              f"{'EA f/in':>8} {'Amb f/in':>9} {'Saving':>9} {'Avg spot':>9}")
        print("  " + "─" * (WM - 2))
        for mk in sorted(monthly):
            m        = monthly[mk]
            ea_fi    = m["export_kwh"] * args.feedin_rate
            amb_fi   = m["feedin"]
            saving   = (m["fixed"] - ea_fi) - (m["wholesale"] - amb_fi)
            avg_s    = (m["weighted_spot"] / m["kwh_priced"]) if m["kwh_priced"] > 0 else 0
            s_sign   = "+" if saving >= 0 else ""
            amb_str  = f"+${amb_fi:.2f}" if amb_fi >= 0 else f"-${abs(amb_fi):.2f}"
            print(f"  {mk:<9} {m['kwh']:>6.1f} ${m['fixed']:>7.2f} ${m['wholesale']:>9.2f} "
                  f" ${ea_fi:>6.2f}  {amb_str:>8}  {s_sign}${saving:>7.2f}  {avg_s:>6.1f}c")
        print("  " + "─" * (WM - 2))
        print(f"  Saving = energy only (excl. supply charge, subscription, GST) — see bill comparison for true totals")

    # ── Price distribution ────────────────────────────────────────────────────
    print(f"\n{'SPOT PRICE DISTRIBUTION (by consumption)':^{W}}")
    print("═" * W)
    print(f"  {'Band':<12} {'kWh':>8} {'Share':>7}")
    print("  " + "─" * (W - 2))
    for name, _, _ in bands:
        kwh = band_kwh.get(name, 0.0)
        if kwh > 0:
            share = kwh / total_kwh * 100
            bar   = "█" * int(share / 2)
            print(f"  {name:<12} {kwh:>8.1f}  {share:>5.1f}%  {bar}")
    print(f"\n  Negative price = grid paying you to consume (rare but real)")
    print(f"  Daily supply charge excluded — same on both plans")

    # ── Actual bill comparison ────────────────────────────────────────────────
    if bills:
        W2 = 100 if tou_enabled else 76
        print(f"\n{'ACTUAL BILL COMPARISON':^{W2}}")
        print("═" * W2)
        if tou_enabled:
            print(f"  {'Period':<23} {'Days':>4}  {'EA paid':>9}  {'Relief':>7}  "
                  f"{'OVO est':>9}  {'OVO-save':>9}  {'Amber est':>9}  {'Amb-save':>9}")
            print(f"  {'':23} {'':>4}  {'(net rel)':>9}  {'':>7}  "
                  f"{'(net rel)':>9}  {'':>9}  {'(net rel)':>9}")
        else:
            print(f"  {'Period':<23} {'Days':>4}  {'EA paid':>9}  {'Relief':>7}  {'Amber est':>9}  {'Saving':>9}")
            print(f"  {'':23} {'':>4}  {'(net rel)':>9}  {'':>7}  {'(net rel)':>9}")
        print("  " + "─" * (W2 - 2))

        total_paid  = 0.0
        total_amber = 0.0
        total_ovo   = 0.0
        has_data    = False

        for i, b in enumerate(bills):
            if b["paid"] is None:
                continue
            days   = (b["end"] - b["start"]).days + 1
            months = days / 30.44

            has_nem12 = bill_acc[i]["kwh"] > 0 or b["start"] >= date_from
            if not has_nem12:
                period = f"{b['start']}→{b['end']}"
                print(f"  {period:<23} {days:>4}  {'(no NEM12 data)':>30}")
                continue

            supply     = days * args.supply_rate
            sub        = months * args.subscription
            amber_excl = bill_acc[i]["wholesale"] - bill_acc[i]["feedin_amber"] + supply + sub
            amber_incl = amber_excl * 1.1 - b["relief"]
            amb_saving = b["paid"] - amber_incl

            total_paid  += b["paid"]
            total_amber += amber_incl
            has_data     = True

            period     = f"{b['start']}→{b['end']}"
            relief_str = f"${b['relief']:.2f}" if b["relief"] > 0 else "-"

            if tou_enabled:
                tou_supply = days * args.tou_supply_rate
                tou_excl   = bill_acc[i]["tou"] - bill_acc[i]["feedin_tou"] + tou_supply
                tou_incl   = tou_excl * 1.1 - b["relief"]
                tou_saving = b["paid"] - tou_incl
                total_ovo += tou_incl
                tou_s_str  = f"+${tou_saving:.2f}" if tou_saving >= 0 else f"-${abs(tou_saving):.2f}"
                amb_s_str  = f"+${amb_saving:.2f}" if amb_saving >= 0 else f"-${abs(amb_saving):.2f}"
                print(f"  {period:<23} {days:>4}  ${b['paid']:>8.2f}  {relief_str:>7}  "
                      f"${tou_incl:>8.2f}  {tou_s_str:>9}  ${amber_incl:>8.2f}  {amb_s_str:>9}")
            else:
                amb_s_str = f"+${amb_saving:.2f}" if amb_saving >= 0 else f"-${abs(amb_saving):.2f}"
                print(f"  {period:<23} {days:>4}  ${b['paid']:>8.2f}  {relief_str:>7}  ${amber_incl:>8.2f}  {amb_s_str:>9}")

        if has_data:
            print("  " + "─" * (W2 - 2))
            if tou_enabled:
                tou_tot_sav = total_paid - total_ovo
                amb_tot_sav = total_paid - total_amber
                tou_s_str   = f"+${tou_tot_sav:.2f}" if tou_tot_sav >= 0 else f"-${abs(tou_tot_sav):.2f}"
                amb_s_str   = f"+${amb_tot_sav:.2f}" if amb_tot_sav >= 0 else f"-${abs(amb_tot_sav):.2f}"
                print(f"  {'Total':<23} {'':>4}  ${total_paid:>8.2f}  {'':>7}  "
                      f"${total_ovo:>8.2f}  {tou_s_str:>9}  ${total_amber:>8.2f}  {amb_s_str:>9}")
            else:
                tot_sav     = total_paid - total_amber
                tot_sav_str = f"+${tot_sav:.2f}" if tot_sav >= 0 else f"-${abs(tot_sav):.2f}"
                print(f"  {'Total':<23} {'':>4}  ${total_paid:>8.2f}  {'':>7}  ${total_amber:>8.2f}  {tot_sav_str:>9}")
            print("═" * W2)
            if tou_enabled:
                ovo_verdict = "CHEAPER" if total_ovo < total_paid else "MORE EXPENSIVE"
                print(f"\n  OVO would have been {ovo_verdict} by ${abs(total_paid - total_ovo):.2f} "
                      f"over this period (historical pattern — see note below)")
                amb_verdict = "CHEAPER" if total_amber < total_paid else "MORE EXPENSIVE"
                print(f"  Amber would have been {amb_verdict} by ${abs(total_paid - total_amber):.2f} "
                      f"over this period")
            else:
                amb_verdict = "CHEAPER" if total_amber < total_paid else "MORE EXPENSIVE"
                print(f"\n  Amber would have been {amb_verdict} by ${abs(total_paid - total_amber):.2f} "
                      f"over this period")

        if tou_enabled:
            print(f"\n  OVO est  = TOU energy ({args.tou_peak_rate*100:.2f}c peak / "
                  f"{args.tou_offpeak_rate*100:.2f}c off-peak, {args.tou_offpeak_start:02d}:00–{args.tou_offpeak_end:02d}:00)")
            print(f"           + supply (${args.tou_supply_rate:.2f}/day excl. GST) "
                  f"- feed-in ({args.tou_feedin_rate*100:.2f}c/kWh), incl. 10% GST, net of relief")
            print(f"  *** OVO estimate reflects your HISTORICAL load pattern. Shifting EV charging")
            print(f"      and other loads to off-peak hours would materially improve the OVO saving.")
        print(f"  Amber est = wholesale energy + supply (${args.supply_rate:.2f}/day excl. GST) "
              f"+ sub (${args.subscription:.2f}/mo), incl. 10% GST")
        print(f"  Both plans: EA paid and estimates are net of govt relief where applicable")


if __name__ == "__main__":
    main()
