"""Battery health rules: turn the latest state into ok / warning / critical.

Thresholds are taken from the BMS's own configuration (`info.conf`) where
possible, so the gateway warns *before* the BMS cuts off.
"""

from dataclasses import dataclass

from bms_gateway.faults import FAULTS

SEVERITY_ORDER = ("ok", "warning", "critical")

TEMP_WARNING_MARGIN_C = 5.0
IMBALANCE_WARNING_MV = 50.0
IMBALANCE_CRITICAL_MV = 100.0
# LFP cells drift apart at the ends of the curve even when healthy, so
# imbalance is only judged in the flat middle part.
IMBALANCE_SOC_RANGE = (20.0, 90.0)
LOW_SOC_PCT = 10.0
SOH_WARNING_PCT = 80.0
SOH_CRITICAL_PCT = 70.0

# Equivalent full cycles until an LFP pack is expected to reach 80 % SoH.
LFP_CYCLES_TO_80_PCT = 4000.0


@dataclass
class Snapshot:
    """What the rules look at. Built by the monitor from the latest `meas`."""

    active_faults: list[str]
    max_temp_c: float
    imbalance_mv: float
    soc_pct: float
    soh_pct: float | None = None
    offline: bool = False
    stale_s: float | None = None  # seconds since the last `meas`, if overdue


def estimate_soh(equivalent_cycles: float) -> float:
    """Linear cycle-ageing model for LFP: 100 % new, 80 % after LFP_CYCLES_TO_80_PCT."""
    return max(0.0, 100.0 - 20.0 * equivalent_cycles / LFP_CYCLES_TO_80_PCT)


def assess(snap: Snapshot, conf: dict) -> tuple[str, list[str]]:
    """Return (status, reasons). Reasons are plain language, worst first."""
    findings: list[tuple[str, str]] = []

    if snap.offline:
        findings.append(("warning", "BMS is offline"))
    elif snap.stale_s is not None:
        findings.append(("warning", f"No data from the BMS for {snap.stale_s / 60:.0f} min"))

    for name in snap.active_faults:
        fault = FAULTS[name]
        findings.append((fault.severity, fault.message))

    temp_limit = min(conf.get("sChgMaxTemp_degC", 45.0), conf.get("sDisMaxTemp_degC", 45.0))
    if snap.max_temp_c >= temp_limit:
        findings.append(("critical", f"Temperature {snap.max_temp_c:.0f} °C at the {temp_limit:.0f} °C limit"))
    elif snap.max_temp_c >= temp_limit - TEMP_WARNING_MARGIN_C:
        findings.append(("warning", f"Temperature {snap.max_temp_c:.0f} °C, close to the {temp_limit:.0f} °C limit"))

    low, high = IMBALANCE_SOC_RANGE
    if low <= snap.soc_pct <= high:
        if snap.imbalance_mv >= IMBALANCE_CRITICAL_MV:
            findings.append(("critical", f"Cell imbalance {snap.imbalance_mv:.0f} mV: a cell may be failing"))
        elif snap.imbalance_mv >= IMBALANCE_WARNING_MV:
            findings.append(("warning", f"Cell imbalance {snap.imbalance_mv:.0f} mV: check balancing"))

    if snap.soc_pct < LOW_SOC_PCT:
        findings.append(("warning", f"Battery nearly empty ({snap.soc_pct:.0f} %)"))

    if snap.soh_pct is not None:
        if snap.soh_pct < SOH_CRITICAL_PCT:
            findings.append(("critical", f"Estimated state of health {snap.soh_pct:.0f} %: plan replacement"))
        elif snap.soh_pct < SOH_WARNING_PCT:
            findings.append(("warning", f"Estimated state of health {snap.soh_pct:.0f} %"))

    if not findings:
        return "ok", []
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f[0]), reverse=True)
    reasons = list(dict.fromkeys(reason for _, reason in findings))
    return findings[0][0], reasons
