"""
BYD Charge Session Logger
Polls the BYD cloud API and logs charge sessions to a CSV file.
Also serves the CSV over HTTPS with token authentication.

Access your data at:
  https://YOUR_VM_IP:8080/sessions.csv?token=YOUR_TOKEN

Charging detection: chargeState==1 means actively charging.
gl field = actual charging power in watts.
Session state is persisted to disk so restarts mid-session don't lose data.
"""

import asyncio
import csv
import json
import logging
import math
import os
import secrets
import signal
import ssl
from aiohttp import web
from datetime import datetime, timezone, timedelta
from pathlib import Path

from pybyd import BydClient, BydConfig

# ── Configuration (set via environment variables) ──────────────────────────
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "60"))
LOG_FILE         = os.getenv("LOG_FILE", "charge_sessions.csv")
STATE_FILE       = os.getenv("STATE_FILE", "session_state.json")
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO")
CHARGE_RATE_KW   = float(os.getenv("CHARGE_RATE_KW", "2.3"))
WEB_PORT         = int(os.getenv("WEB_PORT", "8080"))
ACCESS_TOKEN     = os.getenv("ACCESS_TOKEN", "")
CERT_FILE          = os.getenv("CERT_FILE",          "/opt/byd_logger/cert.pem")
KEY_FILE           = os.getenv("KEY_FILE",           "/opt/byd_logger/key.pem")
UTC_OFFSET_HOURS   = int(os.getenv("UTC_OFFSET_HOURS", "10"))
HOME_RADIUS_M      = int(os.getenv("HOME_RADIUS_M",    "500"))
GPS_SESSIONS_FILE  = os.getenv("GPS_SESSIONS_FILE",  "/opt/byd_logger/gps_sessions.json")
HOME_LOCATION_FILE = os.getenv("HOME_LOCATION_FILE", "/opt/byd_logger/home_location.json")
REQUEST_TIMEOUT  = 30
RECONNECT_DELAY  = 60
# ───────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("byd_logger")

CSV_HEADERS = [
    "session_id",
    "date_local",
    "start_time_local",
    "end_time_local",
    "start_time_utc",
    "end_time_utc",
    "duration_minutes",
    "soc_start_pct",
    "soc_end_pct",
    "soc_delta_pct",
    "kwh_charged_estimated",
    "kwh_charged_actual",
    "avg_charge_power_w",
    "odo_start_km",
    "odo_end_km",
    "km_driven_since_last_charge",
    "range_km",
    "efficiency_kwh_per_100km",
    "lifetime_efficiency_kwh_per_100km",
    "location",
    "notes",
]


def to_local(dt: datetime) -> datetime:
    return dt + timedelta(hours=UTC_OFFSET_HOURS)


def ensure_csv(path: str) -> None:
    p = Path(path)
    if not p.exists():
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        log.info("Created log file: %s", path)
        return
    with open(path, newline="") as f:
        raw_rows = list(csv.reader(f))
    if not raw_rows:
        return
    old_headers = raw_rows[0]
    if old_headers != CSV_HEADERS:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            for raw in raw_rows[1:]:
                # Map values by old header position, then fill extras positionally
                row = {old_headers[i]: raw[i]
                       for i in range(min(len(old_headers), len(raw)))}
                extras = raw[len(old_headers):]
                # location was appended after the original header — pick it up positionally
                if "location" not in old_headers and extras:
                    row["location"] = extras[0]
                writer.writerow({k: row.get(k, "") for k in CSV_HEADERS})
        log.info("Repaired CSV headers: %s", path)


