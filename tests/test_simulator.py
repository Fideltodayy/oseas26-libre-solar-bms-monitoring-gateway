import pytest

from bms_gateway import contract
from bms_gateway.simulator import BatteryModel, Limits, build_info, build_meas, ocv


def test_ocv_matches_firmware_table_ends():
    assert ocv(100) == pytest.approx(3.392)
    assert ocv(0) == pytest.approx(2.833)
    assert ocv(50) == pytest.approx(3.265)


def test_a_simulated_week_always_matches_the_contract():
    model = BatteryModel(seed=1)
    for seq in range(7 * 24 * 12):  # 5-minute steps
        model.step(300)
        contract.validate("meas", build_meas(model, "bms-sim-01", seq))
    contract.validate("info", build_info(model, "bms-sim-01"))


def test_charges_by_day_and_discharges_at_night():
    model = BatteryModel(seed=2, hour=11.5)
    model.step(60)
    assert model.current_a > 0
    model.hour = 22.0
    model.step(60)
    assert model.current_a < 0


def test_soc_follows_coulomb_counting():
    model = BatteryModel(seed=3, soc_pct=50.0, hour=0.0)
    model.step(3600)  # one night hour, 100 Ah pack: 1 A for 1 h = 1 %
    assert model.current_a < 0
    assert model.soc_pct == pytest.approx(50.0 + model.current_a)


def test_pack_voltage_is_sum_of_cells():
    meas = BatteryModel(seed=4).meas()
    assert meas["rPackVoltage_V"] == pytest.approx(sum(meas["rCellVoltages_V"]), abs=0.02)
    assert meas["rCellMinVoltage_V"] == min(meas["rCellVoltages_V"])
    assert len(meas["rCellVoltages_V"]) == 16


def test_overtemperature_raises_flags_and_stops_charging():
    model = BatteryModel(seed=5, ambient_c=50.0, hour=12.0)
    model.step(60)
    meas = model.meas()
    errors = contract.decode_error_flags(meas["rErrorFlags"])
    assert {"CHG_OVERTEMP", "DIS_OVERTEMP"} <= set(errors)
    assert contract.BMS_STATES[meas["rBmsState"]] == "OFF"


def test_undervoltage_only_blocks_discharging():
    model = BatteryModel(seed=6, soc_pct=0.0, limits=Limits(cell_uv=3.0))
    meas = model.meas()
    assert "CELL_UNDERVOLTAGE" in contract.decode_error_flags(meas["rErrorFlags"])
    assert contract.BMS_STATES[meas["rBmsState"]] == "CHG"
