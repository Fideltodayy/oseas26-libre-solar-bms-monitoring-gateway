# MQTT contract v1

This is the data contract between the Libre Solar BMS and every platform that
reads it. The machine-readable source of truth is the JSON Schemas in
[`schemas/v1/`](../schemas/v1/), with a valid example of every message in
[`schemas/v1/examples/`](../schemas/v1/examples/). CI validates both, and every
message the simulator publishes is validated before it is sent.

## Design in one paragraph

The BMS (or the relay next to it) publishes **raw measurements** using the
firmware's own ThingSet names, so publishing from firmware needs no mapping
layer. The **gateway** turns that stream into a **low-frequency summary** and
**fault events**. Home Assistant and Prospect, which are built for timeseries,
read the raw stream. MicroPowerManager is an asset and customer platform, not a
timeseries database, so it reads only the summary and events.

```text
                    ┌─────────── meas (every 5 s) ──────────► Home Assistant, Prospect
BMS / relay / sim ──┤
                    └─► gateway ── summary (15 min) + event ─► MicroPowerManager, SMS alerts
```

## Topics

All topics start with `libre-solar/bms/{device_id}/`. `device_id` matches
`^[A-Za-z0-9_-]{1,64}$`.

| Topic suffix | Published by | When | QoS | Retained | Schema | Read by |
|---|---|---|---|---|---|---|
| `status` | BMS / relay / sim, plus the broker (Last Will) | on connect, clean disconnect, or lost connection | 1 | yes | plain string `online` / `offline` | all (availability, last seen) |
| `info` | BMS / relay / sim | every (re)connect | 1 | yes | [`info.schema.json`](../schemas/v1/info.schema.json) | all (identity, capacity, limits) |
| `meas` | BMS / relay / sim | every `PUBLISH_INTERVAL_S` (default 5 s) | 0 | no | [`meas.schema.json`](../schemas/v1/meas.schema.json) | Home Assistant, Prospect, gateway |
| `summary` | gateway | every 15 min, **and immediately** when health or active faults change | 1 | yes | [`summary.schema.json`](../schemas/v1/summary.schema.json) | MicroPowerManager |
| `event` | gateway | on change only | 1 | no | [`event.schema.json`](../schemas/v1/event.schema.json) | MicroPowerManager, SMS alerts |

Because `status`, `info` and `summary` are retained, a consumer that starts
late (or restarts) sees the current state immediately without waiting for the
next cycle.

## Conventions

- **Units** are in the field name: `_V`, `_A`, `_degC`, `_pct` (ThingSet style) in
  `meas` and `info`; `_v`, `_mv`, `_c`, `_pct`, `_wh` in gateway messages.
- **Current sign** follows the firmware: **positive = charging, negative =
  discharging** (the coulomb counter in `app/src/bms_soc.c` adds `current`).
- **Timestamps** (`ts`) are ISO 8601 in UTC, e.g. `2026-10-22T12:00:05Z`.
- **`schema_version`** is `1`. Adding an optional field does not change it, so
  consumers must ignore fields they don't know. Renaming, removing or changing
  the meaning of a field bumps the version.
- **`source`** in `info` is `simulator`, `serial` or `firmware`, so simulated
  data can never be mistaken for a real battery.

## `meas`: raw measurements

