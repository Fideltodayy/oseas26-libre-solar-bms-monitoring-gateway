"""Libre Solar BMS simulator: publishes contract v1 messages to MQTT.

The battery model uses the firmware's own LFP defaults (OCV curve and
protection limits from app/src/bms_common.c) and follows a compressed solar
day: charging around noon, discharging at night. It is a stand-in for real
hardware, so every `info` message says `"source": "simulator"`.

    python -m bms_gateway.simulator --broker localhost --speed 60
"""

import argparse
import json
import math
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from bms_gateway import __version__, contract

# Firmware LFP OCV table (bms_common.c), SoC from 100 % down to 0 % in 5 % steps.
OCV_LFP = (
    3.392, 3.314, 3.309, 3.308, 3.304, 3.296, 3.283, 3.275, 3.271, 3.268, 3.265,
    3.264, 3.262, 3.252, 3.240, 3.226, 3.213, 3.190, 3.177, 3.132, 2.833,
)


def ocv(soc_pct: float) -> float:
    pos = (100.0 - min(max(soc_pct, 0.0), 100.0)) / 5.0
    i = min(int(pos), len(OCV_LFP) - 2)
    frac = pos - i
    return OCV_LFP[i] + (OCV_LFP[i + 1] - OCV_LFP[i]) * frac


@dataclass
class Limits:
    """Firmware defaults from bms_init_config() for CELL_TYPE_LFP."""

    capacity_ah: float = 100.0
    cell_ov: float = 3.80
    cell_ov_reset: float = 3.40
    cell_uv: float = 2.50
    cell_uv_reset: float = 3.10
    dis_max_temp: float = 45.0
    dis_min_temp: float = -20.0
    chg_max_temp: float = 45.0
    chg_min_temp: float = 0.0
    temp_hyst: float = 5.0
    bal_target_diff: float = 0.01
    bal_min_voltage: float = 3.30

    @property
    def dis_overcurrent(self) -> float:
        return self.capacity_ah  # 1C

    @property
    def chg_overcurrent(self) -> float:
        return self.capacity_ah  # 1C

    def to_conf(self) -> dict:
        return {
            "sNominalCapacity_Ah": self.capacity_ah,
            "sShortCircuitLimit_A": self.dis_overcurrent * 2,
            "sDisOvercurrent_A": self.dis_overcurrent,
            "sChgOvercurrent_A": self.chg_overcurrent,
            "sDisMaxTemp_degC": self.dis_max_temp,
            "sDisMinTemp_degC": self.dis_min_temp,
            "sChgMaxTemp_degC": self.chg_max_temp,
            "sChgMinTemp_degC": self.chg_min_temp,
            "sTempLimitHysteresis_degC": self.temp_hyst,
            "sCellOvervoltage_V": self.cell_ov,
            "sCellOvervoltageReset_V": self.cell_ov_reset,
            "sCellUndervoltage_V": self.cell_uv,
            "sCellUndervoltageReset_V": self.cell_uv_reset,
            "sBalTargetVoltageDiff_V": self.bal_target_diff,
            "sBalMinVoltage_V": self.bal_min_voltage,
        }


