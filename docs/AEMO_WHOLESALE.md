# AEMO Wholesale Pricing

## Background

### What is AEMO?

The **Australian Energy Market Operator (AEMO)** runs the **National Electricity Market (NEM)** — the interconnected grid covering Queensland, NSW, Victoria, South Australia, and Tasmania. Every five minutes, AEMO runs a dispatch auction where generators bid to supply electricity. The clearing price for that five-minute interval is called the **spot price**, measured in $/MWh.

The spot price swings wildly — from near zero overnight when demand is low, to thousands of dollars per MWh during a heatwave. It can even go **negative** when there is more renewable supply than the grid can absorb (and generators pay to keep running rather than shut down).

Your standard electricity retailer buys on the wholesale market, adds a margin, and sells to you at a flat rate. You never see the spot price.

### What is Amber Electric?

[Amber](https://www.amber.com.au/) is an electricity retailer that passes the wholesale spot price directly to customers, adding only:

- A **network/distribution charge** (~9–12c/kWh depending on your distributor)
- A flat **monthly subscription** (~$18/mo)

If spot prices are low — for example, during the middle of the day when solar generation is high — you pay almost nothing for energy. When prices go **negative**, Amber credits you. The trade-off is exposure to price spikes.

### Why does this matter for EV charging?

EV charging is a large, flexible load. Charging during low-price periods (midday solar surplus, overnight low demand) can dramatically reduce costs on a wholesale plan. This tool estimates what your sessions would have cost on Amber versus your current fixed rate.

---

## How It Works

### Data sources

AEMO publishes historical dispatch prices on [NEMWeb](https://www.nemweb.com.au/). Two paths are used depending on whether the month is complete:

| Situation | Source | URL path |
|-----------|--------|----------|
| Completed months | MMSDM monthly archive | `/Data_Archive/Wholesale_Electricity/MMSDM/` |
| Current month | DispatchIS daily ZIPs | `/Reports/ARCHIVE/DispatchIS_Reports/` |

Both use the same MMSDM I/D row CSV format — the fetching logic is shared.

**Note:** AEMO migrated off `aemo.com.au` on 30 April 2026. All data is now at `nemweb.com.au`.

### MMSDM monthly archive (completed months)

One large ZIP per month containing all 5-minute DISPATCHPRICE records for every NEM region. Downloaded once and cached to disk in `aemo_cache/`. Format:

```
I,DISPATCH,PRICE,4,SETTLEMENTDATE,RUNNO,REGIONID,INTERVENTION,...,RRP,...
D,DISPATCH,PRICE,4,2026/04/01 00:05:00,1,VIC1,0,...,45.23,...
```

`SETTLEMENTDATE` is the **end** of the 5-minute interval. `RRP` is $/MWh.

### DispatchIS daily ZIPs (current month)

Each daily ZIP is a **ZIP-of-ZIPs** — it contains ~288 nested ZIPs, one per 5-minute dispatch interval for that day:

```
PUBLIC_DISPATCHIS_20260506.zip
  ├── PUBLIC_DISPATCHIS_202605060005_XXXXXXXXX.zip   ← 00:05 interval
  ├── PUBLIC_DISPATCHIS_202605060010_XXXXXXXXX.zip   ← 00:10 interval
  └── ...  (288 total)
```

Each inner ZIP contains a small CSV with the same MMSDM format. `aemo.py` opens all inner ZIPs and parses them.

**Publication lag:** Daily ZIPs appear in the ARCHIVE roughly 1–2 days after the day ends. Sessions from the last 1–2 days will show a warning and be skipped; they will price automatically on the next run once the data is published.

### NEM time

All AEMO timestamps are in **NEM time = UTC+10** (no daylight saving). Your session timestamps are in local time. The `--utc-offset` argument handles conversion:

- AEST (standard): `--utc-offset 10` (default)
- AEDT (summer): `--utc-offset 11`

---

## Caching

### On-disk cache (`aemo_cache/`)

Completed months are cached as `DISPATCHPRICE_YYYYMM_{region}.csv` after the first download. Subsequent runs read from this file instantly — no network request. The current month is never written to disk because new days are published continuously; it is re-fetched on every run.

### In-memory cache

Within a single run, `aemo.py` holds fetched month data in a Python dict so multiple sessions in the same month only trigger one download.

### Amber results cache (`amber_cache.csv`)

Once a session's wholesale cost is calculated, it is appended to `amber_cache.csv` keyed by `session_id`. Subsequent runs skip already-processed sessions entirely — no AEMO download, no recalculation. As your session history grows, only new sessions ever trigger network requests.

`amber_cache.csv` is listed in `.gitignore` and never committed.

To force a recalculation, delete `amber_cache.csv` (or individual rows from it).

---

## CLI Usage

Add `--region` to any `correlate.py` invocation to enable the Amber estimate:

```bash
python correlate.py --sessions charge_sessions.csv \
  --region VIC \
  --import-rate 0.437 \
  --amber-network-rate 0.09 \
  --amber-subscription 18.00 \
  --utc-offset 10
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--region` | *(off)* | NEM region: `QLD`, `NSW`, `VIC`, `SA`, `TAS`. Required to enable this feature. |
| `--import-rate` | 0.30 | Your current fixed rate $/kWh — used for the "Fixed $" column comparison. |
| `--amber-network-rate` | 0.09 | Network/distribution component $/kWh (check your current bill). |
| `--amber-subscription` | 18.00 | Amber monthly subscription $ — shown in the net verdict only. |
| `--utc-offset` | 10 | Your UTC offset: 10 = AEST, 11 = AEDT. |
| `--amber-cache` | `amber_cache.csv` | Path to the incremental results cache. |
| `--aemo-cache-dir` | `aemo_cache` | Directory for cached AEMO monthly price files. |

---

## Reading the Output

```
  AMBER WHOLESALE ESTIMATE  —  region: VIC  network: 9.0c/kWh  subscription: $18/mo
  AEMO 5-min dispatch prices. Amber bills 30-min trading price — estimate only.
────────────────────────────────────────────────────────────────────────────────
ID      Date           kWh  Avg c/kWh     Min     Max  Neg min  Fixed $  Amber $  Saving $
────────────────────────────────────────────────────────────────────────────────
S0001   2026-05-03    1.65      -0.1c   -0.3c    0.9c     115m $   0.72 $   0.15 $    0.57 ★
────────────────────────────────────────────────────────────────────────────────
```

| Column | Meaning |
|--------|---------|
| **Avg c/kWh** | Average spot price across all 5-min intervals during the session |
| **Min / Max** | Lowest and highest spot price seen during the session |
| **Neg min** | Minutes where the spot price was negative (Amber credits you) |
| **Fixed $** | What you actually paid at your fixed tariff (`kWh × import-rate`) |
| **Amber $** | Estimated cost on Amber (`kWh × avg_spot/1000 + kWh × network_rate`) |
| **Saving $** | `Fixed $ − Amber $` — positive means Amber would have been cheaper |
| **★** | Session contained at least one negative-price interval |

The footer verdict excludes the subscription charge:

```
  Amber $18/mo service charge excluded — covers the whole house, not just the car.
  Vs fixed rate (excl. service charge): Amber would have been CHEAPER by $0.57
```

The subscription covers your entire household electricity use, so attributing it to EV charging alone would be misleading. The verdict reflects only the energy + network cost difference for the sessions shown.

---

## Accuracy and Limitations

- **Estimate only.** Amber bills at the **30-minute trading price** (average of 6 dispatch intervals), not the raw 5-minute dispatch price. Averaging over a full charging session makes the difference small in practice.
- **No loss factors.** Marginal loss factors (MLFs) apply at the connection point level and are not applied here.
- **Network rate is approximate.** Your actual network charge depends on your DNSP, tariff class, and time of use. Check your bill for the correct distribution rate.
- **Subscription not per-session.** The $18/mo subscription is a fixed cost shown only in the summary verdict, not in per-session figures.
- **1–2 day lag for current month.** DispatchIS daily files appear on NEMWeb roughly 1–2 days after the event. Very recent sessions will be skipped and will price automatically on the next run.

---

## Module: `aemo.py`

The AEMO fetching logic lives in a standalone module so it can be reused by other tools (e.g. a bill comparison script).

### Public API

```python
from aemo import spot_prices_for_window
from pathlib import Path
from datetime import datetime

prices = spot_prices_for_window(
    start_nem=datetime(2026, 5, 3, 12, 0),   # NEM time (UTC+10)
    end_nem=datetime(2026, 5, 3, 13, 30),
    region="VIC",
    cache_dir=Path("aemo_cache"),
)

# prices = [{"dt": datetime(...), "rrp": float}, ...]
# rrp is $/MWh — divide by 10 for c/kWh
avg_c_kwh = sum(p["rrp"] for p in prices) / len(prices) / 10
```

`spot_prices_for_window` handles all caching, source selection (MMSDM vs DispatchIS), and ZIP parsing transparently. Pass it NEM-time datetimes and a cache directory and it returns a list of `{dt, rrp}` dicts covering the window.
