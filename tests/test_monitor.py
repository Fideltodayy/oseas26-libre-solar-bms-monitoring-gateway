import json

import pytest

from bms_gateway import contract
from bms_gateway.monitor import DeviceMonitor

EXAMPLES = contract.SCHEMA_DIR / "examples"


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def info():
    return json.loads((EXAMPLES / "info.json").read_text())


def meas(seconds: int, flags: list[str] | None = None, current: float = -10.0, soc: float = 60.0,
         temp: float = 25.0, cell_min: float = 3.27, cell_max: float = 3.28) -> dict:
    """A meas message `seconds` after a fixed start time."""
    message = json.loads((EXAMPLES / "meas.json").read_text())
    m = message["meas"]
    m.update(rPackCurrent_A=current, rPackVoltage_V=50.0, rSOC_pct=soc, rCellTemps_degC=[temp, temp],
             rICTemp_degC=temp, rMOSFETTemp_degC=temp, rCellMinVoltage_V=cell_min, rCellMaxVoltage_V=cell_max,
             rErrorFlags=contract.encode_error_flags(flags or []))
    hh, rest = divmod(seconds, 3600)
    mm, ss = divmod(rest, 60)
    message["ts"] = f"2026-10-22T{10 + hh:02d}:{mm:02d}:{ss:02d}Z"
    return message


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def mon(clock):
    m = DeviceMonitor("bms-sim-01", window_s=900, stale_after_s=120, clock=clock)
    m.on_info(info())
    return m


def kinds(out):
    return [(o.kind, o.payload.get("type")) for o in out]


def validate_all(out):
    for o in out:
        contract.validate(o.kind, o.payload)


def test_first_meas_publishes_summary_then_quiet(mon):
    out = mon.on_meas(meas(0))
    assert kinds(out) == [("summary", None)]
    assert out[0].retain
    assert out[0].payload["health"] == {"status": "ok", "reasons": []}
    validate_all(out)
    assert mon.on_meas(meas(5)) == []


def test_fault_raised_and_cleared_events(mon):
    mon.on_meas(meas(0))
    out = mon.on_meas(meas(5, flags=["CHG_OVERTEMP"], temp=46.0))
    validate_all(out)
    assert kinds(out) == [("event", "fault_raised"), ("summary", None)]
    event, summary = out[0].payload, out[1].payload
    assert event["fault"] == "CHG_OVERTEMP" and event["severity"] == "critical"
    assert not out[0].retain
    assert summary["active_faults"] == ["CHG_OVERTEMP"]
    assert summary["health"]["status"] == "critical"

    assert mon.on_meas(meas(10, flags=["CHG_OVERTEMP"], temp=46.0)) == []  # no repeat while active

    out = mon.on_meas(meas(15))
    validate_all(out)
    assert kinds(out) == [("event", "fault_cleared"), ("summary", None)]
    assert out[1].payload["health"]["status"] == "ok"


def test_event_ids_are_unique(mon):
    mon.on_meas(meas(0))
    a = mon.on_meas(meas(5, flags=["DIS_OVERCURRENT"]))[0].payload["event_id"]
    b = mon.on_meas(meas(10))[0].payload["event_id"]
    assert a != b


def test_window_summary_aggregates(mon, clock):
    mon.on_meas(meas(0, current=-10.0, soc=60.0))
    clock.now += 450
    mon.on_meas(meas(450, current=-10.0, soc=55.0, cell_min=3.25, cell_max=3.30, temp=31.0))
    clock.now += 450
    out = mon.tick()
    assert kinds(out) == [("summary", None)]
    s = out[0].payload
    validate_all(out)
    assert s["window_s"] == 900
    assert s["min_soc_pct"] == 55.0
    assert s["cell_imbalance_mv"] == 50.0
    assert s["max_temp_c"] == 31.0
    assert mon.tick() == []  # new window has no samples yet


def test_energy_and_cycles_from_coulomb_counting(mon):
    mon.on_meas(meas(0, current=-50.0))
    for t in range(30, 3601, 30):  # one hour at 50 A, 50 V
        mon.on_meas(meas(t, current=-50.0))
    assert mon.window.energy_discharged_wh == pytest.approx(2500.0)
    assert mon.equivalent_cycles == pytest.approx(0.5)  # 50 Ah of a 100 Ah pack


def test_gaps_are_not_integrated(mon):
    mon.on_meas(meas(0, current=-50.0))
    mon.on_meas(meas(3600, current=-50.0))  # BMS was silent for an hour
    assert mon.discharged_ah == 0.0


def test_time_scale_for_fast_simulation(clock):
    m = DeviceMonitor("bms-sim-01", time_scale=60, clock=clock)
    m.on_info(info())
    m.on_meas(meas(0, current=-100.0))
    m.on_meas(meas(60, current=-100.0))  # 1 real minute = 1 simulated hour
    assert m.equivalent_cycles == pytest.approx(1.0)


def test_status_baseline_then_offline_and_online_events(mon):
    assert mon.on_status("online") == []  # retained baseline
    mon.on_meas(meas(0))
    out = mon.on_status("offline")
    validate_all(out)
    assert kinds(out) == [("event", "device_offline"), ("summary", None)]
    assert out[0].payload["fault"] is None
    assert "BMS is offline" in out[1].payload["health"]["reasons"]
    out = mon.on_status("online")
    assert kinds(out) == [("event", "device_online"), ("summary", None)]


def test_silent_device_reported_once(mon, clock):
    mon.on_meas(meas(0))
    clock.now += 121
    out = mon.tick()
    assert kinds(out) == [("summary", None)]
    assert out[0].payload["health"]["status"] == "warning"
    assert out[0].payload["health"]["reasons"] == ["No data from the BMS for 2 min"]
    clock.now += 10
    assert mon.tick() == []
    out = mon.on_meas(meas(200))
    assert out[0].payload["health"]["status"] == "ok"


def test_restart_restores_cycles_and_does_not_repeat_faults(clock):
    previous = json.loads((EXAMPLES / "summary.json").read_text())
    previous["active_faults"] = ["CHG_OVERTEMP"]
    previous["health"] = {"status": "critical", "reasons": ["Battery too hot to charge."]}

    m = DeviceMonitor("bms-sim-01", clock=clock)
    m.on_info(info())
    m.on_retained_summary(previous)
    out = m.on_meas(meas(0, flags=["CHG_OVERTEMP"], temp=46.0))
    assert kinds(out) == []  # MPM already knows about this fault
    assert m.equivalent_cycles == pytest.approx(142.6)
    assert m._soh() == pytest.approx(99.3)


def test_own_summary_echo_is_ignored(mon):
    out = mon.on_meas(meas(0, current=-50.0))
    mon.on_meas(meas(30, current=-50.0))
    cycles = mon.equivalent_cycles
    mon.on_retained_summary(out[0].payload)
    assert mon.equivalent_cycles == cycles


def test_without_info_soh_is_unknown(clock):
    m = DeviceMonitor("bms-sim-01", clock=clock)
    out = m.on_meas(meas(0))
    validate_all(out)
    assert out[0].payload["soh_pct"] is None
    assert "soh_method" not in out[0].payload
