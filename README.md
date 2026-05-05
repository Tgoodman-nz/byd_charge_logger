# BYD Charge Session Logger — Complete Setup Guide

## What Questions Does This Answer?

If you have a BYD EV and a home solar system, this tool helps you answer:

1. **How much power does my car use?** — kWh per session, tracked over time
2. **How efficient is my car per km driven?** — kWh/100km per session and lifetime
3. **Does my battery degrade over time?** — track range at full charge and efficiency trends over months and years
4. **How much of my charging comes from solar?** — session-by-session solar vs grid split (via PowerPal API or CSV export)
5. **What does it actually cost me per km?** — true cost accounting for solar opportunity cost vs grid rate
6. **How does efficiency vary by season?** — winter cold battery vs summer, short trips vs long
7. **What are my charging behaviour patterns?** — day vs night charging, average SOC at plug-in
8. **How much have I saved vs petrol?** — compare electricity cost to equivalent petrol cost per km
9. **What is my real-world range?** — average km driven per charge cycle in your actual driving patterns
10. **Should I invest in solar, a battery, or electrify gas appliances?** — payback analysis using your actual data
11. **Would a wholesale electricity plan cost less?** — compare your fixed rate against AEMO spot prices session-by-session to see if switching to a pass-through retailer like Amber would save money on EV charging

---

## How It Works

Polls the BYD cloud API every 60 seconds. Detects when your BYD Seal starts
and stops charging, and writes a CSV log you can correlate against PowerPal
exports to calculate solar vs grid charging costs.

Session state is persisted to disk so restarts mid-session do not lose data.

---

## ⚠️ Critical Lessons Learned (Read First)

These are issues discovered during real deployment. Save yourself hours:

1. **Use Ubuntu 22.04 — not Oracle Linux.** Oracle Linux has memory issues
   installing Python 3.11 on a 1GB RAM VM and uses a different package manager.
   Always select Ubuntu 22.04 when creating the Oracle instance.

2. **Australian BYD accounts need the AU endpoint.** The default pyBYD endpoint
   is EU. Australian accounts must use https://dilinkappoversea-au.byd.auto
   Add BYD_BASE_URL=https://dilinkappoversea-au.byd.auto to your .env.

3. **pyBYD has a bug with imeiMD5.** The library hardcodes an all-zeros imeiMD5
   which causes a "Network busy" error (code 1033) on all accounts. The fix is
   to calculate MD5(username).toUpperCase() and patch it into the library.
   See Step 7 below.

4. **Oracle Cloud has TWO firewall layers.** The Oracle Security List AND Ubuntu
   iptables both need port 8080 opened. The Security List alone is not enough.

5. **Use source not export when loading .env.** Special characters in passwords
   get mangled by export $(cat .env | xargs).

6. **The Oracle instance username is ubuntu not opc.** opc is for Oracle Linux.

7. **BYD charging detection quirks.** The chargingState field is always -1 even
   when actively charging. The correct field to use is chargeState==1 for active
   charging. chargeState==15 means connected but not charging. The gl field
   contains actual charging power in watts and is the most reliable signal.

8. **MQTT connections drop silently.** The BYD connection can hang without
   raising an exception. A 30 second timeout on each poll request plus an
   outer reconnection loop handles this automatically.

---

## Output

charge_sessions.csv — one row per completed charging session:

| Column | Example | Notes |
|---|---|---|
| session_id | S0001 | Auto-incremented |
| date_local | 2026-01-15 | Local date |
| start_time_local | 10:32:00 | Local time |
| end_time_local | 14:18:00 | Local time |
| start_time_utc | 00:32:00 | UTC |
| end_time_utc | 04:18:00 | UTC |
| duration_minutes | 226.0 | |
| soc_start_pct | 42 | Battery % when plugged in |
| soc_end_pct | 91 | Battery % when unplugged |
| soc_delta_pct | 49 | |
| kwh_charged_estimated | 8.64 | duration x 2.3 kW portable EVSE rate |
| kwh_charged_actual | 8.21 | calculated from gl power readings |
| avg_charge_power_w | 1566 | average watts during session |
| odo_start_km | 25963 | Odometer at session start |
| odo_end_km | 25963 | Odometer at session end |
| km_driven_since_last_charge | 87.3 | km driven between charges |
| range_km | 510 | Estimated range at session end |
| efficiency_kwh_per_100km | 15.1 | Session efficiency |
| lifetime_efficiency_kwh_per_100km | 15.3 | BYD lifetime average |
| location | H | H = home charge, A = away charge. Auto-detected from GPS at session start. Blank for sessions logged before GPS was added (treated as H). |
| notes | | Free text, edit manually |

