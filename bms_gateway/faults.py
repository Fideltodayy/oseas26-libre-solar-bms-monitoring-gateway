"""Operator-facing text for every BMS_ERR_* flag.

`message` must fit in one SMS (160 characters, see event.schema.json);
`action` tells a technician what to check on site.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Fault:
    severity: str  # "warning" or "critical"
    message: str
    action: str


FAULTS = {
    "CELL_UNDERVOLTAGE": Fault(
        "critical",
        "Battery empty: a cell is below its minimum voltage. Discharging stopped by the BMS.",
        "Reduce loads and let the battery recharge. If it happens often, the battery or PV array is too small.",
    ),
    "CELL_OVERVOLTAGE": Fault(
        "critical",
        "A cell is above its maximum voltage. Charging stopped by the BMS.",
        "Check the charge controller voltage settings and that cell balancing works.",
    ),
    "SHORT_CIRCUIT": Fault(
        "critical",
        "Short circuit on the battery output. Discharging stopped by the BMS.",
        "Disconnect loads and inspect the output wiring before resetting the BMS.",
    ),
    "DIS_OVERCURRENT": Fault(
        "warning",
        "Load current above the discharge limit. Output switched off by the BMS.",
        "Find and remove the load causing the surge, then the BMS reconnects.",
    ),
    "CHG_OVERCURRENT": Fault(
        "warning",
        "Charge current above the limit. Charging switched off by the BMS.",
        "Check the charge controller current limit matches the battery.",
    ),
    "OPEN_WIRE": Fault(
        "critical",
        "A cell sense wire is disconnected. Cell readings are unreliable.",
        "Inspect the balancing harness and BMS connectors for loose or broken wires.",
    ),
    "DIS_UNDERTEMP": Fault(
        "warning",
        "Battery too cold to discharge. Output switched off by the BMS.",
        "Move or insulate the battery so it stays above its minimum discharge temperature.",
    ),
    "DIS_OVERTEMP": Fault(
        "critical",
        "Battery too hot to discharge. Output switched off by the BMS.",
        "Check ventilation and shade the enclosure. Output resumes when it cools down.",
    ),
    "CHG_UNDERTEMP": Fault(
        "warning",
        "Battery too cold to charge safely. Charging switched off by the BMS.",
        "Charging resumes when the battery warms up. Insulate the enclosure if this repeats.",
    ),
    "CHG_OVERTEMP": Fault(
        "critical",
        "Battery too hot to charge. Charging switched off by the BMS.",
        "Check ventilation and shade the enclosure. Charging resumes when it cools down.",
    ),
    "INT_OVERTEMP": Fault(
        "critical",
        "BMS electronics overheating.",
        "Check airflow around the BMS board and that it is not mounted near a heat source.",
    ),
    "CELL_FAILURE": Fault(
        "critical",
        "Large voltage difference between cells: a cell may be failing.",
        "Measure each cell voltage on site. Plan to replace the weak cell or module.",
    ),
    "DIS_OFF": Fault(
        "critical",
        "Discharge switch is off although it should be on.",
        "Possible BMS hardware fault. Power-cycle the BMS; if it persists, replace the board.",
    ),
    "CHG_OFF": Fault(
        "critical",
        "Charge switch is off although it should be on.",
        "Possible BMS hardware fault. Power-cycle the BMS; if it persists, replace the board.",
    ),
    "FET_OVERTEMP": Fault(
        "critical",
        "BMS power switches overheating.",
        "Reduce the load current and check the BMS heatsink and airflow.",
    ),
}
