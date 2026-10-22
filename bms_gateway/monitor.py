"""Per-device state: turns the `meas` stream into `summary` and `event` messages.

Pure logic without MQTT, so it can be tested with plain dicts. The gateway
feeds it incoming messages and publishes whatever it returns.

The broker is the only storage: the gateway publishes `summary` retained, and
on restart it reads it back to restore the lifetime cycle count and the set of
active faults (so it does not re-announce faults MPM already knows about).
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from bms_gateway import contract, health
from bms_gateway.faults import FAULTS

# Gaps longer than this (e.g. the BMS was offline) are not integrated.
MAX_INTEGRATION_GAP_S = 60.0


@dataclass
class Outgoing:
    kind: str  # "summary" or "event"
    payload: dict
    qos: int = 1
    retain: bool = False


@dataclass
class Window:
    started: float
    min_soc_pct: float = 100.0
    max_imbalance_mv: float = 0.0
    max_temp_c: float = -273.0
    energy_charged_wh: float = 0.0
    energy_discharged_wh: float = 0.0
    samples: int = 0


def parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def max_temp(meas: dict) -> float:
    temps = list(meas["rCellTemps_degC"]) + [meas["rICTemp_degC"]]
    temps += [meas[k] for k in ("rMOSFETTemp_degC", "rShuntTemp_degC") if k in meas]
    return max(temps)


def imbalance_mv(meas: dict) -> float:
    return round((meas["rCellMaxVoltage_V"] - meas["rCellMinVoltage_V"]) * 1000.0, 1)


@dataclass
class DeviceMonitor:
    device_id: str
    window_s: float = 900.0
    stale_after_s: float = 120.0
    # Simulated seconds per real second; only for simulator demos with SIM_SPEED > 1.
    time_scale: float = 1.0
    clock: callable = time.time

    info: dict | None = None
    status: str | None = None
    last_meas: dict | None = None
    last_meas_at: float | None = None  # wall clock, for staleness
    active_faults: list[str] = field(default_factory=list)
    discharged_ah: float = 0.0
    restored_cycles: float = 0.0
    window: Window | None = None
    published: tuple | None = None  # (health status, faults) of the last summary
    stale: bool = False

    # --- inputs -----------------------------------------------------------

    def on_info(self, message: dict) -> list[Outgoing]:
        self.info = message
        return []

    def on_retained_summary(self, message: dict) -> None:
        """Restore state from the summary this gateway published before a restart."""
        if self.last_meas is not None:
            return  # our own summary echoing back
        self.restored_cycles = message["equivalent_cycles"]
        self.active_faults = list(message["active_faults"])
        self.published = (message["health"]["status"], tuple(self.active_faults))

    def on_status(self, payload: str) -> list[Outgoing]:
        previous, self.status = self.status, payload
        if previous is None or previous == payload:
            return []  # first (retained) value is the baseline, not a change
        if payload == "offline":
            out = [self._event("device_offline", None, "warning",
                               "BMS went offline: no connection to the monitoring gateway.",
                               "Check power and the network/cellular connection at the site.")]
        else:
            out = [self._event("device_online", None, "info", "BMS is back online.", "No action needed.")]
        return out + self._summary_if_changed()

    def on_meas(self, message: dict) -> list[Outgoing]:
        meas = message["meas"]
        now = self.clock()
        if self.window is None:
            self.window = Window(started=now)
        self._integrate(message)
        self.last_meas, self.last_meas_at, self.stale = message, now, False

        w = self.window
        w.samples += 1
        w.min_soc_pct = min(w.min_soc_pct, meas["rSOC_pct"])
        w.max_imbalance_mv = max(w.max_imbalance_mv, imbalance_mv(meas))
        w.max_temp_c = max(w.max_temp_c, max_temp(meas))

        out = []
        faults = contract.decode_error_flags(meas["rErrorFlags"])
        for name in faults:
            if name not in self.active_faults:
                f = FAULTS[name]
                out.append(self._event("fault_raised", name, f.severity, f.message, f.action))
        for name in self.active_faults:
            if name not in faults:
                out.append(self._event("fault_cleared", name, "info", f"Cleared: {FAULTS[name].message}",
                                       "No action needed."))
        self.active_faults = faults
        return out + self._summary_if_changed()

    def tick(self) -> list[Outgoing]:
        """Call periodically: closes the window and detects silent devices."""
        now = self.clock()
        out = []
        if self.last_meas_at is not None and not self.stale and now - self.last_meas_at > self.stale_after_s:
            self.stale = True
            out += self._summary_if_changed()
        if self.window is not None and self.window.samples and now - self.window.started >= self.window_s:
            out.append(self._summary())
            self.window = Window(started=now)
        return out

    # --- derived values -------------------------------------------------------

    @property
    def capacity_ah(self) -> float | None:
        return self.info["conf"]["sNominalCapacity_Ah"] if self.info else None

    @property
    def equivalent_cycles(self) -> float:
        if not self.capacity_ah:
            return self.restored_cycles
        return self.restored_cycles + self.discharged_ah / self.capacity_ah

    def assess(self) -> tuple[str, list[str]]:
        meas = self.last_meas["meas"]
        stale_s = self.clock() - self.last_meas_at if self.stale else None
        snap = health.Snapshot(
            active_faults=self.active_faults,
            max_temp_c=max_temp(meas),
            imbalance_mv=imbalance_mv(meas),
            soc_pct=meas["rSOC_pct"],
            soh_pct=self._soh(),
            offline=self.status == "offline",
            stale_s=stale_s,
        )
        return health.assess(snap, self.info["conf"] if self.info else {})

    def _soh(self) -> float | None:
        return round(health.estimate_soh(self.equivalent_cycles), 1) if self.capacity_ah else None

    # --- helpers --------------------------------------------------------------

    def _integrate(self, message: dict) -> None:
        if self.last_meas is None:
            return
        dt = parse_ts(message["ts"]) - parse_ts(self.last_meas["ts"])
        if not 0 < dt <= MAX_INTEGRATION_GAP_S:
            return
        dt_h = dt * self.time_scale / 3600.0
        meas = message["meas"]
        current = meas.get("rPackCurrent_A", 0.0)
        energy_wh = meas["rPackVoltage_V"] * current * dt_h
        if current > 0:
            self.window.energy_charged_wh += energy_wh
        else:
            self.window.energy_discharged_wh -= energy_wh
            self.discharged_ah -= current * dt_h

    def _summary_if_changed(self) -> list[Outgoing]:
        if self.last_meas is None:
            return []
        status, _ = self.assess()
        if (status, tuple(self.active_faults)) == self.published:
            return []
        return [self._summary()]

    def _summary(self) -> Outgoing:
        meas = self.last_meas["meas"]
        w = self.window
        status, reasons = self.assess()
        self.published = (status, tuple(self.active_faults))
        payload = {
            "schema_version": contract.SCHEMA_VERSION,
            "device_id": self.device_id,
            "ts": contract.utc_now(),
            "window_s": max(1, round(self.clock() - w.started)),
            "last_seen": self.last_meas["ts"],
            "bms_state": contract.BMS_STATES[meas["rBmsState"]],
            "soc_pct": meas["rSOC_pct"],
            "min_soc_pct": w.min_soc_pct,
            "pack_voltage_v": meas["rPackVoltage_V"],
            "cell_imbalance_mv": w.max_imbalance_mv,
            "max_temp_c": w.max_temp_c,
            "energy_charged_wh": round(w.energy_charged_wh, 1),
            "energy_discharged_wh": round(w.energy_discharged_wh, 1),
            "equivalent_cycles": round(self.equivalent_cycles, 2),
            "soh_pct": self._soh(),
            "active_faults": list(self.active_faults),
            "health": {"status": status, "reasons": reasons},
        }
        if payload["soh_pct"] is not None:
            payload["soh_method"] = "cycle_model"
        return Outgoing("summary", payload, qos=1, retain=True)

    def _event(self, type_: str, fault: str | None, severity: str, message: str, action: str) -> Outgoing:
        payload = {
            "schema_version": contract.SCHEMA_VERSION,
            "device_id": self.device_id,
            "ts": contract.utc_now(),
            "event_id": str(uuid.uuid4()),
            "type": type_,
            "fault": fault,
            "severity": severity,
            "message": message,
            "recommended_action": action,
        }
        return Outgoing("event", payload, qos=1, retain=False)
