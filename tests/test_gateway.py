import json

from bms_gateway import contract, ha_discovery
from bms_gateway.gateway import Gateway
from bms_gateway.simulator import BatteryModel, build_info, build_meas


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))

    def topics(self):
        return [t for t, _, _ in self.published]


def make_gateway():
    client = FakeClient()
    return Gateway(client, window_s=900, stale_after_s=120, time_scale=1), client


def send(gw, kind, message, device_id="bms-sim-01"):
    gw.handle(contract.topic(device_id, kind), json.dumps(message).encode())


def test_simulator_stream_produces_summary_and_discovery():
    gw, client = make_gateway()
    model = BatteryModel(seed=1)
    gw.handle(contract.topic("bms-sim-01", "status"), b"online")
    send(gw, "info", build_info(model, "bms-sim-01"))
    for seq in range(3):
        model.step(5)
        send(gw, "meas", build_meas(model, "bms-sim-01", seq))

    assert client.topics() == [
        "libre-solar/bms/bms-sim-01/summary",
        "homeassistant/device/libre_solar_bms_bms_sim_01/config",
    ]
    _, summary, retained = client.published[0]
    contract.validate("summary", summary)
    assert retained


def test_discovery_matches_device():
    model = BatteryModel(seed=1, cells=8)
    info = build_info(model, "bms-sim-01")
    payload = ha_discovery.build("bms-sim-01", info, model.meas())
    cmps = payload["cmps"]
    assert payload["avty_t"] == "libre-solar/bms/bms-sim-01/status"
    assert payload["dev"]["mf"] == "Libre Solar"
    assert len([k for k in cmps if k.startswith("cell_") and k[5:].isdigit()]) == 8
    assert cmps["soc"]["dev_cla"] == "battery"
    assert cmps["problem"]["p"] == "binary_sensor"
    assert cmps["health"]["stat_t"] == "libre-solar/bms/bms-sim-01/summary"
    assert len({c["uniq_id"] for c in cmps.values()}) == len(cmps)


def test_discovery_only_republished_when_it_changes_or_ha_restarts():
    gw, client = make_gateway()
    model = BatteryModel(seed=1)
    send(gw, "info", build_info(model, "bms-sim-01"))
    for seq in range(3):
        send(gw, "meas", build_meas(model, "bms-sim-01", seq))
    discovery = [t for t in client.topics() if t.startswith("homeassistant/")]
    assert len(discovery) == 1

    gw.handle("homeassistant/status", b"online")
    discovery = [t for t in client.topics() if t.startswith("homeassistant/")]
    assert len(discovery) == 2


def test_invalid_and_mismatched_messages_are_dropped(caplog):
    gw, client = make_gateway()
    model = BatteryModel(seed=1)
    gw.handle(contract.topic("bms-sim-01", "meas"), b"not json")
    bad = build_meas(model, "bms-sim-01", 0)
    bad["meas"]["rSOC_pct"] = 150
    send(gw, "meas", bad)
    send(gw, "meas", build_meas(model, "other-device", 0))  # payload says another device
    assert client.published == []
    assert caplog.text.count("Dropping") == 3


def test_cleared_retained_message_is_ignored():
    gw, client = make_gateway()
    gw.handle(contract.topic("bms-sim-01", "info"), b"")
    assert client.published == []


def test_devices_are_independent():
    gw, client = make_gateway()
    for device_id in ("site-a", "site-b"):
        model = BatteryModel(seed=1)
        send(gw, "info", build_info(model, device_id), device_id)
        send(gw, "meas", build_meas(model, device_id, 0), device_id)
    assert set(gw.monitors) == {"site-a", "site-b"}
    assert "libre-solar/bms/site-b/summary" in client.topics()
