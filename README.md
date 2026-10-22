<p align="center">
  <a href="https://github.com/EnAccess/oseas26-libre-solar-bms-monitoring-gateway">
    <img
      src="https://drive.google.com/uc?id=1gtL_p7l3HbOcCzc09A7KW5d7B5qn-BDs"
      alt="Open Remote Monitoring Gateway for Libre Solar BMS"
      width="640"
    >
  </a>
</p>
<p align="center">
    October 26-27 | Open Source in Energy Access Symposium Hackathon | Kigali, Rwanda
</p>

---

# Open Remote Monitoring Gateway for Libre Solar BMS

An open, deployable remote monitoring gateway that reads live data from the Libre
Solar BMS, publishes it to a standard MQTT layer, and makes it visible in the
tools operators and the community already use.

## This submission

The core of this repository is the **MQTT contract**
([`docs/mqtt-schema.md`](docs/mqtt-schema.md), [`schemas/v1/`](schemas/v1/)). It
uses the Libre Solar firmware's own ThingSet field names, so a real BMS can
publish it without a mapping layer. A small **gateway** service turns the live
stream into what each platform needs.

```text
BMS ─UART/ThingSet─► relay ─┐
                simulator ──┴─meas, info, status──► Mosquitto ──► Home Assistant (auto-discovered, full stream)
                                            │
                                            └──► gateway ──summary, event──► MicroPowerManager, SMS alerts
                                                    └─ HA discovery, health, faults, SoH, cycles
```

| Expected outcome | Status |
|---|---|
| BMS simulator | ✅ `bms_gateway/simulator.py`, using the firmware's LFP OCV curve and protection limits |
| Documented MQTT schema | ✅ `docs/mqtt-schema.md` plus JSON Schemas and examples, validated in CI |
| Home Assistant auto-discovery | ✅ 34 entities per 16-cell BMS, appearing with no manual setup in the demo stack |
| Health summary and fault events (for MPM) | ✅ `bms_gateway/gateway.py`, with operator messages and recommended actions |
| MPM ingestion and battery-health view | 🚧 in progress |
| Real transport path (ThingSet over serial) | ✅ `bms_gateway/relay.py` for the BMS C1 UART or USB, tested end to end against a ThingSet emulator. 🔜 to be proven on the shared BMS at the event ([guide](docs/hardware.md)) |
| Field-deployment documentation | 🚧 in progress |

### Quick start

Requires Docker. Works on x86 and ARM (including Apple Silicon and Raspberry Pi).

```bash
docker compose up --build -d
```

Open Home Assistant at <http://localhost:8123> and log in as `admin` / `admin`
(demo credentials, set with `HA_USERNAME` / `HA_PASSWORD`). The `ha-setup`
container completes onboarding and adds the MQTT integration, so the
**Libre Solar BMS bms-sim-01** device is already there under Settings → Devices.
The first start takes a minute or two while Home Assistant initialises.

To watch the raw MQTT traffic:

```bash
docker compose exec mosquitto mosquitto_sub -t 'libre-solar/bms/#' -v
```

With `SIM_SPEED=60` (the compose default) a simulated day passes in 24
minutes, so you can watch the battery charge around noon and discharge at
night. The gateway publishes a `summary` every minute in the demo
(`SUMMARY_INTERVAL_S`, 15 minutes in the field) and immediately when health or
faults change.

Stop with `docker compose down` (add `-v` to also reset Home Assistant and the
broker). The simulator publishes `offline` before it exits; if it crashes
instead, the broker publishes `offline` itself (MQTT Last Will), and the
gateway turns that into a `device_offline` event.

### Demo scenarios

Pick a scenario from the **Simulator scenario** dropdown on the BMS device in
Home Assistant, or publish one over MQTT:

```bash
docker compose exec mosquitto mosquitto_pub -t libre-solar/bms/bms-sim-01/sim/scenario/set -m overheat
```

| Scenario | What happens |
|---|---|
| `normal` | Healthy battery on a solar day. Also resets the other scenarios |
| `overheat` | Enclosure heats to 52 °C: health **warning** near the limit first, then `CHG_OVERTEMP` + `DIS_OVERTEMP`, BMS `OFF`, current 0 A |
| `cold` | Freezing night (−5 °C): `CHG_UNDERTEMP`, the BMS only allows discharging |
| `cell_drift` | One cell drifts away: imbalance warning at 50 mV, critical at 100 mV (judged at 20–90 % SoC) |
| `overload` | Load above 1C: `DIS_OVERCURRENT`, output switched off until the load goes away |
| `deep_discharge` | Inverter cut-off fails: `CELL_UNDERVOLTAGE`, the BMS stops discharging until solar recharges the pack |
| `silent` | BMS stops sending data but stays connected: after `STALE_AFTER_S` the health shows "No data from the BMS" |

