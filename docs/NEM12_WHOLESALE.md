# NEM12 Wholesale Cost Comparison

`nem12_wholesale.py` reads a NEM12 interval meter file from your retailer and answers the question: **would I have paid less on a wholesale electricity plan (e.g. Amber) over the past 1–2 years?**

It fetches actual AEMO spot prices for your full data period, calculates what grid import would have cost at spot + network rates, adds supply charge and subscription, and compares against your actual bills.

---

## What You Need

| File | Where to get it |
|------|----------------|
| NEM12 CSV | Your retailer's website — look for "interval data" or "meter data download". Energy Australia: My Account → Usage → Download interval data. Request the maximum date range available (typically 2 years). |
| `bills.csv` | Create from your quarterly bills — see [Bills CSV](#bills-csv) below |
| `aemo_cache/` | Created automatically on first run |

---

## Quick Start

```bash
python nem12_wholesale.py nem12_file.csv --region VIC --fixed-rate 0.437 --bill-csv bills.csv
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

Per-month energy comparison showing import kWh, fixed cost, wholesale cost, EA feed-in credit (at your fixed rate), Amber feed-in (at spot), net saving, and consumption-weighted average spot price.

**Note:** The Saving column is energy-only — it does not include supply charge, subscription, or GST. Use the Actual Bill Comparison for the real-world answer.

June 2025 at 45.1c average shows what a cold-snap winter period looks like — high heating demand drove spot prices well above the fixed rate.

### Spot Price Distribution

Shows what percentage of your consumption fell in each spot price band. Useful for understanding your exposure profile:

- **Negative** — grid was oversupplied, Amber would credit you (or charge you to export)
- **0–15c** — typical daytime solar-oversupply range, significantly cheaper than fixed
- **30c+** — peak demand periods, potentially more expensive than fixed

### Actual Bill Comparison

Compares what you actually paid EA (after discount and govt relief) against what Amber would have cost for the same period:

```
  Period                  Days   EA paid    Relief  Amber est     Saving
                                (net rel)          (net rel)
  ────────────────────────────────────────────────────────────────────────────────
  2025-09-22→2025-12-22     92   $ 724.17   $75.00  $  508.96   +$215.21
  2025-12-23→2026-03-22     90  $ 1102.53        -  $  721.39   +$381.14
```

**Amber estimate includes:** wholesale import cost − Amber feed-in credit + supply charge + subscription, all × 1.1 GST, minus govt relief.

**EA paid:** actual amount paid after pay-on-time discount and govt relief.

Both are net of govt relief so the Saving column reflects a clean energy cost comparison.

---

## Key Caveats

**Solar feed-in:** The feed-in comparison highlights a key risk for solar households on wholesale plans. Negative spot prices cluster during sunny midday hours — exactly when solar export is highest. On Amber, those negative-price intervals cost you money; on a fixed plan you always receive the guaranteed feed-in rate. The more solar you have, the more this matters.

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
