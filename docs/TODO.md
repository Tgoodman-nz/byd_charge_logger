# TODO / Future Work

## Trip Logging

### Background
Investigated on 2026-05-20 using a temporary `status.json` written on each poll.
The `byd_logger.py` status file write can be removed once no longer needed.

### chargeState values discovered
| Value | Meaning |
|---|---|
| `0` | Parked / idle (inferred) |
| `1` | Charging |
| `15` | Driving |

### Useful raw fields for trip logging
| Field | Notes |
|---|---|
| `gl` | Instantaneous motor power in **watts**. Negative = driving load, positive = regen/charging. Current code discards negatives via `max(gl, 0)` — would need to be captured for trips. |
| `speed` | km/h |
| `energyConsumption` | Car's own kWh/100km for current trip segment |
| `nearestEnergyConsumption` | Recent segment efficiency (kWh/100km) |
| `recent50kmEnergy` | Rolling 50km average efficiency string |
| `totalConsumptionEn` | Lifetime average efficiency |
| `powerGear` | Gear position (3 = Drive observed) |

### Proposed implementation
Same pattern as charge session detection in `byd_logger.py`:
- `chargeState == 15` transition → open trip (record start ODO, SOC, timestamp)
- Poll during trip: collect `speed` and `gl` readings
- Transition away from `15` → close trip, append row to `trips.csv`

Suggested `trips.csv` columns:
`trip_id, date_local, start_time_local, end_time_local, duration_minutes,`
`odo_start_km, odo_end_km, km_driven, soc_start_pct, soc_end_pct, soc_delta_pct,`
`max_speed_kmh, avg_speed_kmh, avg_power_kw, max_power_kw, car_efficiency_kwh_100km`

### Known limitation
Poll interval is 60 seconds. Short trips (< ~2 min) may be missed entirely or
have imprecise start/end times. Several trips in `EC_database.db` were under 5 minutes.
Consider reducing `POLL_INTERVAL` if trip accuracy matters.

### Correlation with EC_database.db
The car's `EC_database.db` (Android SQLite) stores the same data at trip granularity
but is only accessible by copying the file off the phone. Trip logger would give
continuous, automatic equivalent. Schema reference in `EC_database.db`:
`EnergyConsumption(_id, month, date, start_timestamp, end_timestamp, is_deleted, duration, trip, electricity, fuel)`