session_state.json — written during an active session, deleted when complete.
If the service restarts mid-session, this file is used to resume accurately.

---

## Step 1 — BYD Account Setup

1. Open the BYD app on your phone
2. Create a second dedicated BYD account using a different email address
   - Use a simple email with no + aliases (e.g. a new Gmail account)
   - The +alias trick can cause authentication issues
3. In the BYD app: My Car > Vehicle Management > Share Vehicle
4. Share your Seal to the new account
5. Log into the BYD app with the secondary account and accept all T&Cs
6. Use this secondary account credentials for the logger

---

## Step 2 — Create an Oracle Cloud Free Tier VM

1. Go to https://cloud.oracle.com and click Start for free
2. Sign up — credit card for ID verification only, you will not be charged
3. Go to Compute > Instances > Create Instance
4. Click Change Image and select Canonical Ubuntu 22.04 (not the default Oracle Linux)
5. Shape: VM.Standard.E2.1.Micro (Always Free eligible)
6. Under Add SSH keys: paste your public key (see Step 3)
7. Under Networking: ensure Assign a public IPv4 address is set to Yes
8. Click Create — note the Public IP address once running

---

## Step 3 — Create SSH Keys

On Mac (Terminal):
    ssh-keygen -t ed25519 -C "oracle-byd-logger"
    cat ~/.ssh/id_ed25519.pub

On Windows (PowerShell):
    ssh-keygen -t ed25519 -C "oracle-byd-logger"
    cat $env:USERPROFILE\.ssh\id_ed25519.pub

Copy the output starting with ssh-ed25519 and paste into Oracle during instance creation.

---

## Step 4 — Open Port 8080 (Two Places Required)

Oracle Security List (firewall layer 1):
1. Networking > Virtual Cloud Networks > your VCN
2. Security Lists > Default Security List
3. Add Ingress Rule:
   - Source CIDR: 0.0.0.0/0  (paste in Source CIDR field, NOT Source Port Range)
   - IP Protocol: TCP
   - Destination Port Range: 8080

Ubuntu iptables (firewall layer 2):
This is required even if the Security List rule is set. Oracle Cloud applies
iptables rules on Ubuntu instances independently of the Security List.

SSH into the VM first (see Step 5), then run:
    sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
    sudo apt install -y iptables-persistent
    sudo netfilter-persistent save

---

## Step 5 — Connect via SSH

Mac:
    ssh -i ~/.ssh/id_ed25519 ubuntu@YOUR_VM_IP

Windows PowerShell:
    ssh -i $env:USERPROFILE\.ssh\id_ed25519 ubuntu@YOUR_VM_IP

Note: username is ubuntu (not opc — that is for Oracle Linux).
Type yes when asked about the fingerprint.

---

## Step 6 — Install Python and Dependencies

    sudo apt update && sudo apt install -y python3 python3-venv python3-pip python3.11 python3.11-venv

    sudo mkdir -p /opt/byd_logger
    sudo chown ubuntu:ubuntu /opt/byd_logger
    cd /opt/byd_logger

    python3.11 -m venv venv
    venv/bin/pip install --upgrade pip
    venv/bin/pip install pybyd aiohttp

Note: Must use python3.11 explicitly. Ubuntu 22.04 defaults to 3.10 but pybyd requires 3.11+.

---

## Step 7 — Fix the pyBYD imeiMD5 Bug (Required)

Without this fix, login fails with code 1033 "Network busy".

The bug: pyBYD hardcodes an all-zeros imeiMD5. BYD now requires MD5(username).toUpperCase().

