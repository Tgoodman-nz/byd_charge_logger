"""
nem12_wholesale.py — NEM12 interval meter data vs AEMO wholesale cost comparison
==================================================================================
Reads a NEM12 CSV from your retailer (e.g. Energy Australia) and calculates what
you would have paid on an AEMO spot pass-through plan (e.g. Amber) vs your current
fixed tariff, across the full history in the file.

Usage:
    python nem12_wholesale.py data.csv --region VIC
    python nem12_wholesale.py data.csv --region VIC --fixed-rate 0.32 --network-rate 0.10

Arguments:
    nem12_file        NEM12 CSV from your retailer
    --region          NEM region (required): QLD, NSW, VIC, SA, TAS
    --fixed-rate      Your current flat tariff $/kWh (default: 0.30)
    --network-rate    Network/distribution charge $/kWh added to spot on wholesale plan
                      (default: 0.09 — check your bill for the exact figure)
    --subscription    Monthly plan fee $ for wholesale retailer e.g. Amber (default: 18.00)
    --aemo-cache-dir  Directory for cached AEMO price files (default: aemo_cache)

Notes:
    - NEM12 data uses NEM time (UTC+10, no daylight saving). AEMO prices are also
      in NEM time so no conversion is needed.
    - This script covers grid import only. Solar export is a separate NEM12 register
      and is not included here.
    - Intervals with no AEMO price data (rare for historical data) fall back to the
      fixed rate for the wholesale cost estimate.
    - Daily supply charge is identical on both plans and is excluded from the comparison.
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
    intervals       = []
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
                    intervals.append((end_dt, kwh))

    intervals.sort(key=lambda x: x[0])
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
    args = parser.parse_args()

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
          f"(avg {total_kwh/n_days:.2f} kWh/day)")

    _, _, export_intervals = parse_nem12(args.nem12_file, register="B1")
    if export_intervals:
        total_export_kwh = sum(kwh for _, kwh in export_intervals)
        print(f"  Solar export:  {total_export_kwh:.1f} kWh (B1, {len(export_intervals):,} intervals)")

    spot_lookup = build_spot_lookup(
        intervals, args.region, Path(args.aemo_cache_dir), interval_min)

    n_priced   = sum(1 for dt, _ in intervals if dt in spot_lookup)
    n_unpriced = len(intervals) - n_priced
    if n_unpriced:
        pct = n_unpriced / len(intervals) * 100
        print(f"  Warning: {n_unpriced:,} intervals ({pct:.1f}%) have no AEMO price — "
              f"falling back to fixed rate for those")

    bills    = load_bills(args.bill_csv) if args.bill_csv else []
    bill_acc = [{"kwh": 0.0, "wholesale": 0.0, "feedin_amber": 0.0} for _ in bills]

    # ── Per-interval cost calculation ────────────────────────────────────────
    fixed_total     = 0.0
    wholesale_total = 0.0

    monthly: dict = defaultdict(lambda: {
        "kwh": 0.0, "fixed": 0.0, "wholesale": 0.0,
        "weighted_spot": 0.0, "kwh_priced": 0.0,
        "feedin": 0.0, "export_kwh": 0.0,
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

        fixed_total     += fixed_cost
        wholesale_total += wholesale_cost

        if bills:
            d = (end_dt - timedelta(minutes=interval_min)).date()
            for bi, b in enumerate(bills):
                if b["start"] <= d <= b["end"]:
                    bill_acc[bi]["kwh"]       += kwh
                    bill_acc[bi]["wholesale"] += wholesale_cost
                    break

        mk = end_dt.strftime("%Y-%m")
        monthly[mk]["kwh"]       += kwh
        monthly[mk]["fixed"]     += fixed_cost
        monthly[mk]["wholesale"] += wholesale_cost
        if rrp_mwh is not None:
            monthly[mk]["weighted_spot"] += spot_c_kwh * kwh
            monthly[mk]["kwh_priced"]    += kwh

        for name, lo, hi in bands:
            if lo <= spot_c_kwh < hi:
                band_kwh[name]  += kwh
                band_cost[name] += wholesale_cost
                break

    # ── Solar export (B1) feed-in at spot ────────────────────────────────────
    feedin_total_amber = 0.0
    for end_dt, kwh_exp in export_intervals:
        rrp_mwh = spot_lookup.get(end_dt)
        if rrp_mwh is None:
            continue
        feedin = kwh_exp * (rrp_mwh / 1000)
        feedin_total_amber += feedin
        mk = end_dt.strftime("%Y-%m")
        monthly[mk]["feedin"]     += feedin
        monthly[mk]["export_kwh"] += kwh_exp
        if bills:
            d = (end_dt - timedelta(minutes=interval_min)).date()
            for bi, b in enumerate(bills):
                if b["start"] <= d <= b["end"]:
                    bill_acc[bi]["feedin_amber"] += feedin
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
    print(f"  Wholesale (spot + {args.network_rate*100:.0f}c network):")
    print(f"    Import cost:                   ${wholesale_total:>8.2f}")
    if export_intervals:
        fi_label = "Feed-in credit" if feedin_total_amber >= 0 else "Feed-in cost  "
        print(f"    {fi_label} (B1 at spot): ${feedin_total_amber:>8.2f}")
    print(f"    Subscription ({n_months} × ${args.subscription:.0f}/mo):   ${subscription_total:>8.2f}")
    amber_net = wholesale_total - feedin_total_amber + subscription_total
    print(f"    Total:                         ${amber_net:>8.2f}")
    print("  " + "─" * (W - 2))

    saving_incl_fi = fixed_total - amber_net
    if saving_incl_fi >= 0:
        print(f"  Wholesale CHEAPER by ${saving_incl_fi:.2f} over {n_months} months (incl. sub + feed-in)")
    else:
        print(f"  Wholesale MORE EXPENSIVE by ${abs(saving_incl_fi):.2f} over {n_months} months (incl. sub + feed-in)")

    print(f"  Consumption-weighted avg spot:  {avg_spot_all:.1f}c/kWh")

    # ── Monthly breakdown ─────────────────────────────────────────────────────
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
        W2 = 76
        print(f"\n{'ACTUAL BILL COMPARISON':^{W2}}")
        print("═" * W2)
        print(f"  {'Period':<23} {'Days':>4}  {'EA paid':>9}  {'Relief':>7}  {'Amber est':>9}  {'Saving':>9}")
        print(f"  {'':23} {'':>4}  {'(net rel)':>9}  {'':>7}  {'(net rel)':>9}")
        print("  " + "─" * (W2 - 2))

        total_paid  = 0.0
        total_amber = 0.0
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
            saving     = b["paid"] - amber_incl

            total_paid  += b["paid"]
            total_amber += amber_incl
            has_data     = True

            period      = f"{b['start']}→{b['end']}"
            saving_str  = f"+${saving:.2f}" if saving >= 0 else f"-${abs(saving):.2f}"
            relief_str  = f"${b['relief']:.2f}" if b["relief"] > 0 else "-"
            print(f"  {period:<23} {days:>4}  ${b['paid']:>8.2f}  {relief_str:>7}  ${amber_incl:>8.2f}  {saving_str:>9}")

        if has_data:
            total_saving     = total_paid - total_amber
            total_saving_str = f"+${total_saving:.2f}" if total_saving >= 0 else f"-${abs(total_saving):.2f}"
            print("  " + "─" * (W2 - 2))
            print(f"  {'Total':<23} {'':>4}  ${total_paid:>8.2f}  {'':>7}  ${total_amber:>8.2f}  {total_saving_str:>9}")
            print("═" * W2)
            if total_saving >= 0:
                print(f"\n  Amber would have been CHEAPER by ${total_saving:.2f} over this period")
            else:
                print(f"\n  Amber would have been MORE EXPENSIVE by ${abs(total_saving):.2f} over this period")
        print(f"\n  Amber est = wholesale energy + supply (${args.supply_rate:.2f}/day) "
              f"+ sub (${args.subscription:.2f}/mo), incl. 10% GST")
        print(f"  Both plans: EA paid and Amber est are net of govt relief where applicable")


if __name__ == "__main__":
    main()
