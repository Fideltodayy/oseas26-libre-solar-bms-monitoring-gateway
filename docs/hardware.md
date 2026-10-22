# Connecting a real Libre Solar BMS

The relay (`bms_gateway/relay.py`) reads the BMS over its ThingSet serial
interface and publishes the same MQTT contract as the simulator. The gateway,
Home Assistant and MPM don't need any changes, and don't know the difference.

```text
BMS C1 ──UART (ThingSet text)──► relay on a Raspberry Pi / laptop ──MQTT──► broker ──► gateway, HA, MPM
```

> **Status:** the relay has been tested against the ThingSet emulator over TCP
> (with CRCs) and against firmware output samples. It has **not yet been tested
> on a physical BMS C1**. That is the plan for the shared BMS at the hackathon.
> The facts below come from the firmware source and the BMS C1 manual.

## Which port to use

The BMS C1 has two serial interfaces that speak ThingSet
([`boards/libresolar/bms_c1/bms_c1.dts`](https://github.com/LibreSolar/bms-firmware/blob/main/boards/libresolar/bms_c1/bms_c1.dts)):

| Port | ThingSet live reports | How to run the relay |
|---|---|---|
| **UART** (`uart0`, JST PH connector, pinout on the PCB; 115200 baud) | ✅ sent automatically (`#mLive`) | `--source serial:/dev/ttyUSB0` |
| **USB** (ESP32-C3 USB serial, `/dev/ttyACM0`) | ❌ off (`CONFIG_THINGSET_SHELL_REPORTING=n`). USB is the Zephyr shell | `--source serial:/dev/ttyACM0 --usb-shell --poll 5` |

**Use the UART if you can.** It needs a **3.3 V** USB-to-UART adapter
(CP2102, FT232 or similar, set to 3.3 V, not 5 V). Connect adapter RX to BMS TX
(GPIO21), adapter TX to BMS RX (GPIO20), and GND to GND.

USB needs no extra hardware. The relay switches the shell to ThingSet mode
(`select thingset`) and polls `?Meas`.

## Run the relay

On the machine connected to the BMS (Python 3.10+):

```bash
git clone <this repository> /opt/bms-gateway && cd /opt/bms-gateway
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/python -m bms_gateway.relay \
    --source serial:/dev/ttyUSB0 --device-id site-01 --broker <broker address>
```

The device id becomes part of every topic (`libre-solar/bms/site-01/...`), so
choose one per battery and keep it stable. The user running the relay needs
access to the serial port: `sudo usermod -aG dialout $USER`.

To run it as a service that starts on boot and restarts on failure, use
[`infra/systemd/bms-relay.service`](../infra/systemd/bms-relay.service).

Run the gateway (and Home Assistant, MPM) wherever the broker is. On real
hardware keep `TIME_SCALE=1` (the default).

## What the relay does

| ThingSet from the BMS | MQTT published |
|---|---|
| `#mLive {"Meas":{…}}` (sent every few seconds), or `:85 {…}` reply to `?Meas` in poll mode | `meas`. The `Meas` group goes in unchanged |
| `:85 {…}` replies to `?Device` and `?Conf` (requested on connect, retried every 30 s until answered) | `info`, retained, with `"source": "serial"` |
| connection opened / lost | `status` `online` / `offline`; MQTT Last Will if the relay itself dies |

It also handles the following:

- **CRC.** Lines ending in ` XXXXXXXX#` (`CONFIG_THINGSET_SERIAL_USE_CRC`) are
  checked with CRC-32, and corrupted lines are dropped.
- **Startup values.** The firmware sends `null` for values it has not
  measured yet (NaN), e.g. `rCellAvgVoltage_V` right after boot. Reports are
  held back until all required fields are numbers, so no half-valid data
  reaches MPM.
- **Older firmware.** The single `rBatTemp_degC` value is mapped to
  `rCellTemps_degC: [value]`.
- **Shell noise.** Log lines, prompts, colour codes and command echoes are
  ignored.
- **Reconnects.** If the serial port or TCP connection drops, the relay
  publishes `offline` and reconnects every few seconds.

## Without a board

The ThingSet emulator serves the battery model over TCP exactly like the
UART, so you can try the whole path:

```bash
docker compose --profile thingset up --build -d
```

This adds a second battery, `bms-ts-01`, which goes emulated UART → ThingSet
(with CRCs) → relay → MQTT. It appears in Home Assistant next to the plain
simulator. Its `info` says `"source": "simulator"`.

To replay a capture from a real BMS (e.g. saved with `picocom --logfile`):

```bash
python -m bms_gateway.relay --source file:capture.txt --replay-interval 1
```

## Path A2: Cicada 4G relay

A Cicada (STM32 + SIM7600) relay would read the same UART and publish the same
topics over cellular. The table above is the full specification it has to
implement. Its firmware isn't part of this repository yet.
