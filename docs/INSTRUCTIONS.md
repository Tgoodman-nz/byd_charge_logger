# Home Energy Analysis System — Complete Guide

A set of Python scripts to track your BYD Seal EV charging, measure solar
vs grid usage, and analyse whether a battery, solar upgrade, or gas-to-
electric switch makes financial sense.

---

## What's in this package

| File | Purpose |
|---|---|
| `byd_logger.py` | Runs on Oracle Cloud — polls BYD API, logs charge sessions, serves CSV via URL |
| `correlate.py` | Runs on your Mac/PC — matches charge sessions against PowerPal data, calculates solar vs grid cost per session |
| `analyse.py` | Runs on your Mac/PC — full investment analysis: battery, solar upgrade, gas-to-electric |
| `byd_logger.service` | Linux systemd service file — keeps the logger running after reboots |
| `requirements.txt` | Python dependencies |
| `README.md` | Full deployment guide for the Oracle VM |

### Household electricity tools (not BYD-specific)

| File | Purpose |
|---|---|
| `nem12_wholesale.py` | Compares 1–2 years of NEM12 interval meter data against AEMO wholesale spot prices — answers "would I save on Amber?" See [NEM12_WHOLESALE.md](NEM12_WHOLESALE.md) |
| `aemo.py` | Shared module — fetches and caches AEMO 5-min dispatch prices from NEMWeb. Used by both `correlate.py` and `nem12_wholesale.py` |
| `bills.csv` | Your quarterly bill history — period dates, actual amount paid, govt relief. Used by `nem12_wholesale.py` |

---

## System Overview

```
BYD Car  -->  BYD Cloud (AU)  -->  Oracle VM (byd_logger.py)
                                        |
                                        |  http://YOUR_IP:8080/sessions.csv?token=...
                                        v
                                 Your Mac/PC (correlate.py / analyse.py)
                                        ^
                                 PowerPal CSV export (manual, monthly)
                                 EnergyAustralia CSV export (manual, monthly)
                                 Gas bill CSV (manual, per bill)
```

---

## ⚠️ Critical Setup Notes

Read these before starting — they will save you hours:

1. **Use Ubuntu 22.04** when creating the Oracle VM. Do not accept the default Oracle Linux image — it has memory issues installing Python 3.11.

2. **Australian BYD accounts use a different API endpoint** than the pyBYD default. You must add `BYD_BASE_URL=https://dilinkappoversea-au.byd.auto` to your .env file.

3. **pyBYD has an imeiMD5 bug** that causes login to fail with "Network busy" (code 1033). You must patch the library after installing it. See README Step 7.

4. **Oracle Cloud has two firewall layers** — you must open port 8080 in BOTH the Oracle Security List AND Ubuntu's iptables. The Security List alone is not sufficient.

5. **Use a secondary BYD account** for the logger (not your main account). Create a new account, share the vehicle to it, and use those credentials. Use a simple email address without + aliases.

---

## Part 1 — BYD Charge Logger (Oracle Cloud VM)

See **README.md** for the complete step-by-step deployment guide including all
fixes for known issues. Summary of steps:

1. Create secondary BYD account and share vehicle to it
2. Create Oracle Cloud Free Tier VM with **Ubuntu 22.04**
3. Open port 8080 in Oracle Security List
4. SSH in and install Python 3.11
5. Install pybyd and aiohttp in a virtualenv
6. **Apply the imeiMD5 fix to pyBYD** (required for all accounts)
7. Upload byd_logger.py
8. Create .env with credentials including **BYD_BASE_URL for Australia**
9. Open port 8080 in Ubuntu iptables (separate from Oracle Security List)
10. Test, then install as a systemd service

### Accessing Your Data

Once running, bookmark this URL:
```
http://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN
```

Every visit downloads the complete session history. Open directly in Excel
via Data > From Web > paste the URL.

Health check (no token needed):
```
http://YOUR_VM_IP:8080/health
```

View live logs on the VM:
```bash
sudo journalctl -u byd_logger -f
```

---

## Part 2 — EV Session Correlation (Your Mac/PC)

### What it does
Takes your BYD session log and PowerPal export, matches them minute-by-minute,
and tells you for each charge session:
- How many kWh came from solar vs grid
- What it cost
- How much you saved vs charging entirely from the grid