Calculate your imeiMD5:
    /opt/byd_logger/venv/bin/python3 -c "
    import hashlib
    username = 'your-byd-account@email.com'
    print(hashlib.md5(username.encode()).hexdigest().upper())
    "

Find the current hardcoded value:
    grep -n "imei_md5" /opt/byd_logger/venv/lib/python3.11/site-packages/pybyd/config.py

Edit the file:
    nano /opt/byd_logger/venv/lib/python3.11/site-packages/pybyd/config.py

Find the imei_md5 line and replace the zeros string with your calculated MD5 value.

Note: If you upgrade pybyd in future, you will need to reapply this fix.

---

## Step 8 — Upload Script

From your local machine:

Mac:
    scp -i ~/.ssh/id_ed25519 byd_logger.py ubuntu@YOUR_VM_IP:/opt/byd_logger/

Windows PowerShell:
    scp -i $env:USERPROFILE\.ssh\id_ed25519 byd_logger.py ubuntu@YOUR_VM_IP:/opt/byd_logger/

---

## Step 9 — Generate HTTPS Certificate

The server now requires a TLS certificate. Run this once on the VM (replace YOUR_VM_IP with your actual IP):

    sudo openssl req -x509 -newkey rsa:4096 \
      -keyout /opt/byd_logger/key.pem \
      -out    /opt/byd_logger/cert.pem \
      -days 3650 -nodes \
      -subj "/CN=YOUR_VM_IP" \
      -addext "subjectAltName=IP:YOUR_VM_IP"

    sudo chown ubuntu:ubuntu /opt/byd_logger/cert.pem /opt/byd_logger/key.pem
    sudo chmod 640 /opt/byd_logger/key.pem

The `-addext "subjectAltName=IP:..."` line is required — Python's SSL library rejects IP-address certs that only have a CN.

Then download the cert to your local machine (run this from your local terminal, not the VM):

Mac:
    scp -i ~/.ssh/id_ed25519 ubuntu@YOUR_VM_IP:/opt/byd_logger/cert.pem .

Windows PowerShell:
    scp -i $env:USERPROFILE\.ssh\id_ed25519 ubuntu@YOUR_VM_IP:/opt/byd_logger/cert.pem .

Place `cert.pem` in the same folder as `correlate.py`. The script finds it automatically.

---

## Step 10 — Configure Credentials

    nano /opt/byd_logger/.env

Contents:
    BYD_USERNAME=your-secondary-byd@email.com
    BYD_PASSWORD=yourpassword
    BYD_COUNTRY_CODE=AU
    BYD_LANGUAGE=en
    BYD_TIME_ZONE=Australia/Melbourne
    BYD_BASE_URL=https://dilinkappoversea-au.byd.auto
    LOG_FILE=/opt/byd_logger/charge_sessions.csv
    STATE_FILE=/opt/byd_logger/session_state.json
    POLL_INTERVAL=60
    CHARGE_RATE_KW=2.3
    WEB_PORT=8080
    UTC_OFFSET_HOURS=10
    ACCESS_TOKEN=paste-a-long-random-string-here

Generate a secure token:
    python3.11 -c "import secrets; print(secrets.token_urlsafe(32))"

Protect the file:
    chmod 600 /opt/byd_logger/.env

---

## Step 11 — Test It

    cd /opt/byd_logger
    set -a
    source .env
    set +a
    venv/bin/python byd_logger.py

You should see:
    INFO  Web server listening on port 8080 (HTTPS)
    INFO  Connecting to BYD API ...
    INFO  Monitoring VIN: LXXXXXXXXXXXXXXXXX
    INFO  Polling every 60 seconds ...

When the car is plugged in and charging:
    INFO  ⚡ Charging started  SOC=82.0%  ODO=26050.0 km  power=1544W  local=14:32

When unplugged:
    INFO  ✅ Charging ended  SOC=100%  ODO=26050.0 km  duration=96.0 min  actual=2.48 kWh  avg=1552W

Press Ctrl+C once confirmed working.

---

## Step 12 — Install as a System Service

    sudo nano /etc/systemd/system/byd_logger.service

