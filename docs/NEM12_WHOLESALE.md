# NEM12 Wholesale Cost Comparison

`nem12_wholesale.py` reads a NEM12 interval meter file from your retailer and answers the question: **would I have paid less on a wholesale or time-of-use (TOU) plan over the past 1–2 years?**

It fetches actual AEMO spot prices for your full data period and supports three-way comparison:

- **Fixed** — your current flat tariff (e.g. Energy Australia Flexi Saver)
- **TOU** — a time-of-use plan with a cheap off-peak window (e.g. OVO with 4.5c/kWh midnight–6am)
- **Wholesale** — spot pass-through plan (e.g. Amber) at AEMO prices + network charge

---

## What You Need

| File | Where to get it |
|------|----------------|
| NEM12 CSV | Your retailer's website — look for "interval data" or "meter data download". Energy Australia: My Account → Usage → Download interval data. Request the maximum date range available (typically 2 years). |
| `bills.csv` | Create from your quarterly bills — see [Bills CSV](#bills-csv) below |
| `aemo_cache/` | Created automatically on first run |

---

## Quick Start

**Wholesale comparison only:**
```bash
py nem12_wholesale.py nem12_file.csv --region VIC --fixed-rate 0.437 --bill-csv bills.csv
```

**Three-way comparison including OVO TOU:**
```bash
py nem12_wholesale.py nem12_file.csv --region VIC --bill-csv bills.csv \
    --tou-peak-rate 0.29381 --tou-offpeak-rate 0.045 \
    --tou-supply-rate 0.946 --tou-feedin-rate 0.01
```

AEMO price data downloads automatically and is cached locally — subsequent runs are fast.

---

## Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `nem12_file` | *(required)* | NEM12 CSV from your retailer |
| `--region` | *(required)* | NEM region: `QLD`, `NSW`, `VIC`, `SA`, `TAS` |
| `--fixed-rate` | 0.30 | Your current flat tariff $/kWh — check your bill |
| `--network-rate` | 0.09 | Network/distribution charge $/kWh added to spot on Amber |
| `--feedin-rate` | 0.015 | Your EA fixed feed-in tariff $/kWh — for comparison column |
| `--subscription` | 18.00 | Wholesale retailer monthly fee $ (e.g. Amber) |
| `--supply-rate` | 1.68 | Daily supply charge $/day excl. GST — check your bill |
| `--bill-csv` | *(optional)* | Path to `bills.csv` for actual bill comparison |
| `--register` | E1 | NEM12 register to analyse: `E1` = grid import, `B1` = solar export |
| `--aemo-cache-dir` | `aemo_cache` | Directory for cached AEMO price files |

**TOU plan arguments** (all optional — TOU comparison is enabled by providing `--tou-peak-rate`):

| Argument | Default | Description |
|----------|---------|-------------|
| `--tou-peak-rate` | *(disabled)* | TOU peak rate $/kWh — enables TOU comparison (e.g. `0.29381` for OVO) |
| `--tou-offpeak-rate` | 0.045 | TOU off-peak rate $/kWh (e.g. OVO EV rate) |
| `--tou-offpeak-start` | 0 | Off-peak window start hour 0–23 (default `0` = midnight) |
| `--tou-offpeak-end` | 6 | Off-peak window end hour 0–23 (default `6` = 6am) |
| `--tou-supply-rate` | same as `--supply-rate` | TOU plan daily supply $/day excl. GST |
| `--tou-feedin-rate` | 0.01 | TOU plan feed-in tariff $/kWh |

---

## Bills CSV

Create `bills.csv` with one row per quarterly bill:

```csv
period_start,period_end,total_cost_incl_gst,actual_paid_incl_gst,govt_relief
2025-09-22,2025-12-22,943.31,724.17,75.00
2025-12-23,2026-03-22,1424.47,1102.53,0.00
```

| Column | Notes |
|--------|-------|
| `period_start` / `period_end` | Bill period dates in YYYY-MM-DD format |
| `total_cost_incl_gst` | Total charges on the bill before any discount, incl. GST |
| `actual_paid_incl_gst` | What you actually paid — after pay-on-time discount and any credits |
| `govt_relief` | Australian Government Energy Bill Relief applied this period (0 if none) |

**Govt relief:** If the relief applied to your bill, it would have applied equally on Amber. Both EA paid and Amber estimate are shown net of relief so the saving column reflects a true energy cost comparison.

---

## Multi-Register NEM12 Files

Energy Australia (and most retailers) include both registers in a single file:

- **E1** — grid consumption / import (default, used for this analysis)
- **B1** — solar export / feed-in

The script automatically reads both. E1 is used for import cost calculations; B1 is used for the feed-in comparison.

---

## Output Sections

### Cost Comparison

High-level summary over the full data period:

```
  Fixed rate (43.7c/kWh):              $ 9157.60
  Wholesale (spot + 9c network):
    Import cost:                   $ 4271.02
    Feed-in credit (B1 at spot): $   -39.81
    Subscription (25 × $18/mo):   $   450.00
    Total:                         $ 4760.82
  ──────────────────────────────────────────────────────
  Wholesale CHEAPER by $4396.78 over 25 months (incl. sub + feed-in)
```

- **Fixed rate** = total import kWh × your fixed tariff. Does not include feed-in credits.
- **Import cost** = what you'd pay Amber for grid import at spot + network rate.
- **Feed-in credit** = what Amber would pay/charge for solar export at spot price. Can be **negative** — during negative spot price events Amber charges you to export, whereas your fixed retailer always pays a guaranteed rate.
- **Subscription** = monthly fee × number of months in the data period.

### Monthly Breakdown

Per-month energy comparison showing import kWh, fixed cost, TOU cost (if enabled), wholesale cost, saving vs fixed for each plan, and consumption-weighted average spot price.

**Note:** The save columns are energy-only — they do not include supply charge, subscription, or GST. Use the Actual Bill Comparison for the real-world answer.

June 2025 at 45.1c average shows what a cold-snap winter period looks like — high heating demand drove spot prices well above the fixed rate. In that month Amber cost $192 *more* than the fixed rate while OVO remained cheaper.

**TOU save column caveat:** TOU savings are calculated against your *historical* consumption pattern. If you shift EV charging or other large loads into the off-peak window (midnight–6am), actual TOU savings will be materially higher than shown.

### Spot Price Distribution

Shows what percentage of your consumption fell in each spot price band. Useful for understanding your exposure profile:

- **Negative** — grid was oversupplied, Amber would credit you (or charge you to export)
- **0–15c** — typical daytime solar-oversupply range, significantly cheaper than fixed
- **30c+** — peak demand periods, potentially more expensive than fixed

### Actual Bill Comparison

Compares what you actually paid EA (after discount and govt relief) against what OVO and/or Amber would have cost for the same period:

```
  Period                  Days    EA paid   Relief    OVO est   OVO-save  Amber est   Amb-save
                                (net rel)           (net rel)             (net rel)
  ──────────────────────────────────────────────────────────────────────────────────────────────
  2025-09-22→2025-12-22     92  $  724.17   $75.00  $  554.82   +$169.35  $  508.96   +$215.21
  2025-12-23→2026-03-22     90  $ 1102.53        -  $  876.92   +$225.61  $  721.39   +$381.14
```

**OVO estimate includes:** TOU energy cost (peak/off-peak split from NEM12 intervals) − feed-in credit + TOU supply charge, all × 1.1 GST, minus govt relief.

**Amber estimate includes:** wholesale import cost − Amber feed-in credit + supply charge + subscription, all × 1.1 GST, minus govt relief.

**EA paid:** actual amount paid after pay-on-time discount and govt relief.

All estimates are net of govt relief so the saving columns reflect a clean energy cost comparison.

---

## Key Caveats

**TOU off-peak savings are conservative:** The TOU comparison applies off-peak rates only to consumption that *already* falls in the midnight–6am window in your historical NEM12 data. If you shift EV charging, hot water, or other large loads into that window after switching, real-world savings will be significantly higher. OVO's 4.5c EV rate vs ~29c peak is the primary reason to switch — that benefit won't appear in the historical comparison.

**Wholesale volatility:** Amber saves money in most months when wholesale prices are low (6–18c avg), but a single high-price event can wipe out months of savings. June 2025 (45c avg spot in VIC) cost $192 more than the fixed rate in one month alone. OVO's fixed TOU rates eliminate this risk.

**Solar feed-in:** Negative spot prices cluster during sunny midday hours — exactly when solar export is highest. On Amber, those intervals cost you money; on a fixed plan you always receive the guaranteed feed-in rate. OVO's fixed feed-in rate (typically 1c/kWh) is low but at least predictable. The more solar you export, the more Amber's negative-price risk matters.

**Pay-on-time discount:** A 25% discount (as offered by Energy Australia) significantly reduces your effective fixed rate. This makes wholesale plans harder to beat than the headline tariff suggests.

**Battery:** The economic case for wholesale plans is much stronger with a home battery — you can charge overnight at near-zero or negative prices, discharge during peak price periods, and stop exporting when spot is negative.

**Missing AEMO data:** If AEMO data is unavailable for some months (rare), affected intervals fall back to the fixed rate for the Amber estimate, making Amber look worse than it actually would have been. The output warns you if this occurs.

**Supply rate changes:** `--supply-rate` accepts a single rate. If your retailer changed the supply charge mid-period, use the most common rate across your data — the error is small relative to energy costs.

---

## Data Sources

AEMO publishes historical 5-minute dispatch prices on [NEMWeb](https://www.nemweb.com.au/). Two sources are used:

| Situation | Source |
|-----------|--------|
| Completed months (> ~6 weeks ago) | MMSDM monthly archive |
| Recently completed months | DispatchIS daily ZIPs (auto-fallback when MMSDM not yet published) |
| Current month | DispatchIS daily ZIPs (re-downloaded each run for latest data) |

Completed months are cached to `aemo_cache/` after the first download. See [AEMO_WHOLESALE.md](AEMO_WHOLESALE.md) for full details on data sources and caching.

---

## NEM Time

NEM12 data is in Eastern Standard Time (EST = UTC+10, no daylight saving). AEMO prices are also in NEM time. No timezone conversion is needed — the timestamps align directly.

When comparing NEM12 data against your bill (which uses local wall-clock time), you would need to account for DST. This script does not compare against bill line items, only against AEMO prices, so no adjustment is required.