### Setup (one time)
```bash
pip3 install pybyd aiohttp
```

### Data you need
1. BYD sessions — fetched automatically from your Oracle VM URL
2. PowerPal export — open the PowerPal app > Export > CSV

### Run it
```bash
python3 correlate.py \
  --powerpal powerpal_data.csv \
  --url "http://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" \
  --import-rate 0.30 \
  --feedin-rate 0.06
```

---

## Part 3 — Investment Analysis (Your Mac/PC)

### What it does
Uses your electricity and gas data to answer:
1. Should I add a battery?
2. Should I upgrade solar panels?
3. Should I switch gas heating to electric?

Also models payback under different rate inflation scenarios — flat rates,
moderate inflation, and high gas price increase scenarios.

### Data you need

Electricity (monthly):
- Log into EnergyAustralia My Account
- Go to Usage > Download CSV
- Save each month into a folder called `elec_data/`

Gas (per bill):
- Save each gas bill CSV into a folder called `gas_data/`

PowerPal:
- Same export as used for EV correlation

### Run it
```bash
python3 analyse.py \
  --elec ./elec_data \
  --powerpal powerpal.csv \
  --gas ./gas_data \
  --import-rate 0.30 \
  --feedin-rate 0.06 \
  --gas-rate 0.025
```

Add your BYD session URL for EV analysis too:
```bash
python3 analyse.py \
  --elec ./elec_data \
  --powerpal powerpal.csv \
  --gas ./gas_data \
  --byd "http://YOUR_VM_IP:8080/sessions.csv?token=TOKEN" \
  --import-rate 0.30 \
  --feedin-rate 0.06 \
  --gas-rate 0.025
```

---

## Using the Analysis Tools

### Prerequisites

Install dependencies on your Mac or PC (one time):
```bash
pip3 install pybyd aiohttp
```

---

### correlate.py — EV Session Analysis

This is your primary day-to-day tool. Run it whenever you want to see how
your car is performing and what your charging is costing.

**Quickest run (no PowerPal needed):**
```bash
python3 correlate.py \
  --url "http://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" \
  --import-rate 0.30 \
  --feedin-rate 0.06
```

This fetches sessions from the Oracle VM and prints all 9 EV insight questions.

**Full run with PowerPal (adds solar vs grid breakdown per session):**
```bash
python3 correlate.py \
  --url "http://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" \
  --powerpal powerpal_data.csv \
  --import-rate 0.30 \
  --feedin-rate 0.06
```

**Reading the output:**

- **Session table** — only appears with --powerpal. Solar vs grid per session.
  Sessions with low PowerPal data coverage are flagged.

- **Power Usage (Q1)** — accumulates over time. After 6-12 months you will
  have a reliable annual kWh figure for your car.

- **Efficiency (Q2)** — 15.3 kWh/100km is excellent for the BYD Seal.
  Watch for this rising in winter (cold battery, heater on) vs summer.

- **Battery Health (Q3)** — range at 100% SOC is your degradation indicator.
  Check every 6 months. A drop of more than 20km over a year warrants attention.

- **Solar vs Grid (Q4)** — requires --powerpal. Will improve as you shift
  charging to daytime hours to capture solar.

- **Cost per km (Q5)** — should be 3-5c/km depending on solar coverage.
  Compare to your old petrol car (typically 15-25c/km).

- **Seasonal efficiency (Q6)** — winter will be 1-2 kWh/100km higher than
  summer. Normal — cold batteries are less efficient and heater draws power.

- **Charging behaviour (Q7)** — day vs night split shows how much solar you
  are naturally capturing without any deliberate effort.

- **Savings vs petrol (Q8)** — update PETROL_RATE_PER_L at the top of
  correlate.py with your current local unleaded price.

- **Real-world range (Q9)** — your actual average km between charges.
  Much more useful than the 510km rated range for planning purposes.

**Output file:**
correlation_report.csv is saved in the current directory and contains all
session data with solar/grid breakdown. Open in Excel for further analysis.

---

### analyse.py — Home Energy Investment Analysis

Run this monthly with your latest electricity and gas data.

**Minimum run (electricity only):**
```bash
python3 analyse.py \
  --elec ./elec_data \
  --import-rate 0.30 \
  --feedin-rate 0.06
```