@dataclass
class BatteryModel:
    cells: int = 16
    limits: Limits = field(default_factory=Limits)
    soc_pct: float = 80.0
    ambient_c: float = 25.0
    hour: float = 8.0  # simulated time of day
    seed: int | None = None
    cell_resistance_ohm: float = 0.0008
    current_a: float = 0.0

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        # Manufacturing spread of a few mV, plus one slightly weak cell.
        self.cell_offsets = [self.rng.uniform(-0.004, 0.004) for _ in range(self.cells)]
        self.cell_offsets[self.rng.randrange(self.cells)] -= 0.008

    def _demand_current(self) -> float:
        """Solar charging between 06:00 and 18:00 minus the household load."""
        solar = 0.0
        if 6.0 <= self.hour <= 18.0:
            solar = 0.35 * self.limits.capacity_ah * math.sin(math.pi * (self.hour - 6.0) / 12.0)
        load = 0.08 * self.limits.capacity_ah + self.rng.uniform(-1.5, 1.5)
        return solar - load

    def step(self, dt_s: float) -> None:
        self.hour = (self.hour + dt_s / 3600.0) % 24.0
        current = self._demand_current()
        # Charge taper near full, load cut-off near empty (as the inverter/charger would do).
        if current > 0 and self.soc_pct >= 99.0:
            current *= max(0.0, (100.0 - self.soc_pct))
        if current < 0 and self.soc_pct <= 5.0:
            current = 0.0
        self.current_a = current
        self.soc_pct += current * dt_s / 3600.0 / self.limits.capacity_ah * 100.0
        self.soc_pct = min(max(self.soc_pct, 0.0), 100.0)

    def cell_voltages(self) -> list[float]:
        base = ocv(self.soc_pct) + self.current_a * self.cell_resistance_ohm
        return [base + off + self.rng.gauss(0, 0.0005) for off in self.cell_offsets]

    def temperatures(self) -> tuple[list[float], float, float]:
        heat = abs(self.current_a)
        cell_temps = [self.ambient_c + 0.06 * heat + self.rng.gauss(0, 0.1) for _ in range(2)]
        ic_temp = self.ambient_c + 4.0 + self.rng.gauss(0, 0.1)
        mosfet_temp = self.ambient_c + 0.15 * heat + self.rng.gauss(0, 0.1)
        return cell_temps, ic_temp, mosfet_temp

    def error_flags(self, cells: list[float], cell_temps: list[float], mosfet_temp: float) -> int:
        lim = self.limits
        errors = []
        if max(cells) > lim.cell_ov:
            errors.append("CELL_OVERVOLTAGE")
        if min(cells) < lim.cell_uv:
            errors.append("CELL_UNDERVOLTAGE")
        if self.current_a > lim.chg_overcurrent:
            errors.append("CHG_OVERCURRENT")
        if -self.current_a > lim.dis_overcurrent:
            errors.append("DIS_OVERCURRENT")
        if max(cell_temps) > lim.dis_max_temp:
            errors.append("DIS_OVERTEMP")
        if min(cell_temps) < lim.dis_min_temp:
            errors.append("DIS_UNDERTEMP")
        if max(cell_temps) > lim.chg_max_temp:
            errors.append("CHG_OVERTEMP")
        if min(cell_temps) < lim.chg_min_temp:
            errors.append("CHG_UNDERTEMP")
        if mosfet_temp > 80.0:
            errors.append("FET_OVERTEMP")
        return contract.encode_error_flags(errors)

    @staticmethod
    def bms_state(flags: int) -> int:
        """Steady state of the firmware state machine for the given errors."""
        errors = set(contract.decode_error_flags(flags))
        chg_ok = not errors & {"CELL_OVERVOLTAGE", "CHG_OVERCURRENT", "CHG_OVERTEMP", "CHG_UNDERTEMP"}
        dis_ok = not errors & {
            "CELL_UNDERVOLTAGE", "DIS_OVERCURRENT", "SHORT_CIRCUIT", "DIS_OVERTEMP", "DIS_UNDERTEMP",
        }
        name = {(True, True): "NORMAL", (True, False): "CHG", (False, True): "DIS"}.get((chg_ok, dis_ok), "OFF")
        return contract.BMS_STATES.index(name)

    def balancing_status(self, cells: list[float]) -> int:
        """Simplified: balance cells above the lowest one while charging."""
        if self.current_a <= 0:
            return 0
        low = min(cells)
        lim = self.limits
        return sum(
            1 << i for i, v in enumerate(cells) if v > lim.bal_min_voltage and v - low > lim.bal_target_diff
        )

    def meas(self) -> dict:
        """The ThingSet `Meas` group, rounded like the firmware does."""
        cells = self.cell_voltages()
        cell_temps, ic_temp, mosfet_temp = self.temperatures()
        flags = self.error_flags(cells, cell_temps, mosfet_temp)
        pack_v = sum(cells)
        return {
            "rPackVoltage_V": round(pack_v, 2),
            "rStackVoltage_V": round(pack_v, 2),
            "rPackCurrent_A": round(self.current_a, 2),
            "rCellTemps_degC": [round(t, 1) for t in cell_temps],
            "rICTemp_degC": round(ic_temp, 1),
            "rMOSFETTemp_degC": round(mosfet_temp, 1),
            "rSOC_pct": round(self.soc_pct, 1),
            "rErrorFlags": flags,
            "rBmsState": self.bms_state(flags),
            "rCellVoltages_V": [round(v, 3) for v in cells],
            "rCellAvgVoltage_V": round(pack_v / len(cells), 3),
            "rCellMinVoltage_V": round(min(cells), 3),
            "rCellMaxVoltage_V": round(max(cells), 3),
            "rBalancingStatus": self.balancing_status(cells),
        }