def append_session(row: dict) -> None:
    with open(LOG_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
    log.info(
        "Session logged  id=%s  %s %s→%s  soc %s%%→%s%%  "
        "est %.2f kWh  actual %s kWh  avg %sW",
        row["session_id"],
        row["date_local"],
        row["start_time_local"],
        row["end_time_local"],
        row["soc_start_pct"],
        row["soc_end_pct"],
        row["kwh_charged_estimated"],
        row["kwh_charged_actual"] or "n/a",
        row["avg_charge_power_w"] or "n/a",
    )


# ── Session state persistence ───────────────────────────────────────────────

def save_session_state(session_start: datetime, soc_at_start, odo_at_start,
                       power_readings: list, session_lat=None, session_lon=None) -> None:
    """Write current in-progress session to disk."""
    state = {
        "session_start_utc": session_start.isoformat(),
        "soc_at_start":      soc_at_start,
        "odo_at_start":      odo_at_start,
        "power_readings":    power_readings,
        "session_lat":       session_lat,
        "session_lon":       session_lon,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_session_state():
    """Load in-progress session from disk if it exists."""
    if not Path(STATE_FILE).exists():
        return None, None, None, [], None, None
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        session_start = datetime.fromisoformat(state["session_start_utc"])
        log.info("Resumed in-progress session from %s local  SOC=%s%%  ODO=%s km",
                 to_local(session_start).strftime("%H:%M"),
                 state["soc_at_start"],
                 state["odo_at_start"])
        return (session_start,
                state["soc_at_start"],
                state["odo_at_start"],
                state.get("power_readings", []),
                state.get("session_lat"),
                state.get("session_lon"))
    except Exception as e:
        log.warning("Could not load session state: %s", e)
        return None, None, None, [], None, None


def clear_session_state() -> None:
    """Delete the session state file after a session completes."""
    try:
        Path(STATE_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ── GPS and home location ───────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R     = 6_371_000
    phi1  = math.radians(lat1)
    phi2  = math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a     = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_home_location():
    if not Path(HOME_LOCATION_FILE).exists():
        return None
    try:
        data = json.loads(Path(HOME_LOCATION_FILE).read_text())
        return data["lat"], data["lon"]
    except Exception as e:
        log.warning("Could not load home location: %s", e)
        return None


def save_home_location(lat: float, lon: float) -> None:
    try:
        Path(HOME_LOCATION_FILE).write_text(json.dumps({"lat": lat, "lon": lon}))
    except Exception as e:
        log.warning("Could not save home location: %s", e)


def load_gps_sessions() -> dict:
    if not Path(GPS_SESSIONS_FILE).exists():
        return {}
    try:
        return json.loads(Path(GPS_SESSIONS_FILE).read_text())
    except Exception as e:
        log.warning("Could not load GPS sessions: %s", e)
        return {}


def save_gps_sessions(sessions: dict) -> None:
    try:
        Path(GPS_SESSIONS_FILE).write_text(json.dumps(sessions))
    except Exception as e:
        log.warning("Could not save GPS sessions: %s", e)


def detect_home(gps_sessions: dict) -> tuple:
    """Return (lat, lon) of home. Finds closest pair within HOME_RADIUS_M; falls back to first session."""
    coords = [
        (v["lat"], v["lon"])
        for v in gps_sessions.values()
        if v and v.get("lat") is not None and v.get("lon") is not None
    ]
    if len(coords) < 2:
        return coords[0] if coords else (0.0, 0.0)

    best_dist, best_pair = float("inf"), None
    for i, a in enumerate(coords):
        for b in coords[i + 1:]:
            d = haversine_m(a[0], a[1], b[0], b[1])
            if d < best_dist:
                best_dist, best_pair = d, (a, b)

    if best_pair and best_dist <= HOME_RADIUS_M:
        return ((best_pair[0][0] + best_pair[1][0]) / 2,
                (best_pair[0][1] + best_pair[1][1]) / 2)

    log.info("No home cluster found (closest pair %.0fm apart) — defaulting to first session", best_dist)
    return coords[0]


def determine_location(session_id: str, lat, lon) -> str:
    """Return 'H' or 'A'. Defaults to 'H' if GPS unavailable or home not yet established."""
    if lat is None or lon is None:
        return "H"
    try:
        gps_sessions = load_gps_sessions()
        gps_sessions[session_id] = {"lat": lat, "lon": lon}
        save_gps_sessions(gps_sessions)

        home = load_home_location()
        if home is None:
            if len(gps_sessions) < 2:
                return "H"
            home = detect_home(gps_sessions)
            save_home_location(*home)
            log.info("Home location established: %.6f, %.6f", home[0], home[1])

        dist = haversine_m(lat, lon, home[0], home[1])
        loc  = "H" if dist <= HOME_RADIUS_M else "A"
        log.info("Session %s: %s (%.0fm from home)", session_id, loc, dist)
        return loc
    except Exception as e:
        log.warning("Location determination failed: %s — defaulting to H", e)
        return "H"


def get_realtime_fields(realtime):
    raw          = getattr(realtime, "raw", {})
    charge_state = raw.get("chargeState", 0)
    is_charging  = charge_state == 1
    gl_watts     = max(raw.get("gl", 0.0), 0.0)
    return {
        "is_charging":   is_charging,
        "charge_state":  charge_state,
        # Treat 0 as None — the API transiently zeroes these fields when chargeState changes.
        # A 0 odometer or 0% SOC is never valid for a car in use.
        "soc":           getattr(realtime, "elec_percent", None) or None,
        "odo":           getattr(realtime, "total_mileage", None) or None,
        "range_km":      getattr(realtime, "endurance_mileage_v2", None),
        "lifetime_eff":  raw.get("totalConsumptionEn", ""),
        "speed":         getattr(realtime, "speed", None),
        "gl_watts":      gl_watts,
    }


def parse_lifetime_efficiency(raw_str: str) -> str:
    if not raw_str:
        return ""
    try:
        return raw_str.replace("kW·h/100km", "").replace("kW•h/100km", "").strip()
    except Exception:
        return raw_str


def resume_csv_state():
    """Resume session count and last odo from existing CSV."""
    session_count   = 0
    odo_last_charge = None
    if Path(LOG_FILE).exists():
        with open(LOG_FILE) as f:
            rows = list(csv.DictReader(f))
            session_count = len(rows)
            if rows:
                try:
                    odo_last_charge = float(rows[-1]["odo_end_km"])
                except (ValueError, KeyError):
                    pass
    return session_count, odo_last_charge


# ── Web server ──────────────────────────────────────────────────────────────

async def handle_csv(request: web.Request) -> web.Response:
    token = request.rel_url.query.get("token", "")
    if not ACCESS_TOKEN:
        return web.Response(status=500, text="ACCESS_TOKEN not configured.")
    if not secrets.compare_digest(token, ACCESS_TOKEN):
        log.warning("Unauthorised access attempt from %s", request.remote)
        return web.Response(status=401, text="Unauthorised.")
    if not Path(LOG_FILE).exists():
        return web.Response(status=404, text="No sessions logged yet.")
    content = Path(LOG_FILE).read_text()
    return web.Response(
        body=content,
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=charge_sessions.csv"},
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="BYD logger running ✓")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/sessions.csv", handle_csv)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT, ssl_context=ssl_ctx)
    await site.start()
    log.info("Web server listening on port %s (HTTPS)", WEB_PORT)
    token_preview = (ACCESS_TOKEN[:8] + "...") if ACCESS_TOKEN else "<not set>"
    log.info("Download URL: https://YOUR_VM_IP:%s/sessions.csv?token=%s",
             WEB_PORT, token_preview)
    return runner


# ── Single polling session ──────────────────────────────────────────────────

async def run_polling_session(config, session_count, odo_last_charge):
    """
    Connect to BYD API and poll until a timeout or error.
    Returns updated (session_count, odo_last_charge) for the outer loop.
    """
    async with BydClient(config) as client:
        await client.login()
        vehicles = await client.get_vehicles()
        if not vehicles:
            log.error("No vehicles found.")
            return session_count, odo_last_charge

        vin = vehicles[0].vin
        log.info("Monitoring VIN: %s...%s", vin[:4], vin[-4:])
        log.info("Polling every %s seconds …", POLL_INTERVAL)

        # Restore any in-progress session from disk
        session_start, soc_at_start, odo_at_start, power_readings, \
            session_lat, session_lon = load_session_state()
        was_charging = session_start is not None

        if was_charging:
            log.info("Continuing in-progress session from %s",
                     to_local(session_start).strftime("%H:%M local"))

        # Fallback values for when the API transiently returns 0 for soc/odo
        last_valid_soc = soc_at_start
        last_valid_odo = odo_at_start

        while True:
            try:
                realtime = await asyncio.wait_for(
                    client.get_vehicle_realtime(vin),
                    timeout=REQUEST_TIMEOUT
                )
                fields      = get_realtime_fields(realtime)
                is_charging = fields["is_charging"]
                soc         = fields["soc"]
                odo         = fields["odo"]
                gl_watts    = fields["gl_watts"]
                now_utc     = datetime.now(timezone.utc)
                now_local   = to_local(now_utc)

                if soc:
                    last_valid_soc = soc
                if odo:
                    last_valid_odo = odo

                if is_charging and not was_charging:
                    # ── Session started ──
                    session_start  = now_utc
                    soc_at_start   = soc or last_valid_soc
                    odo_at_start   = odo or last_valid_odo
                    power_readings = [gl_watts] if gl_watts > 0 else []
                    session_lat = session_lon = None
                    try:
                        gps = await asyncio.wait_for(
                            client.get_gps_info(vin), timeout=30)
                        session_lat = gps.latitude
                        session_lon = gps.longitude
                        if session_lat is not None:
                            log.info("GPS at charge start: %.6f, %.6f",
                                     session_lat, session_lon)
                    except Exception as gps_err:
                        log.debug("GPS unavailable at session start: %s", gps_err)
                    save_session_state(session_start, soc_at_start,
                                       odo_at_start, power_readings,
                                       session_lat, session_lon)
                    log.info("⚡ Charging started  SOC=%s%%  ODO=%s km  "
                             "power=%.0fW  local=%s",
                             soc, odo, gl_watts, now_local.strftime("%H:%M"))

                elif is_charging and was_charging:
                    # ── Session ongoing ──
                    if gl_watts > 0:
                        power_readings.append(gl_watts)
                    # Update persisted power readings periodically
                    save_session_state(session_start, soc_at_start,
                                       odo_at_start, power_readings,
                                       session_lat, session_lon)

                elif not is_charging and was_charging and session_start is not None:
                    # ── Session ended ──
                    soc_end = soc or last_valid_soc
                    odo_end = odo or last_valid_odo
                    if soc != soc_end or odo != odo_end:
                        log.warning("API returned 0 for soc/odo at session end — "
                                    "using last valid values (soc=%s%%, odo=%s km)",
                                    soc_end, odo_end)

                    duration_min   = round(
                        (now_utc - session_start).total_seconds() / 60, 1)
                    kwh_est        = round(duration_min / 60 * CHARGE_RATE_KW, 3)
                    session_count += 1
                    location       = determine_location(
                        f"S{session_count:04d}", session_lat, session_lon)
                    start_local    = to_local(session_start)

                    avg_power  = round(sum(power_readings) / len(power_readings), 0) \
                                 if power_readings else None
                    kwh_actual = round(avg_power * (duration_min / 60) / 1000, 3) \
                                 if avg_power else None

                    km_driven = ""
                    if odo_at_start and odo_last_charge:
                        km_driven = round(odo_at_start - odo_last_charge, 1)

                    efficiency  = ""
                    kwh_for_eff = kwh_actual or kwh_est
                    if km_driven and km_driven > 0 and kwh_for_eff > 0:
                        efficiency = round(kwh_for_eff / km_driven * 100, 1)

                    lifetime_eff = parse_lifetime_efficiency(fields["lifetime_eff"])

                    append_session({
                        "session_id":                        f"S{session_count:04d}",
                        "date_local":                        start_local.strftime("%Y-%m-%d"),
                        "start_time_local":                  start_local.strftime("%H:%M:%S"),
                        "end_time_local":                    now_local.strftime("%H:%M:%S"),
                        "start_time_utc":                    session_start.strftime("%H:%M:%S"),
                        "end_time_utc":                      now_utc.strftime("%H:%M:%S"),
                        "duration_minutes":                  duration_min,
                        "soc_start_pct":                     soc_at_start,
                        "soc_end_pct":                       soc_end,
                        "soc_delta_pct":                     (soc_end - soc_at_start)
                                                             if (soc_end and soc_at_start) else "",
                        "kwh_charged_estimated":             kwh_est,
                        "kwh_charged_actual":                kwh_actual or "",
                        "avg_charge_power_w":                avg_power or "",
                        "odo_start_km":                      odo_at_start,
                        "odo_end_km":                        odo_end,
                        "km_driven_since_last_charge":       km_driven,
                        "range_km":                          fields["range_km"],
                        "efficiency_kwh_per_100km":          efficiency,
                        "lifetime_efficiency_kwh_per_100km": lifetime_eff,
                        "location":                          location,
                        "notes":                             "",
                    })

                    clear_session_state()
                    odo_last_charge = odo_end or odo_last_charge
                    session_start   = None
                    soc_at_start    = None
                    odo_at_start    = None
                    power_readings  = []
                    session_lat     = None
                    session_lon     = None
                    log.info("✅ Charging ended  SOC=%s%%  ODO=%s km  "
                             "duration=%s min  actual=%s kWh  avg=%sW",
                             soc_end, odo_end, duration_min,
                             kwh_actual or "n/a", avg_power or "n/a")

                else:
                    log.debug("Status: charging=%s  chargeState=%s  SOC=%s%%  "
                              "ODO=%s km  power=%.0fW  speed=%s km/h",
                              is_charging, fields["charge_state"],
                              soc, odo, gl_watts, fields["speed"])

                was_charging = is_charging

            except asyncio.TimeoutError:
                log.warning("Poll timed out after %ss — reconnecting …",
                            REQUEST_TIMEOUT)
                return session_count, odo_last_charge

            except Exception as exc:
                log.warning("Poll error — reconnecting: %s", exc)
                return session_count, odo_last_charge

            await asyncio.sleep(POLL_INTERVAL)


# ── Outer reconnection loop ─────────────────────────────────────────────────

async def poll_byd() -> None:
    ensure_csv(LOG_FILE)

    if not ACCESS_TOKEN:
        log.error("ACCESS_TOKEN not set. Add to .env and restart.")
        return

    config = BydConfig.from_env()
    session_count, odo_last_charge = resume_csv_state()
    log.info("Starting — session count=%s  last odo=%s km",
             session_count, odo_last_charge)

    while True:
        try:
            log.info("Connecting to BYD API …")
            session_count, odo_last_charge = await run_polling_session(
                config, session_count, odo_last_charge
            )
            log.info("Polling session ended — reconnecting in %ss …",
                     RECONNECT_DELAY)
        except Exception as exc:
            log.error("Unexpected error: %s — reconnecting in %ss …",
                      exc, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


# ── Entry point ─────────────────────────────────────────────────────────────

async def main_async() -> None:
    runner = await start_web_server()
    try:
        await poll_byd()
    finally:
        await runner.cleanup()


def main() -> None:
    loop = asyncio.new_event_loop()

    def _shutdown(*_):
        log.info("Shutting down …")
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(main_async())
    finally:
        loop.close()


if __name__ == "__main__":
    main()