import copy
import json

import pytest
from jsonschema import ValidationError

from bms_gateway import contract

EXAMPLES = contract.SCHEMA_DIR / "examples"


def load_example(kind: str) -> dict:
    return json.loads((EXAMPLES / f"{kind}.json").read_text())


@pytest.mark.parametrize("kind", contract.MESSAGE_TYPES)
def test_examples_match_schema(kind):
    contract.validate(kind, load_example(kind))


def test_topics():
    assert contract.topic("bms-01", "meas") == "libre-solar/bms/bms-01/meas"
    assert contract.topic("bms-01", "status") == "libre-solar/bms/bms-01/status"


def test_error_flags_follow_firmware_bit_order():
    # BMS_ERR_CELL_UNDERVOLTAGE = BIT(0), BMS_ERR_CHG_OVERTEMP = BIT(9), BMS_ERR_FET_OVERTEMP = BIT(14)
    assert contract.decode_error_flags(1 << 0) == ["CELL_UNDERVOLTAGE"]
    assert contract.decode_error_flags(1 << 9) == ["CHG_OVERTEMP"]
    assert contract.decode_error_flags(1 << 14) == ["FET_OVERTEMP"]
    assert contract.decode_error_flags(0) == []


def test_error_flags_round_trip():
    names = ["CELL_OVERVOLTAGE", "DIS_OVERTEMP", "CHG_OFF"]
    assert contract.decode_error_flags(contract.encode_error_flags(names)) == names


def test_fault_enum_matches_error_flags():
    schema = json.loads((contract.SCHEMA_DIR / "event.schema.json").read_text())
    assert tuple(schema["$defs"]["fault"]["enum"]) == contract.ERROR_FLAGS


@pytest.mark.parametrize(
    "kind, mutate",
    [
        ("meas", lambda m: m["meas"].pop("rSOC_pct")),
        ("meas", lambda m: m["meas"].update(rSOC_pct=101)),
        ("meas", lambda m: m["meas"].update(rBmsState=7)),
        ("meas", lambda m: m.update(schema_version=2)),
        ("meas", lambda m: m.update(device_id="has spaces")),
        ("meas", lambda m: m.update(ts="yesterday")),
        ("info", lambda m: m.update(source="cloud")),
        ("summary", lambda m: m.update(active_faults=["NOT_A_FAULT"])),
        ("summary", lambda m: m["health"].update(status="fine")),
        ("event", lambda m: m.update(event_id="not-a-uuid")),
        ("event", lambda m: m.update(message="x" * 161)),
    ],
)
def test_invalid_messages_are_rejected(kind, mutate):
    message = copy.deepcopy(load_example(kind))
    mutate(message)
    with pytest.raises(ValidationError):
        contract.validate(kind, message)


def test_device_events_have_null_fault():
    event = load_example("event")
    event.update(type="device_offline", fault=None, severity="warning")
    contract.validate("event", event)
