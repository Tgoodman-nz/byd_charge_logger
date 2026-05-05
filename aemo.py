"""
aemo.py — AEMO NEM spot price fetcher
======================================
Fetches 5-minute dispatch prices from NEMWeb and returns them for any
time window and NEM region. Suitable for wholesale cost estimation or
comparison against fixed-rate tariffs.

Data sources (both use the MMSDM I/D row CSV format):
  Complete months:  MMSDM DISPATCHPRICE monthly archive
                    https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/
  Current month:    DispatchIS daily ZIPs (ZIP-of-ZIPs, one per 5-min interval)
                    https://www.nemweb.com.au/Reports/ARCHIVE/DispatchIS_Reports/

AEMO migrated to NEMWeb on 30 April 2026 — old aemo.com.au URLs no longer work.

Public API
----------
  spot_prices_for_window(start_nem, end_nem, region, cache_dir) -> list[dict]

  Each dict has:
    dt  — datetime (NEM time, UTC+10)
    rrp — float, $/MWh (negative values possible)

  Divide rrp by 10 to get cents/kWh.

Caching
-------
  Complete months are cached to disk in cache_dir as
  DISPATCHPRICE_YYYYMM_{region}.csv so each month is only downloaded once.
  The current month is cached in memory per run (re-downloaded each run
  so newly published days are always picked up).
"""

import csv
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

# In-memory cache: (year, month, region) → list[dict]
# Prevents re-downloading the same month for multiple sessions in one run.
_aemo_month_cache: dict = {}


def _parse_mmsdm_csv(raw: str, region: str) -> list[str]:
    """Parse MMSDM CSV text; return list of 'SETTLEMENTDATE,RRP' strings."""
    headers, filtered = None, []
    region_id = f"{region}1"
    for line in raw.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if not parts:
            continue
        if parts[0] == "I":
            headers = parts[4:]
        elif parts[0] == "D" and headers:
            row = dict(zip(headers, parts[4:]))
            if row.get("REGIONID") == region_id and row.get("INTERVENTION", "0") == "0":
                filtered.append(f"{row.get('SETTLEMENTDATE','')},{row.get('RRP','')}")
    return filtered


def _parse_mmsdm_zip(zip_bytes: bytes, region: str) -> list[str] | None:
    """Parse an MMSDM-format ZIP; handles both flat-CSV and ZIP-of-ZIPs formats.

    MMSDM monthly archives contain one large CSV.
    DispatchIS daily archives contain ~288 nested ZIPs (one per 5-min interval),
    each containing a small CSV for that interval.
    """
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            csv_names = [n for n in names if n.upper().endswith(".CSV")]
            zip_names = [n for n in names if n.upper().endswith(".ZIP")]

            if csv_names:
                # Flat format (MMSDM monthly) — one large CSV
                raw = zf.read(csv_names[0]).decode("utf-8", errors="replace")
                return _parse_mmsdm_csv(raw, region)

            if zip_names:
                # Nested format (DispatchIS daily) — one ZIP per 5-min interval
                all_filtered = []
                for inner_name in zip_names:
                    try:
                        inner_bytes = zf.read(inner_name)
                        with zipfile.ZipFile(BytesIO(inner_bytes)) as inner_zf:
                            inner_csv = [n for n in inner_zf.namelist()
                                         if n.upper().endswith(".CSV")]
                            if inner_csv:
                                raw = inner_zf.read(inner_csv[0]).decode(
                                    "utf-8", errors="replace")
                                all_filtered.extend(_parse_mmsdm_csv(raw, region))
                    except Exception:
                        continue
                return all_filtered or None

            print("  Warning: no CSV or ZIP found inside AEMO ZIP")
            return None
    except Exception as e:
        print(f"  Warning: could not read AEMO ZIP — {e}")
        return None


def _read_dispatch_cache(cached: Path) -> list[dict]:
    rows = []
    try:
        with open(cached, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    dt  = datetime.strptime(row["SETTLEMENTDATE"], "%Y/%m/%d %H:%M:%S")
                    rrp = float(row["RRP"])
                    rows.append({"dt": dt, "rrp": rrp})
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"  Warning: could not read AEMO cache — {e}")
    return rows


