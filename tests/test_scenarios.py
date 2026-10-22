"""Each simulator scenario, run through the gateway's monitor end to end."""

from datetime import datetime, timedelta, timezone

import pytest

from bms_gateway import contract
from bms_gateway.monitor import DeviceMonitor
from bms_gateway.simulator import SCENARIOS, BatteryModel, build_info, scenario_discovery

STEP_S = 300  # 5 simulated minutes per message, like SIM_SPEED=60 with a 5 s interval


class Run:
    """Feeds a model into a monitor and records what the gateway would publish."""

    def __init__(self, **model_kw):
        self.model = BatteryModel(seed=11, **model_kw)
        self.t = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.clock = 0.0
        self.monitor = DeviceMonitor("bms-sim-01", time_scale=1, clock=lambda: self.clock)
        self.monitor.on_info(build_info(self.model, "bms-sim-01"))
        self.events, self.summaries, self.states = [], [], []

    def advance(self, steps: int) -> None:
        for _ in range(steps):
            self.model.step(STEP_S)
            self.t += timedelta(seconds=STEP_S)
            self.clock += 5
            meas = self.model.meas()
            self.states.append(contract.BMS_STATES[meas["rBmsState"]])
            message = {"schema_version": 1, "device_id": "bms-sim-01", "ts": self.t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "seq": len(self.states), "meas": meas}
            contract.validate("meas", message)
            for out in self.monitor.on_meas(message):
                contract.validate(out.kind, out.payload)
                (self.events if out.kind == "event" else self.summaries).append(out.payload)

    def raised(self) -> list[str]:
        return [e["fault"] for e in self.events if e["type"] == "fault_raised"]

    def cleared(self) -> list[str]:
        return [e["fault"] for e in self.events if e["type"] == "fault_cleared"]

    def health(self) -> list[str]:
        return [s["health"]["status"] for s in self.summaries]


def test_normal_day_stays_healthy():
    run = Run()
    run.advance(24 * 12)  # one simulated day
    assert run.events == []
    assert set(run.health()) == {"ok"}
    assert set(run.states) == {"NORMAL"}


def test_overheat_warns_before_the_bms_cuts_off_then_recovers():
    run = Run(hour=12.0)
    run.advance(2)
    run.model.set_scenario("overheat")
    run.advance(24)
    assert run.health()[:3] == ["ok", "warning", "critical"]  # warning comes first
    assert {"CHG_OVERTEMP", "DIS_OVERTEMP"} <= set(run.raised())
    assert run.states[-1] == "OFF"
    assert run.model.current_a == 0.0  # BMS switches are open

    run.model.set_scenario("normal")
    run.advance(24)
    assert {"CHG_OVERTEMP", "DIS_OVERTEMP"} <= set(run.cleared())
    assert run.health()[-1] == "ok"
    assert run.states[-1] == "NORMAL"


def test_temperature_faults_use_hysteresis():
    run = Run(hour=12.0)
    run.model.set_scenario("overheat")
    run.advance(30)
    assert "CHG_OVERTEMP" in run.model.errors
    run.model.ambient_c = 42.0  # below the 45 °C limit but above the 40 °C reset
    run.advance(1)
    assert "CHG_OVERTEMP" in run.model.errors


def test_cold_blocks_charging_only():
    run = Run(hour=10.0)
    run.model.set_scenario("cold")
    run.advance(24)
    assert run.raised() == ["CHG_UNDERTEMP"]
    assert run.states[-1] == "DIS"
    assert run.model.current_a <= 0


def test_cell_drift_escalates_imbalance():
    run = Run(soc_pct=60.0, hour=20.0)
    run.model.set_scenario("cell_drift")
    run.advance(5 * 12)
    reasons = [r for s in run.summaries for r in s["health"]["reasons"]]
    assert any("check balancing" in r for r in reasons)
    assert any("a cell may be failing" in r for r in reasons)
    assert run.health()[-1] == "critical"


def test_overload_trips_output_without_flapping():
    run = Run(hour=20.0)
    run.model.set_scenario("overload")
    run.advance(6)
    assert run.raised() == ["DIS_OVERCURRENT"]  # raised once, not every message
    assert run.states[-1] == "CHG"
    run.model.set_scenario("normal")
    run.advance(1)
    assert run.cleared() == ["DIS_OVERCURRENT"]


def test_deep_discharge_raises_undervoltage_until_recharged():
    run = Run(hour=19.0)
    run.model.set_scenario("deep_discharge")
    run.advance(6)
    assert "CELL_UNDERVOLTAGE" in run.raised()
    assert run.states[-1] == "CHG"
    run.model.set_scenario("normal")
    run.advance(24 * 12)  # through the next sunny day
    assert "CELL_UNDERVOLTAGE" in run.cleared()


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError):
        BatteryModel().set_scenario("meltdown")


def test_scenario_select_shares_the_bms_device():
    topic, payload = scenario_discovery("bms-sim-01")
    assert topic == "homeassistant/device/libre_solar_bms_bms_sim_01_simulator/config"
    assert payload["dev"]["ids"] == ["libre_solar_bms_bms_sim_01"]
    assert payload["cmps"]["scenario"]["options"] == list(SCENARIOS)