The simulator models what the firmware does: faults clear at the firmware's
reset thresholds (hysteresis), and current really stops in a direction the BMS
has switched off. The `sim/scenario` topics are for the simulator only and are
not part of the BMS contract.

### Real hardware

See [docs/hardware.md](docs/hardware.md). In short, run the relay next to the
BMS and point it at the broker:

```bash
python -m bms_gateway.relay --source serial:/dev/ttyUSB0 --device-id site-01 --broker <broker>
```

To try the hardware path without a board, `docker compose --profile thingset up --build -d`
adds a second battery that goes through an emulated UART, ThingSet with CRCs
and the relay.

### Gateway configuration

`python -m bms_gateway.gateway --help` lists every option. Each option can also
be set with an environment variable:

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | `localhost`, `1883` | broker |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | none | broker credentials |
| `SUMMARY_INTERVAL_S` | `900` | summary window for MPM |
| `STALE_AFTER_S` | `120` | report a BMS as silent after this long without data |
| `TIME_SCALE` | `1` | set equal to `SIM_SPEED` with the simulator; keep `1` for real hardware |
| `HA_DISCOVERY` | `true` | publish Home Assistant discovery |

The gateway finds batteries from their topics, so you don't configure anything
per battery. It keeps no database: after a restart it reads its last retained
`summary` back from the broker, so the cycle count survives and MPM is not sent
duplicate fault events.

### Without Docker

Requires Python 3.10+ and an MQTT broker on `localhost:1883`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m bms_gateway.simulator --speed 60 &
python -m bms_gateway.gateway --time-scale 60
pytest
```

### Repository layout

```text
schemas/v1/            JSON Schemas (source of truth) and one valid example per message
docs/mqtt-schema.md    The contract explained: topics, fields, units, error flags
bms_gateway/
  contract.py          Topics, error flags, BMS states, schema validation
  simulator.py         Simulated BMS
  gateway.py           MQTT service: one DeviceMonitor per battery
  monitor.py           summary/event logic (window, energy, cycles, faults)
  health.py            Health rules (ok / warning / critical with reasons)
  faults.py            Operator text and recommended action per firmware fault
  ha_discovery.py      Home Assistant device discovery
  ha_setup.py          Zero-click Home Assistant setup for the demo stack
  relay.py             Real BMS: ThingSet over serial/TCP/file -> MQTT
  thingset.py          ThingSet text-mode parser and encoder (with CRC)
  thingset_emulator.py BMS UART stand-in over TCP, for testing the relay path