def _fetch_aemo_month(year: int, month: int, region: str, cache_dir: Path) -> list[dict]:
    """Return AEMO dispatch prices for a month, downloading if not already cached."""
    key = (year, month, region)
    if key in _aemo_month_cache:
        return _aemo_month_cache[key]

    cache_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    is_current_month = (year == today.year and month == today.month)
    cached = cache_dir / f"DISPATCHPRICE_{year:04d}{month:02d}_{region}.csv"

    if not is_current_month and cached.exists():
        rows = _read_dispatch_cache(cached)
        _aemo_month_cache[key] = rows
        return rows

    if not is_current_month:
        # MMSDM monthly archive — only available for completed months
        # %23 = URL-encoded # (required — bare # is treated as fragment)
        zip_name = f"PUBLIC_ARCHIVE%23DISPATCHPRICE%23FILE01%23{year:04d}{month:02d}010000.zip"
        url = (f"https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM"
               f"/{year:04d}/MMSDM_{year:04d}_{month:02d}"
               f"/MMSDM_Historical_Data_SQLLoader/DATA/{zip_name}")
        print(f"  Downloading AEMO {region} prices {year}-{month:02d} …")
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                zip_bytes = r.read()
        except Exception as e:
            print(f"  Warning: could not download AEMO data — {e}")
            _aemo_month_cache[key] = []
            return []
        filtered = _parse_mmsdm_zip(zip_bytes, region)
        if filtered is None:
            _aemo_month_cache[key] = []
            return []
        with open(cached, "w", newline="") as f:
            f.write("SETTLEMENTDATE,RRP\n")
            f.write("\n".join(filtered))
        print(f"  Cached {len(filtered):,} dispatch intervals for {region}")
        rows = _read_dispatch_cache(cached)
        _aemo_month_cache[key] = rows
        return rows

    # Current month — DispatchIS daily ZIPs, published same-day, no disk cache
    # (re-downloaded each run so newly published days are always included)
    print(f"  Downloading DispatchIS daily prices for {region} {year}-{month:02d} …")
    all_filtered = []
    for day in range(1, today.day + 1):
        d = date(year, month, day)
        day_url = (f"https://www.nemweb.com.au/Reports/ARCHIVE/DispatchIS_Reports/"
                   f"PUBLIC_DISPATCHIS_{d.strftime('%Y%m%d')}.zip")
        try:
            with urllib.request.urlopen(day_url, timeout=60) as r:
                day_zip = r.read()
            day_data = _parse_mmsdm_zip(day_zip, region)
            if day_data:
                all_filtered.extend(day_data)
        except Exception as e:
            if "404" not in str(e):
                print(f"    Warning: could not download {d} — {e}")

    if not all_filtered:
        print(f"  Warning: no DispatchIS data retrieved for {year}-{month:02d}")
        _aemo_month_cache[key] = []
        return []

    print(f"  Got {len(all_filtered):,} dispatch intervals for {region} {year}-{month:02d}")
    rows = []
    for line in all_filtered:
        try:
            parts = line.split(",", 1)
            dt  = datetime.strptime(parts[0], "%Y/%m/%d %H:%M:%S")
            rrp = float(parts[1])
            rows.append({"dt": dt, "rrp": rrp})
        except (ValueError, IndexError):
            continue
    _aemo_month_cache[key] = rows
    return rows


def spot_prices_for_window(start_nem: datetime, end_nem: datetime,
                            region: str, cache_dir: Path) -> list[dict]:
    """Return all AEMO dispatch price intervals overlapping a session window.

    Args:
        start_nem: session start in NEM time (UTC+10)
        end_nem:   session end in NEM time (UTC+10)
        region:    NEM region code: QLD, NSW, VIC, SA, or TAS
        cache_dir: directory for on-disk month caches

    Returns:
        List of {"dt": datetime, "rrp": float ($/MWh)} dicts, sorted by time.
        Empty list if no data available.
    """
    months = set()
    cur = start_nem.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_buf = end_nem + timedelta(minutes=35)
    while cur <= end_buf:
        months.add((cur.year, cur.month))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    prices = []
    for y, m in sorted(months):
        prices.extend(_fetch_aemo_month(y, m, region, cache_dir))

    # SETTLEMENTDATE = end of 5-min interval; 35-min buffer ensures the first
    # and last intervals are captured regardless of session boundary alignment
    return [p for p in prices
            if start_nem - timedelta(minutes=35) < p["dt"] <= end_nem + timedelta(minutes=35)]
