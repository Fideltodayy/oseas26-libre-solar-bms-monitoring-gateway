"""Home Assistant MQTT discovery for a BMS.

Uses device-based discovery: one retained message on
homeassistant/device/<node_id>/config describes the device and all its
entities. Live values come straight from `meas`; health, SoH and faults come
from the gateway's `summary`.
"""

from bms_gateway import __version__, contract

DISCOVERY_PREFIX = "homeassistant"
REPO_URL = "https://github.com/EnAccess/oseas26-libre-solar-bms-monitoring-gateway"


def node_id(device_id: str) -> str:
    return "libre_solar_bms_" + device_id.replace("-", "_")


def discovery_topic(device_id: str) -> str:
    return f"{DISCOVERY_PREFIX}/device/{node_id(device_id)}/config"


def _sensor(uid: str, name: str, topic: str, template: str, unit: str | None = None,
            device_class: str | None = None, state_class: str | None = "measurement",
            precision: int | None = None, diagnostic: bool = False, **extra) -> dict:
    cmp = {"p": "sensor", "uniq_id": uid, "name": name, "stat_t": topic, "val_tpl": template}
    if unit:
        cmp["unit_of_meas"] = unit
    if device_class:
        cmp["dev_cla"] = device_class
    if state_class:
        cmp["stat_cla"] = state_class
    if precision is not None:
        cmp["sug_dsp_prc"] = precision
    if diagnostic:
        cmp["ent_cat"] = "diagnostic"
    cmp.update(extra)
    return cmp


def build(device_id: str, info: dict, meas: dict) -> dict:
    """Discovery payload. `meas` is a sample, used for cell count and optional fields."""
    node = node_id(device_id)
    t_meas = contract.topic(device_id, "meas")
    t_sum = contract.topic(device_id, "summary")
    m = "value_json.meas."

    def uid(key: str) -> str:
        return f"{node}_{key}"

    cmps = {
        "soc": _sensor(uid("soc"), "State of charge", t_meas, "{{ %srSOC_pct }}" % m, "%", "battery"),
        "pack_voltage": _sensor(uid("pack_voltage"), "Pack voltage", t_meas, "{{ %srPackVoltage_V }}" % m,
                                "V", "voltage", precision=2),
        "cell_min": _sensor(uid("cell_min"), "Lowest cell voltage", t_meas, "{{ %srCellMinVoltage_V }}" % m,
                            "V", "voltage", precision=3),
        "cell_max": _sensor(uid("cell_max"), "Highest cell voltage", t_meas, "{{ %srCellMaxVoltage_V }}" % m,
                            "V", "voltage", precision=3),
        "imbalance": _sensor(uid("imbalance"), "Cell imbalance", t_meas,
                             "{{ ((%srCellMaxVoltage_V - %srCellMinVoltage_V) * 1000) | round(0) }}" % (m, m),
                             "mV", "voltage", precision=0),
        "ic_temp": _sensor(uid("ic_temp"), "BMS IC temperature", t_meas, "{{ %srICTemp_degC }}" % m,
                           "°C", "temperature", diagnostic=True),
        "bms_state": _sensor(uid("bms_state"), "BMS state", t_meas,
                             "{{ %s[%srBmsState] }}" % (list(contract.BMS_STATES), m),
                             device_class="enum", state_class=None, options=list(contract.BMS_STATES)),
        "problem": {
            "p": "binary_sensor", "uniq_id": uid("problem"), "name": "Problem", "stat_t": t_meas,
            "val_tpl": "{{ 'ON' if %srErrorFlags > 0 else 'OFF' }}" % m, "dev_cla": "problem",
        },
        "health": _sensor(uid("health"), "Health", t_sum, "{{ value_json.health.status }}",
                          device_class="enum", state_class=None, options=["ok", "warning", "critical"],
                          json_attr_t=t_sum, json_attr_tpl="{{ {'reasons': value_json.health.reasons} | tojson }}"),
        "health_reasons": _sensor(uid("health_reasons"), "Health reasons", t_sum,
                                  "{{ (value_json.health.reasons | join('; ') or 'none')[:255] }}",
                                  state_class=None),
        "active_faults": _sensor(uid("active_faults"), "Active faults", t_sum,
                                 "{{ value_json.active_faults | join(', ') or 'none' }}", state_class=None),
        # A null soh_pct renders as "None", which HA shows as unknown.
        "soh": _sensor(uid("soh"), "Estimated state of health", t_sum, "{{ value_json.soh_pct }}", "%",
                       precision=1),
        "cycles": _sensor(uid("cycles"), "Equivalent full cycles", t_sum, "{{ value_json.equivalent_cycles }}",
                          state_class="total_increasing", precision=1),
    }

    if "rPackCurrent_A" in meas:
        cmps["current"] = _sensor(uid("current"), "Pack current", t_meas, "{{ %srPackCurrent_A }}" % m,
                                  "A", "current", precision=2)
        cmps["power"] = _sensor(uid("power"), "Pack power", t_meas,
                                "{{ (%srPackVoltage_V * %srPackCurrent_A) | round(1) }}" % (m, m),
                                "W", "power", precision=0)
    if "rMOSFETTemp_degC" in meas:
        cmps["mosfet_temp"] = _sensor(uid("mosfet_temp"), "MOSFET temperature", t_meas,
                                      "{{ %srMOSFETTemp_degC }}" % m, "°C", "temperature", diagnostic=True)
    for i in range(len(meas["rCellTemps_degC"])):
        cmps[f"cell_temp_{i + 1}"] = _sensor(uid(f"cell_temp_{i + 1}"), f"Battery temperature {i + 1}", t_meas,
                                             "{{ %srCellTemps_degC[%d] }}" % (m, i), "°C", "temperature")
    for i in range(len(meas["rCellVoltages_V"])):
        cmps[f"cell_{i + 1}"] = _sensor(uid(f"cell_{i + 1}"), f"Cell {i + 1} voltage", t_meas,
                                        "{{ %srCellVoltages_V[%d] }}" % (m, i), "V", "voltage",
                                        precision=3, diagnostic=True)

    device = info["device"]
    return {
        "dev": {
            "ids": [node],
            "name": f"Libre Solar BMS {device_id}",
            "mf": device["cManufacturer"],
            "mdl": device["cDeviceType"],
            "sw": device["cFirmwareVersion"],
            **({"hw": device["cHardwareVersion"]} if "cHardwareVersion" in device else {}),
        },
        "o": {"name": "bms-gateway", "sw": __version__, "url": REPO_URL},
        "avty_t": contract.topic(device_id, "status"),
        "qos": 1,
        "cmps": cmps,
    }