tests/                 Unit and end-to-end tests (run in CI)
docs/hardware.md       Connecting a real BMS C1 (UART or USB), running the relay as a service
infra/                 Broker configuration, systemd unit for the relay
```

## Abstract and goal

The Libre Solar BMS is a capable open-source battery management system, but once
deployed in the field it is effectively a black box. Operators have no
straightforward, open way to see what a pack is doing — state of charge, cell
balance, temperature, fault history — or to adjust its configuration without
physically visiting the site. This pushes operators toward proprietary monitoring
hardware and recurring cloud subscriptions, or leaves them flying blind. Across
Sub-Saharan Africa, where site visits are costly and connectivity varies widely,
early detection of a failing cell, an over-temperature event, or an undersized
system can be the difference between a quick fix and a dead battery bank.

The goal is an open, deployable remote monitoring gateway that reads live data
from the Libre Solar BMS, publishes it to a standard MQTT layer, and makes it
visible in the tools operators and the community already use.

A crucial design point drives the whole architecture: the three consumer
platforms are different in kind, and the data must be shaped for each.

- **MicroPowerManager (MPM)** is a customer-and-asset management platform
  (Laravel + MySQL) — essentially a CRM/ERP for decentralised utilities. It is
  *not* a
  timeseries database and must not be treated as one. MPM should receive
  low-frequency, decision-relevant battery state as asset metadata and events,
  shown in the battery's asset view — the numbers an operator acts on, not a
  high-resolution trace.
- **Home Assistant and Prospect (A2EI)** are open-source, self-hostable
  monitoring platforms built to handle timeseries telemetry. They are the right
  home for live/historical curves and dashboards. Both are read-only consumers;
  neither controls the battery.

Because all three read from the same MQTT feed, the real deliverable is a clean,
documented MQTT topic + JSON schema. Get that right and each platform is just a
subscriber; MPM takes a downsampled/event slice, while Home Assistant and
Prospect take the full stream.

## Expected outcomes

A new repository being created and populated to provide:

- **BMS simulator (first step):** a script that publishes representative BMS JSON
  to MQTT against the agreed schema. This unblocks all software work and is the
  reference for the data contract.
- **Documented MQTT schema:** topic structure and JSON payloads, including Home
  Assistant auto-discovery, as the project's source of truth.
- **MPM integration:** an ingestion path that writes the low-frequency metrics
  into MPM, and a battery-health view in the asset UI (SoC, SoH/cycles,
  imbalance, max temperature, fault log, last-seen).
- **Home Assistant integration:** entities appearing automatically via MQTT
  auto-discovery.
- **One real transport path proven end-to-end:** either A1 (BMS publishes MQTT
  over WiFi) or A2 (BMS → UART → Cicada → cellular → broker), demonstrated
  against the shared real BMS — with the simulator as fallback.
- **Documentation:** setup and reproduction steps so the result is a
  field-deployable monitoring kit (target users: SLS Energy and other Libre Solar
  deployments).

## Required knowledge

### Stack

- **BMS simulator (first step):** a small script that publishes representative BMS
  data (JSON) to MQTT, standing in for real hardware. This is built and used
  before any board is touched.
- **BMS firmware:** Zephyr RTOS on the Libre Solar BMS (ESP32-C3 target) — the
  source of the battery data and the target of any (stretch) configuration
  commands.
- **Transport layer:** either MQTT-over-WiFi published directly from the BMS, or a
  UART stream relayed by an EnAccess Cicada 4G module (Mbed OS / STM32, SIM7600
  modem) over cellular.
- **Message broker:** an MQTT broker (Mosquitto) as the single hub; a documented
  topic + JSON schema is the project's source of truth.
- **Backend / integration:** a service that consumes MQTT and writes battery state
  into MPM, plus adapters that let Home Assistant and Prospect consume the same
  feed.
- **Frontend:** a battery-health view inside MPM's existing asset UI. (Home
  Assistant and Prospect generate their own dashboards from the feed — no custom
  frontend needed there.)

### Programming languages

- **Python or JavaScript / TypeScript** — the simulator, the backend ingestion
  service, and the platform adapters. This is the bulk of the core work and needs
  no embedded skills.
- **C / Zephyr** — BMS firmware: WiFi + MQTT client (path A1) or a framed UART
  export (path A2), plus the optional config handler.
- **PHP / Laravel 11 + Vue.js 2** — MPM's own stack, for the battery-health asset
  view and the ingestion endpoint on the MPM side (MySQL 8, PHP 8, Node 18).
- **C / Mbed OS** — Cicada relay firmware (path A2 only). Not code-compatible with
  the BMS Zephyr stack; the two communicate only over the defined UART contract.

### Helpful experiences

- MQTT: brokers, topic design, and Home Assistant's MQTT auto-discovery
  convention.
- Backend/API integration in Python or TypeScript.
- Docker (running Mosquitto, Home Assistant, MPM, and Prospect locally).
- Embedded C / an RTOS — for the firmware paths only (Zephyr especially; Mbed OS
  for the relay).
- Serial / UART protocol design (framing, checksums) — path A2 only.
- Basic battery/BMS literacy — enough to know what the telemetry means (mentoring
  available from the case giver).

## Person of contact supporting this challenge

- Joscha Winzer

## Getting started

- Join the OSEAS Discord server: <https://community.oseas.org/>
- Introduce yourself in the `#introductions` channel and join the relevant
  channels for this challenge.
- Read the documentation:
  - <https://enaccess.org/materials/battery-management-system/>
  - <https://github.com/LibreSolar/bms-firmware>
  - <https://github.com/LibreSolar/bms-c1>
  - <https://micropowermanager.io/get-started.html>
  - <https://github.com/EnAccess/micropowermanager/>
  - <https://github.com/home-assistant>
  - <https://www.home-assistant.io/installation/>
  - <https://github.com/zephyrproject-rtos/zephyr>
  - <https://github.com/eclipse-mosquitto/mosquitto>
  - <https://github.com/prospect-server>
