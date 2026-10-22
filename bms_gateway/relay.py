"""Relay: reads a Libre Solar BMS over ThingSet and publishes contract v1 to MQTT.

Runs next to the BMS (Raspberry Pi, laptop, router) and connects to its UART:

    python -m bms_gateway.relay --source serial:/dev/ttyUSB0 --device-id site-01
    python -m bms_gateway.relay --source tcp://192.168.1.50:3333     # ser2net, native_sim
    python -m bms_gateway.relay --source file:capture.txt            # replay a recorded log

The BMS sends `#mLive` reports by itself; the relay also asks for the
`Device` and `Conf` groups to build the `info` message. The same mapping is
what a Cicada 4G relay (path A2) has to implement in its firmware.
"""

import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from jsonschema import ValidationError

from bms_gateway import contract, thingset

log = logging.getLogger("bms_gateway.relay")

INFO_RETRY_S = 30.0
DEVICE_FIELDS = ("cManufacturer", "cDeviceType", "cHardwareVersion", "cFirmwareVersion")


class Relay:
    """ThingSet lines in, contract messages out. No I/O, so it is easy to test.

    Responses are recognised by their content rather than by request order, so
    a lost or unsolicited response cannot shift the stream out of step.
    """

    def __init__(self, device_id: str, source_kind: str = "serial", poll_interval: float | None = None):
        self.device_id = device_id
        self.source_kind = source_kind
        # Poll `?Meas` instead of waiting for `#mLive` reports (USB shell, where reporting is off).
        self.poll_interval = poll_interval
        self.seq = 0
        self.device: dict | None = None
        self.conf: dict | None = None
        self.last_info_request = float("-inf")
        self.last_poll = float("-inf")

    def requests(self, now: float) -> list[bytes]:
        """GET requests to send now: info while incomplete, and `?Meas` in poll mode."""
        out = []
        if not (self.device and self.conf) and now - self.last_info_request >= INFO_RETRY_S:
            self.last_info_request = now
            out += [thingset.encode_get(path) for path, have in (("Device", self.device), ("Conf", self.conf))
                    if not have]
        if self.poll_interval and now - self.last_poll >= self.poll_interval:
            self.last_poll = now
            out.append(thingset.encode_get("Meas"))
        return out

    def handle_line(self, line: str) -> list[tuple[str, dict]]:
        """Returns (kind, message) pairs to publish."""
        try:
            parsed = thingset.parse_line(line)
        except thingset.ThingSetError as exc:
            log.warning("%s", exc)
            return []
        if isinstance(parsed, thingset.Report) and parsed.path == thingset.LIVE_SUBSET:
            meas = self._meas(parsed.data.get("Meas"))
            return [("meas", meas)] if meas else []
        if isinstance(parsed, thingset.Response):
            if not parsed.ok or not isinstance(parsed.data, dict):
                log.warning("ThingSet request failed: %02X %s", parsed.status, parsed.data)
                return []
            data = parsed.data
            if "rSOC_pct" in data:
                meas = self._meas(data)
                return [("meas", meas)] if meas else []
            if "cManufacturer" in data:
                self.device = {k: data[k] for k in DEVICE_FIELDS if k in data}
                return self._info()
            if "sNominalCapacity_Ah" in data:
                self.conf = {k: v for k, v in data.items() if k.startswith("s") and v is not None}
                return self._info()
        return []

    def _meas(self, meas) -> dict | None:
        if not isinstance(meas, dict):
            return None
        # The firmware reports NaN as null, e.g. right after boot before the first
        # measurement. Optional fields are dropped; if a required one is null the
        # report is held back rather than sending a half-valid message.
        meas = {k: v for k, v in meas.items() if v is not None}
        # Older firmware (e.g. the BMS C1 manual's example) has one battery temperature.
        if "rBatTemp_degC" in meas and "rCellTemps_degC" not in meas:
            meas["rCellTemps_degC"] = [meas.pop("rBatTemp_degC")]
        message = {
            "schema_version": contract.SCHEMA_VERSION,
            "device_id": self.device_id,
            "ts": contract.utc_now(),
            "seq": self.seq,
            "meas": meas,
        }
        try:
            contract.validate("meas", message)
        except ValidationError as exc:
            log.info("Holding back report that does not match the contract yet: %s", exc.message)
            return None
        self.seq += 1
        return message

    def _info(self) -> list[tuple[str, dict]]:
        if not (self.device and self.conf):
            return []
        info = {
            "schema_version": contract.SCHEMA_VERSION,
            "device_id": self.device_id,
            "ts": contract.utc_now(),
            "source": self.source_kind,
            "device": self.device,
            "conf": self.conf,
        }
        try:
            contract.validate("info", info)
        except ValidationError as exc:
            log.warning("BMS info does not match the contract: %s", exc.message)
            return []
        return [("info", info)]