**Full run:**
```bash
python3 analyse.py \
  --elec ./elec_data \
  --powerpal powerpal.csv \
  --gas ./gas_data \
  --byd "http://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" \
  --import-rate 0.30 \
  --feedin-rate 0.06 \
  --gas-rate 0.025
```

**What it covers:**

- Current energy baseline — annual electricity and gas costs, seasonal
  breakdown, solar export vs self-consumption rates

- Electrification payback — payback period on switching gas to electric
  under three rate inflation scenarios (flat, moderate, high gas inflation)

- HVAC sizing — heat load calculation based on house specs, comparison
  to quoted system capacity

- Battery analysis — whether a home battery makes financial sense based
  on your actual export volumes

- Solar upgrade — whether adding panels helps or whether battery and
  electrification should come first

**Getting your electricity data:**
1. Log into EnergyAustralia My Account at energy.com.au
2. Go to Usage and download CSV for each month
3. Save all files into a folder called elec_data/
4. File names do not matter — the script reads all CSVs in the folder

**Getting your gas data:**
1. Log into your gas retailer portal
2. Download each bill as CSV
3. Save into a folder called gas_data/

**Update your tariff rates:**
Check your electricity bill for exact rates. Pass them as flags:
```bash
--import-rate 0.285 --feedin-rate 0.05
```

---

## Monthly Routine

Once everything is set up:

1. Download PowerPal export (app > Export > CSV)
2. Download EnergyAustralia monthly CSV (My Account > Usage > Download)
3. Run `analyse.py` for investment analysis
4. Run `correlate.py` for EV session breakdown
5. BYD sessions are fetched automatically from the URL — no manual step needed

The whole thing takes about 5 minutes once a month.

---

## Tariff Assumptions

Update these with your actual bill figures:

| Rate | Default | Flag |
|---|---|---|
| Grid import | 30c/kWh | --import-rate |
| Solar feed-in | 6c/kWh | --feedin-rate |
| Gas usage | 2.5c/MJ | --gas-rate |

---

## Caveats and Known Limitations

- **pyBYD is alpha software** — reverse-engineered BYD API, not official.
  May break if BYD changes their backend. Check https://github.com/jkaberg/pyBYD for updates.
  Australian app connectivity only launched December 2025 so the AU endpoint is new.

- **imeiMD5 fix may be resolved** in a future pyBYD release. If you reinstall
  or upgrade pybyd you will need to reapply the patch to config.py.

- **kWh charged is estimated** from session duration x 2.3 kW (portable EVSE rate).
  The SOC delta is a useful cross-check. A Shelly EM clamp on the circuit would
  give exact figures but is not required.

- **Solar generation is estimated** in the investment analysis — no Sungrow
  inverter data is used. Figures marked 'est.' have a +/-15% margin.

- **Time zones** — BYD logger records in both UTC and Melbourne local time.
  PowerPal records in local time. UTC_OFFSET_HOURS should be 10 (AEST) or
  11 (AEDT). Victoria observes daylight saving October to April.

- **EnergyAustralia data** — daily resolution only. Hourly data is available
  in the portal but must be downloaded day by day. The daily data is sufficient
  for investment analysis.

---

## Key Findings (Sample Analysis, Melbourne Victoria, May 2026)

Based on two years of actual electricity data and gas bills:

| Question | Answer |
|---|---|
| Add a battery? | Not yet — 17-35 year payback. Revisit when feed-in reaches 0c. |
| Upgrade solar? | Not yet — electrify first, battery second, solar upgrade last. |
| Switch gas to electric? | Yes — $1,600-1,750/yr saving. 7-9 year payback on Goodbye Gas quote. |

Rate inflation scenarios show payback improves to 7.4 years under moderate
inflation (electricity +5%/yr, gas +8%/yr) — historically realistic for Victoria.

Gas prices are rising faster than electricity as network costs spread across
fewer customers. The feed-in tariff is declining toward zero. Both trends
strengthen the electrification case over time.

---

## Support and References

- pyBYD library: https://github.com/jkaberg/pyBYD
- HA BYD integration: https://github.com/jkaberg/hass-byd-vehicle
- Oracle Cloud free tier: https://cloud.oracle.com
- PowerPal: https://www.powerpal.app
- Goodbye Gas: https://www.goodbyegas.com.au
