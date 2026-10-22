"""MQTT contract v1: topics, enums and schema validation.

The JSON Schemas in schemas/v1/ are the source of truth; this module only
loads them and holds the constants every producer and consumer shares.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_VERSION = 1
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "v1"
TOPIC_PREFIX = "libre-solar/bms"

# Bit order of BMS_ERR_* in include/bms/bms_common.h (LibreSolar/bms-firmware).
ERROR_FLAGS = (
    "CELL_UNDERVOLTAGE",
    "CELL_OVERVOLTAGE",
    "SHORT_CIRCUIT",
    "DIS_OVERCURRENT",
    "CHG_OVERCURRENT",
    "OPEN_WIRE",
    "DIS_UNDERTEMP",
    "DIS_OVERTEMP",
    "CHG_UNDERTEMP",
    "CHG_OVERTEMP",
    "INT_OVERTEMP",
    "CELL_FAILURE",
    "DIS_OFF",
    "CHG_OFF",
    "FET_OVERTEMP",
)

# enum bms_state in include/bms/bms.h.
BMS_STATES = ("OFF", "CHG", "DIS", "NORMAL", "SHUTDOWN")

MESSAGE_TYPES = ("meas", "info", "summary", "event")


def topic(device_id: str, kind: str) -> str:
    """Topic for one message kind: meas, info, status, summary or event."""
    return f"{TOPIC_PREFIX}/{device_id}/{kind}"


def decode_error_flags(flags: int) -> list[str]:
    return [name for bit, name in enumerate(ERROR_FLAGS) if flags & (1 << bit)]


def encode_error_flags(names: list[str]) -> int:
    return sum(1 << ERROR_FLAGS.index(name) for name in names)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=None)
def _registry() -> Registry:
    resources = []
    for kind in MESSAGE_TYPES:
        schema = json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())
    return Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())


def validate(kind: str, message: dict) -> None:
    """Raise jsonschema.ValidationError if `message` breaks the contract."""
    validator(kind).validate(message)