class LineSource:
    """A text line stream from serial, TCP or a file."""

    def __init__(self, url: str, baudrate: int, replay_interval: float):
        self.url = url
        self.kind = url.split(":", 1)[0]
        self.baudrate = baudrate
        self.replay_interval = replay_interval
        self._open()

    def _open(self) -> None:
        if self.kind == "serial":
            import serial  # pyserial

            self.conn = serial.Serial(self.url.split(":", 1)[1], self.baudrate, timeout=1)
        elif self.kind == "tcp":
            u = urlparse(self.url)
            self.conn = socket.create_connection((u.hostname, u.port), timeout=10)
            self.conn.settimeout(1)
            self.buffer = b""
        elif self.kind == "file":
            self.conn = open(self.url.split(":", 1)[1], "rb")
        else:
            raise ValueError(f"Unsupported source {self.url!r}: use serial:, tcp:// or file:")

    @property
    def writable(self) -> bool:
        return self.kind != "file"

    def readline(self) -> str | None:
        """One line, '' on timeout, None at the end of a file or a closed connection."""
        if self.kind == "tcp":
            # Buffer by hand: a socket file object is unusable after its first timeout.
            while b"\n" not in self.buffer:
                try:
                    chunk = self.conn.recv(4096)
                except (socket.timeout, TimeoutError):
                    return ""
                if not chunk:
                    return None
                self.buffer += chunk
            raw, self.buffer = self.buffer.split(b"\n", 1)
        else:
            raw = self.conn.readline()
            if raw == b"" and self.kind == "file":
                return None
        if self.kind == "file" and raw.strip():
            time.sleep(self.replay_interval)
        return raw.decode(errors="replace")

    def write(self, data: bytes) -> None:
        if self.kind == "tcp":
            self.conn.sendall(data)
        elif self.kind == "serial":
            self.conn.write(data)

    def close(self) -> None:
        self.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=os.getenv("RELAY_SOURCE", "serial:/dev/ttyUSB0"),
                        help="serial:<port>, tcp://<host>:<port> or file:<path>")
    parser.add_argument("--baudrate", type=int, default=int(os.getenv("RELAY_BAUDRATE", "115200")))
    parser.add_argument("--replay-interval", type=float, default=1.0, help="seconds between lines for file:")
    parser.add_argument("--device-id", default=os.getenv("DEVICE_ID", "bms-01"))
    parser.add_argument("--poll", type=float, default=float(os.getenv("RELAY_POLL_S", "0")) or None,
                        metavar="SECONDS", help="request ?Meas every SECONDS instead of waiting for #mLive "
                        "reports (needed on the BMS C1 USB shell, where reporting is off)")
    parser.add_argument("--usb-shell", action="store_true", default=os.getenv("RELAY_USB_SHELL") == "true",
                        help="the source is the Zephyr shell on USB: switch it to ThingSet mode first")
    parser.add_argument("--source-kind", choices=["serial", "simulator"], default=os.getenv("SOURCE_KIND", "serial"),
                        help="how `info` labels the data; use simulator when reading the ThingSet emulator")
    parser.add_argument("--broker", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD"))
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    status_topic = contract.topic(args.device_id, "status")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"relay-{args.device_id}")
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.will_set(status_topic, "offline", qos=1, retain=True)
    connected = threading.Event()
    client.on_connect = lambda *_: connected.set()
    client.connect(args.broker, args.port, keepalive=30)
    client.loop_start()
    if not connected.wait(timeout=10):
        raise SystemExit(f"Could not connect to MQTT broker at {args.broker}:{args.port}")

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    relay = Relay(args.device_id, source_kind=args.source_kind, poll_interval=args.poll)
    online = False
    try:
        while True:
            try:
                source = LineSource(args.source, args.baudrate, args.replay_interval)
            except (OSError, ValueError) as exc:
                log.warning("Cannot open %s: %s; retrying in 5 s", args.source, exc)
                time.sleep(5)
                continue
            log.info("Reading ThingSet from %s", args.source)
            if args.usb_shell:
                source.write(b"select thingset\r\n")
            while True:
                if source.writable:
                    for request in relay.requests(time.time()):
                        source.write(request)
                line = source.readline()
                if line is None:
                    break
                for kind, message in relay.handle_line(line):
                    if not online:
                        client.publish(status_topic, "online", qos=1, retain=True)
                        online = True
                    retain = kind == "info"
                    client.publish(contract.topic(args.device_id, kind), json.dumps(message),
                                   qos=1 if retain else 0, retain=retain)
            source.close()
            if source.kind == "file":
                log.info("End of %s", args.source)
                break
            log.warning("Lost connection to %s; reconnecting", args.source)
            client.publish(status_topic, "offline", qos=1, retain=True)
            online = False
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(status_topic, "offline", qos=1, retain=True).wait_for_publish(timeout=2)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
