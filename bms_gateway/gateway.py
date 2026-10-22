"""Gateway service: subscribes to every BMS on the broker and publishes
`summary`, `event` and Home Assistant discovery for each.

    python -m bms_gateway.gateway --broker localhost

Devices are found automatically from their topics; nothing is configured per
battery. Invalid messages are logged and dropped, so a misbehaving device
cannot push bad data into MPM.
"""

import argparse
import json
import logging
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt
from jsonschema import ValidationError

from bms_gateway import contract, ha_discovery
from bms_gateway.monitor import DeviceMonitor, Outgoing

log = logging.getLogger("bms_gateway")

HA_STATUS_TOPIC = f"{ha_discovery.DISCOVERY_PREFIX}/status"


class Gateway:
    def __init__(self, client: mqtt.Client, window_s: float, stale_after_s: float, time_scale: float,
                 ha_discovery_enabled: bool = True):
        self.client = client
        self.window_s = window_s
        self.stale_after_s = stale_after_s
        self.time_scale = time_scale
        self.ha_discovery_enabled = ha_discovery_enabled
        self.monitors: dict[str, DeviceMonitor] = {}
        self.discovered: dict[str, dict] = {}  # device_id -> last discovery payload
        self.lock = threading.Lock()

    def monitor(self, device_id: str) -> DeviceMonitor:
        if device_id not in self.monitors:
            log.info("New device %s", device_id)
            self.monitors[device_id] = DeviceMonitor(
                device_id, window_s=self.window_s, stale_after_s=self.stale_after_s, time_scale=self.time_scale
            )
        return self.monitors[device_id]

    def subscribe(self) -> None:
        for kind in ("status", "info", "meas", "summary"):
            self.client.subscribe(contract.topic("+", kind), qos=1)
        self.client.subscribe(HA_STATUS_TOPIC, qos=1)

    def handle(self, topic: str, payload: bytes) -> None:
        if topic == HA_STATUS_TOPIC:
            if payload == b"online":  # Home Assistant restarted: send discovery again
                with self.lock:
                    for device_id in list(self.discovered):
                        self._publish_discovery(device_id, force=True)
            return

        _, _, device_id, kind = topic.rsplit("/", 3)
        if not payload:
            return  # retained message cleared
        with self.lock:
            m = self.monitor(device_id)
            if kind == "status":
                self._send(device_id, m.on_status(payload.decode()))
                return
            try:
                message = json.loads(payload)
                contract.validate(kind, message)
            except (ValueError, ValidationError) as exc:
                log.warning("Dropping invalid %s from %s: %s", kind, device_id, str(exc).splitlines()[0])
                return
            if message["device_id"] != device_id:
                log.warning("Dropping %s: device_id %s does not match topic", kind, message["device_id"])
                return
            if kind == "info":
                m.on_info(message)
            elif kind == "summary":
                m.on_retained_summary(message)
            elif kind == "meas":
                self._send(device_id, m.on_meas(message))
                self._publish_discovery(device_id)

    def tick(self) -> None:
        with self.lock:
            for device_id, m in self.monitors.items():
                self._send(device_id, m.tick())

    def _send(self, device_id: str, messages: list[Outgoing]) -> None:
        for out in messages:
            contract.validate(out.kind, out.payload)
            self.client.publish(contract.topic(device_id, out.kind), json.dumps(out.payload),
                                qos=out.qos, retain=out.retain)
            if out.kind == "event":
                log.info("%s %s %s", device_id, out.payload["type"], out.payload["fault"] or "")
            else:
                log.info("%s summary: %s", device_id, out.payload["health"]["status"])

    def _publish_discovery(self, device_id: str, force: bool = False) -> None:
        m = self.monitors[device_id]
        if not self.ha_discovery_enabled or m.info is None or m.last_meas is None:
            return
        payload = ha_discovery.build(device_id, m.info, m.last_meas["meas"])
        if not force and self.discovered.get(device_id) == payload:
            return
        self.client.publish(ha_discovery.discovery_topic(device_id), json.dumps(payload), qos=1, retain=True)
        self.discovered[device_id] = payload
        log.info("%s Home Assistant discovery published (%d entities)", device_id, len(payload["cmps"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--broker", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD"))
    parser.add_argument("--summary-interval", type=float, default=float(os.getenv("SUMMARY_INTERVAL_S", "900")),
                        help="seconds per summary window (default 900)")
    parser.add_argument("--stale-after", type=float, default=float(os.getenv("STALE_AFTER_S", "120")),
                        help="seconds without meas before the device is reported as silent")
    parser.add_argument("--time-scale", type=float, default=float(os.getenv("TIME_SCALE", "1")),
                        help="set to the simulator's SIM_SPEED so energy and cycles match; 1 for real hardware")
    parser.add_argument("--no-ha-discovery", action="store_true",
                        default=os.getenv("HA_DISCOVERY", "true").lower() == "false")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bms-gateway", clean_session=False)
    if args.username:
        client.username_pw_set(args.username, args.password)
    gateway = Gateway(client, args.summary_interval, args.stale_after, args.time_scale,
                      ha_discovery_enabled=not args.no_ha_discovery)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("Connected to %s:%s (%s)", args.broker, args.port, reason_code)
        gateway.subscribe()

    def on_message(client, userdata, msg):
        try:
            gateway.handle(msg.topic, msg.payload)
        except Exception:
            log.exception("Error handling %s", msg.topic)

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=30)
    client.loop_start()
    try:
        while True:
            time.sleep(1)
            gateway.tick()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