Paste:
    [Unit]
    Description=BYD Charge Session Logger
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    User=ubuntu
    WorkingDirectory=/opt/byd_logger
    EnvironmentFile=/opt/byd_logger/.env
    Environment="BYD_BASE_URL=https://dilinkappoversea-au.byd.auto"

Note: all credentials (username, password, token) belong in .env only — never add them as inline Environment= lines in the service file.
    ExecStart=/opt/byd_logger/venv/bin/python byd_logger.py
    Restart=on-failure
    RestartSec=30

    [Install]
    WantedBy=multi-user.target

Then:
    sudo systemctl daemon-reload
    sudo systemctl enable byd_logger
    sudo systemctl start byd_logger
    sudo systemctl status byd_logger

---

## Accessing Your Data

Download CSV:
    https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN

Note: the full URL including token grants access to your session data — do not share it publicly.

Health check (no token needed):
    https://YOUR_VM_IP:8080/health

View live logs:
    sudo journalctl -u byd_logger -f

View last 50 lines:
    sudo journalctl -u byd_logger -n 50

---

## Analysing Your Data — correlate.py

`correlate.py` fetches your BYD charge sessions and correlates them against PowerPal energy data to calculate solar vs grid charging, cost per session, efficiency, and more.

### Quick start (fully automatic)

Once you have run `get_powerpal_key.py` (see [POWERPAL_SETUP.md](POWERPAL_SETUP.md)), credentials are saved to `powerpal_ble.json` and everything is automatic:

**Windows:**
```powershell
py correlate.py --url "https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN"
```

**Mac:**
```bash
python3 correlate.py --url "https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN"
```

### PowerPal data — three options

| Option | When to use | Command |
|---|---|---|
| Automatic API | Default — credentials in `powerpal_ble.json` | *(nothing extra needed)* |
| Explicit API | Credentials not in file | `--powerpal-serial 00051664 --powerpal-key <key>` |
| Manual CSV | Downloaded from PowerPal app | `--powerpal powerpal_data.csv` |

### Optional arguments

| Argument | Default | Description |
|---|---|---|
| `--import-rate` | 0.30 | Grid import rate $/kWh |
| `--feedin-rate` | 0.06 | Solar feed-in tariff $/kWh |
| `--output` | correlation_report.csv | Output file path |
| `--sessions` | — | Local BYD sessions CSV (instead of --url) |

### Output

Prints a summary table to the terminal showing per-session: odometer, SOC% start→end, km driven, kWh charged, solar vs grid split, cost, and savings. Also saves `correlation_report.csv` and an EV Insights summary covering efficiency, battery health, seasonal variation, cost per km, and savings vs petrol.

### Would wholesale electricity cost less?

Add `--region` to enable the Amber wholesale estimate. For each session, it fetches the actual AEMO spot price from NEMWeb and calculates what you would have paid on a pass-through plan like Amber, compared to your fixed tariff.

**Windows:**
```powershell
py correlate.py --url "https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" `
  --region VIC `
  --import-rate 0.437 `
  --amber-network-rate 0.09
```

**Mac:**
```bash
python3 correlate.py --url "https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN" \
  --region VIC \
  --import-rate 0.437 \
  --amber-network-rate 0.09
```

Replace `VIC` with your NEM region (`QLD`, `NSW`, `VIC`, `SA`, or `TAS`). Set `--import-rate` to your actual fixed tariff from your electricity bill. Set `--amber-network-rate` to the network/distribution component shown on your bill (typically 9–12c/kWh).

| Argument | Default | Description |
|---|---|---|
| `--region` | *(off)* | NEM region — required to enable wholesale comparison |
| `--import-rate` | 0.30 | Your current fixed rate $/kWh |
| `--amber-network-rate` | 0.09 | Network charge $/kWh (check your bill) |
| `--amber-subscription` | 18.00 | Amber monthly fee $ — shown in output for reference only |
| `--utc-offset` | 10 | UTC offset: 10 = AEST, 11 = AEDT |

Results are cached in `amber_cache.csv` — already-priced sessions are never re-fetched, so subsequent runs are instant for existing sessions. AEMO price data is cached in `aemo_cache/`. For a full explanation of how the data is sourced and what the output means, see [AEMO_WHOLESALE.md](AEMO_WHOLESALE.md).

### Setting up PowerPal API access

See [POWERPAL_SETUP.md](POWERPAL_SETUP.md) for step-by-step instructions to retrieve your PowerPal API key via Bluetooth. This is a one-time setup — covers both Windows and Mac.

---

## Power Bill Analysis — analyse.py

> **Run quarterly** — after receiving a new electricity or gas bill, or every ~3 months of accumulated data.

`analyse.py` correlates solar feed-in with household usage across your electricity and gas bills to answer questions about electrification payback, battery viability, and true energy costs. It is not a daily/weekly tool — results are only meaningful once a full billing period of data is available.

**Before each run:** add your new bill CSV exports to your `elec_data` and `gas_data` folders.

```powershell
# Electricity + gas only
py analyse.py --elec "C:\path\to\elec_data" --gas "C:\path\to\gas_data"

