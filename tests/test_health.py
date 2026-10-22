import pytest

from bms_gateway import contract
from bms_gateway.faults import FAULTS
from bms_gateway.health import Snapshot, assess, estimate_soh

CONF = {"sChgMaxTemp_degC": 45.0, "sDisMaxTemp_degC": 45.0}


def snap(**kw) -> Snapshot:
    base = dict(active_faults=[], max_temp_c=25.0, imbalance_mv=10.0, soc_pct=60.0, soh_pct=98.0)
    return Snapshot(**{**base, **kw})


def test_every_firmware_error_has_operator_text():
    assert tuple(FAULTS) == contract.ERROR_FLAGS
    for fault in FAULTS.values():
        assert fault.severity in ("warning", "critical")
        assert len(fault.message) <= 160 - len("Cleared: ")  # fits one SMS, also as a clear event


def test_healthy():
    assert assess(snap(), CONF) == ("ok", [])


@pytest.mark.parametrize(
    "kw, status, reason",
    [
        (dict(max_temp_c=41.0), "warning", "Temperature 41 °C, close to the 45 °C limit"),
        (dict(max_temp_c=45.5), "critical", "Temperature 46 °C at the 45 °C limit"),
        (dict(imbalance_mv=60.0), "warning", "Cell imbalance 60 mV: check balancing"),
        (dict(imbalance_mv=120.0), "critical", "Cell imbalance 120 mV: a cell may be failing"),
        (dict(soc_pct=8.0), "warning", "Battery nearly empty (8 %)"),
        (dict(soh_pct=75.0), "warning", "Estimated state of health 75 %"),
        (dict(soh_pct=65.0), "critical", "Estimated state of health 65 %: plan replacement"),
        (dict(offline=True), "warning", "BMS is offline"),
        (dict(stale_s=300.0), "warning", "No data from the BMS for 5 min"),
        (dict(active_faults=["DIS_OVERCURRENT"]), "warning", FAULTS["DIS_OVERCURRENT"].message),
        (dict(active_faults=["OPEN_WIRE"]), "critical", FAULTS["OPEN_WIRE"].message),
    ],
)
def test_rules(kw, status, reason):
    assert assess(snap(**kw), CONF) == (status, [reason])


def test_imbalance_ignored_at_ends_of_lfp_curve():
    assert assess(snap(imbalance_mv=120.0, soc_pct=95.0), CONF) == ("ok", [])


def test_temperature_limit_comes_from_bms_config():
    assert assess(snap(max_temp_c=41.0), {"sChgMaxTemp_degC": 55.0, "sDisMaxTemp_degC": 60.0}) == ("ok", [])


def test_worst_reason_first():
    status, reasons = assess(snap(max_temp_c=41.0, active_faults=["OPEN_WIRE"]), CONF)
    assert status == "critical"
    assert reasons[0] == FAULTS["OPEN_WIRE"].message


def test_soh_model():
    assert estimate_soh(0) == 100.0
    assert estimate_soh(4000) == pytest.approx(80.0)
    assert estimate_soh(1e9) == 0.0