def build_meas(model: BatteryModel, device_id: str, seq: int) -> dict:
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "device_id": device_id,
        "ts": contract.utc_now(),
        "seq": seq,
        "meas": model.meas(),
    }


def build_info(model: BatteryModel, device_id: str) -> dict:
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "device_id": device_id,
        "ts": contract.utc_now(),
        "source": "simulator",
        "device": {
            "cManufacturer": "Libre Solar",
            "cDeviceType": "BMS C1 (simulated)",
            "cHardwareVersion": "v0.4",
            "cFirmwareVersion": f"bms-gateway-sim {__version__}",
        },
        "conf": model.limits.to_conf(),
    }


def _publish(client: mqtt.Client, kind: str, device_id: str, message: dict, qos: int, retain: bool) -> None:
    contract.validate(kind, message)
    client.publish(contract.topic(device_id, kind), json.dumps(message), qos=qos, retain=retain)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--broker", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--device-id", default=os.getenv("DEVICE_ID", "bms-sim-01"))
    parser.add_argument("--interval", type=float, default=float(os.getenv("PUBLISH_INTERVAL_S", "5")),
                        help="seconds between meas messages")
    parser.add_argument("--speed", type=float, default=float(os.getenv("SIM_SPEED", "1")),
                        help="simulated seconds per real second, e.g. 60 to see a day pass in 24 min")
    parser.add_argument("--cells", type=int, default=int(os.getenv("SIM_CELLS", "16")))
    parser.add_argument("--capacity", type=float, default=float(os.getenv("SIM_CAPACITY_AH", "100")))
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    model = BatteryModel(cells=args.cells, limits=Limits(capacity_ah=args.capacity), seed=args.seed)
    status_topic = contract.topic(args.device_id, "status")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{args.device_id}")
    client.will_set(status_topic, "offline", qos=1, retain=True)
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"Connected to {args.broker}:{args.port} ({reason_code})", flush=True)
        client.publish(status_topic, "online", qos=1, retain=True)
        _publish(client, "info", args.device_id, build_info(model, args.device_id), qos=1, retain=True)
        connected.set()

    # Treat `docker stop` like Ctrl+C so the clean "offline" status still goes out.
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    client.on_connect = on_connect
    client.connect(args.broker, args.port, keepalive=30)
    client.loop_start()
    if not connected.wait(timeout=10):
        sys.exit(f"Could not connect to MQTT broker at {args.broker}:{args.port}")

    print(f"Publishing to {contract.topic(args.device_id, 'meas')} every {args.interval}s. Ctrl+C to stop.")
    seq = 0
    try:
        while True:
            model.step(args.interval * args.speed)
            message = build_meas(model, args.device_id, seq)
            _publish(client, "meas", args.device_id, message, qos=0, retain=False)
            m = message["meas"]
            print(
                f"[{seq}] {model.hour:05.2f}h  {m['rPackVoltage_V']:.2f} V  {m['rPackCurrent_A']:+.2f} A  "
                f"SoC {m['rSOC_pct']:.1f} %  {contract.BMS_STATES[m['rBmsState']]}  "
                f"errors {contract.decode_error_flags(m['rErrorFlags']) or '-'}",
                flush=True,
            )
            seq += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(status_topic, "offline", qos=1, retain=True).wait_for_publish(timeout=2)
        client.loop_stop()
        client.disconnect()
        print("\nSimulator stopped.")


if __name__ == "__main__":
    main()