# Full — includes BYD charging data
py analyse.py --elec "C:\path\to\elec_data" --gas "C:\path\to\gas_data" --byd "%BYD_URL%"
```

See [run.bat.example](run.bat.example) for a ready-to-use Windows shortcut.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| Permission denied publickey | Wrong username | Use ubuntu@ not opc@ |
| code=1033 Network busy | imeiMD5 bug in pyBYD | See Step 7 |
| code=3008 Wrong password | Wrong endpoint or credentials | Check BYD_BASE_URL and password |
| Cannot connect to host | Wrong BYD_BASE_URL | Use dilinkappoversea-au.byd.auto |
| Port 8080 not accessible | iptables not configured | See Step 4 — both firewalls needed |
| OSError Errno 98 address in use | Old process still running | sudo systemctl stop byd_logger |
| No module named pybyd | Using system Python not venv | Use venv/bin/python not python3 |
| Charging not detected | chargeState field confusion | Script uses chargeState==1, not chargingState |
| Session missed after restart | Old version without persistence | Upgrade to latest byd_logger.py |
| Silent polling stop | MQTT connection timeout | Script now auto-reconnects after 30s timeout |
| All sessions show H, no A detected | Home location wrong or GPS unavailable | Delete /opt/byd_logger/home_location.json to reset |
| FileNotFoundError cert.pem / key.pem | Cert not generated yet | Run Step 9 commands on the VM |
| SSL CERTIFICATE_VERIFY_FAILED | cert.pem not alongside correlate.py | Download cert from VM — see Step 9 |
| hostname mismatch / IP SANs error | Cert generated without -addext SAN | Regenerate cert with the full Step 9 command |

---

## Repository

https://github.com/Tgoodman-nz/byd_charge_logger

---

## Notes

- Time zone: UTC_OFFSET_HOURS=10 for AEST, 11 for AEDT (daylight saving Oct-Apr)
- kWh estimated: Portable EVSE on 10A = ~2.3 kW. Use kwh_charged_actual for real figures.
- kWh actual: Calculated from gl field (watts) averaged across the session.
- chargeState values: 0=not connected, 1=actively charging, 15=connected but not charging
- pyBYD is alpha software. BYD may change their API. Check https://github.com/jkaberg/pyBYD
- Australian app connectivity launched December 2025. Always use the -au endpoint.
- Polling rate: 60 seconds is appropriate. Do not poll more frequently.
- imeiMD5 fix: Confirmed working for Australian accounts.
  May be fixed in a future pyBYD release — reapply after any pybyd upgrade.
- Session persistence: session_state.json is written every poll during charging.
  If the service restarts mid-session it resumes from the original start time.
- Home/away detection: GPS coordinates are captured at the start of each charge session.
  After two sessions within 500m of each other, that location is saved as home in
  home_location.json. All subsequent sessions are automatically labelled H or A.
  Sessions with no GPS fix (e.g. car in a garage with no satellite signal) default to H.
  Existing sessions with no location column are also treated as H.
  If home detection is wrong, delete /opt/byd_logger/home_location.json on the VM
  and it will re-detect from the next two sessions. You can also delete
  gps_sessions.json to clear all stored GPS readings and start fresh.