The `meas` object is the ThingSet `Meas` group from
[`app/src/data_objects.c`](https://github.com/LibreSolar/bms-firmware/blob/main/app/src/data_objects.c),
using the same names, units and rounding.

```json
{
  "schema_version": 1,
  "device_id": "bms-sim-01",
  "ts": "2026-10-22T12:00:05Z",
  "seq": 1284,
  "meas": {
    "rPackVoltage_V": 53.25,
    "rStackVoltage_V": 53.25,
    "rPackCurrent_A": 26.77,
    "rCellTemps_degC": [26.6, 26.8],
    "rICTemp_degC": 28.9,
    "rMOSFETTemp_degC": 29.1,
    "rSOC_pct": 88.8,
    "rErrorFlags": 0,
    "rBmsState": 3,
    "rCellVoltages_V": [3.328, 3.32, 3.332, 3.327, 3.33, 3.329, 3.327, 3.331, 3.327, 3.329, 3.326, 3.327, 3.329, 3.333, 3.327, 3.328],
    "rCellAvgVoltage_V": 3.328,
    "rCellMinVoltage_V": 3.32,
    "rCellMaxVoltage_V": 3.333,
    "rBalancingStatus": 8340
  }
}
```

`seq` increases by one per message (reset on restart), so consumers can count
lost messages on poor links.

`rStackVoltage_V`, `rPackCurrent_A` and `rMOSFETTemp_degC` exist only on boards
built with `CONFIG_BMS_IC_SWITCHES` (the BMS C1 has it), and
`rShuntTemp_degC` only with `CONFIG_BMS_IC_SHUNT_TEMP`. They are therefore
optional in the schema.

### `rBmsState`

From `enum bms_state` in `include/bms/bms.h`:

| Value | Name | Meaning |
|---|---|---|
| 0 | `OFF` | charging and discharging disabled |
| 1 | `CHG` | only charging allowed |
| 2 | `DIS` | only discharging allowed |
| 3 | `NORMAL` | charging and discharging allowed |
| 4 | `SHUTDOWN` | BMS is shutting down |

### `rErrorFlags`

Bitmask of `BMS_ERR_*` from `include/bms/bms_common.h`. Gateway messages
(`summary.active_faults`, `event.fault`) use these names without the `BMS_ERR_`
prefix.

| Bit | Name | Meaning |
|---|---|---|
| 0 | `CELL_UNDERVOLTAGE` | cell below undervoltage limit |
| 1 | `CELL_OVERVOLTAGE` | cell above overvoltage limit |
| 2 | `SHORT_CIRCUIT` | pack short circuit (discharge direction) |
| 3 | `DIS_OVERCURRENT` | pack overcurrent, discharge |
| 4 | `CHG_OVERCURRENT` | pack overcurrent, charge |
| 5 | `OPEN_WIRE` | cell open wire |
| 6 | `DIS_UNDERTEMP` | below discharge minimum temperature |
| 7 | `DIS_OVERTEMP` | above discharge maximum temperature |
| 8 | `CHG_UNDERTEMP` | below charge minimum temperature |
| 9 | `CHG_OVERTEMP` | above charge maximum temperature |
| 10 | `INT_OVERTEMP` | internal (BMS IC) overtemperature |
| 11 | `CELL_FAILURE` | cell voltage difference too high |
| 12 | `DIS_OFF` | discharge FET off although it should be on |
| 13 | `CHG_OFF` | charge FET off although it should be on |
| 14 | `FET_OVERTEMP` | MOSFET overtemperature |

`bms_gateway.contract.decode_error_flags()` turns the bitmask into names.

### `rBalancingStatus`

Bit *n* set means cell *n* (0-based, same order as `rCellVoltages_V`) is being
balanced.

## `info`: identity and protection limits

Published retained on connect. `device` and `conf` are the ThingSet `Device`
and `Conf` groups. Consumers use `conf.sNominalCapacity_Ah` (for cycle
counting) and the protection limits (for warning thresholds below the BMS's own
cut-offs). The cell count is the length of `meas.rCellVoltages_V`.

Full example: [`schemas/v1/examples/info.json`](../schemas/v1/examples/info.json).

## `summary`: for MicroPowerManager

What an operator needs to decide whether to act, aggregated over a window
(default 900 s). It is published retained at the end of every window, and
**immediately** when `health.status` or `active_faults` change, so MPM never
waits 15 minutes to learn about a critical fault.

```json
{
  "schema_version": 1,
  "device_id": "bms-sim-01",
  "ts": "2026-10-22T12:00:05Z",
  "window_s": 900,
  "last_seen": "2026-10-22T12:00:05Z",
  "bms_state": "NORMAL",
  "soc_pct": 88.8,
  "min_soc_pct": 84.1,
  "pack_voltage_v": 53.25,
  "cell_imbalance_mv": 14,
  "max_temp_c": 29.4,
  "energy_charged_wh": 1180.5,
  "energy_discharged_wh": 0.0,
  "equivalent_cycles": 142.6,
  "soh_pct": 97.1,
  "soh_method": "cycle_model",
  "active_faults": [],
  "health": { "status": "ok", "reasons": [] }
}
```

| Field | How it is derived |
|---|---|
| `min_soc_pct` | lowest `rSOC_pct` in the window. If it is regularly low, the system may be undersized |
| `cell_imbalance_mv` | largest `rCellMaxVoltage_V − rCellMinVoltage_V` in the window |
| `max_temp_c` | highest cell, IC or MOSFET temperature in the window |
| `energy_charged_wh` / `energy_discharged_wh` | ∫ V·I dt over the window, split by current sign |
| `equivalent_cycles` | lifetime discharged Ah ÷ `sNominalCapacity_Ah` |
| `soh_pct` | **an estimate.** The firmware does not measure SoH. `soh_method` says how it was obtained; `null` if unknown |
| `health.status` | `ok` / `warning` / `critical`, and `health.reasons` lists plain-language reasons |

## `event`: fault and connectivity changes

Published when a fault is raised or cleared, or when the device goes offline
or comes back online. `event_id` is a UUID so consumers can drop duplicates
(QoS 1 can deliver a message twice). `message` fits in one SMS.

```json
{
  "schema_version": 1,
  "device_id": "bms-sim-01",
  "ts": "2026-10-22T13:42:10Z",
  "event_id": "5f0c1c3e-8b8e-4d55-9a57-2f1e6a3d7c42",
  "type": "fault_raised",
  "fault": "CHG_OVERTEMP",
  "severity": "critical",
  "message": "Battery too hot to charge (46.2 °C). Charging stopped by the BMS.",
  "recommended_action": "Check ventilation and shade the battery enclosure. Charging resumes automatically below 40 °C."
}
```

`type` is one of `fault_raised`, `fault_cleared`, `device_online` or
`device_offline`. For `device_*` events `fault` is `null`.

## Home Assistant discovery

The gateway publishes one retained
[device discovery](https://www.home-assistant.io/integrations/mqtt/#device-discovery-payload)
message per BMS on `homeassistant/device/libre_solar_bms_<device_id>/config`,
once it has seen both `info` and `meas`. The payload is rebuilt when the
cell count or the set of optional fields changes, and resent when Home
Assistant announces `online` on `homeassistant/status`.

All entities belong to one device, named after `cManufacturer`, `cDeviceType`
and `cFirmwareVersion`. They use `libre-solar/bms/<id>/status` for
availability, so they show as unavailable when the BMS goes offline.

| From `meas` (live) | From `summary` (gateway) |
|---|---|
| State of charge, pack voltage, current, power | Health (`ok`/`warning`/`critical`, reasons as attribute) |
| Lowest/highest cell voltage, cell imbalance | Health reasons, active faults |
| Battery temperatures, MOSFET and BMS IC temperature | Estimated state of health, equivalent full cycles |
| BMS state, problem (binary sensor) | |
| Per-cell voltages (diagnostic) | |

## Validating messages

Python:

```python
from bms_gateway import contract
contract.validate("meas", message)  # raises jsonschema.ValidationError
```

Any other language: load the files in `schemas/v1/` with a JSON Schema
draft 2020-12 validator.

## Security for field deployment

The development broker accepts anonymous plaintext connections. Before
deploying in the field, enable a Mosquitto `password_file`, give each device an
ACL limited to `libre-solar/bms/<its id>/#`, and use TLS on port 8883.
