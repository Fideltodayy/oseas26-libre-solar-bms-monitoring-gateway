"""Emulates the Libre Solar BMS serial port over TCP, speaking ThingSet text mode.

Stands in for the UART when no board is at hand, so the relay path
(BMS → ThingSet → relay → MQTT) can be run and tested end to end:

    python -m bms_gateway.thingset_emulator --port 3333 --speed 60
    python -m bms_gateway.relay --source tcp://localhost:3333 --source-kind simulator

Sends `#mLive` reports and answers `?Device` and `?Conf`, like the firmware.
"""

import argparse
import os
import socket
import threading
import time

from bms_gateway import __version__, thingset
from bms_gateway.simulator import SCENARIOS, BatteryModel, Limits


class Emulator:
    def __init__(self, model: BatteryModel, interval: float, speed: float, with_crc: bool):
        self.model = model
        self.interval = interval
        self.speed = speed
        self.with_crc = with_crc
        self.lock = threading.Lock()

    def groups(self) -> dict:
        return {
            "Device": {
                "cManufacturer": "Libre Solar",
                "cDeviceType": "BMS C1 (ThingSet emulator)",
                "cHardwareVersion": "v0.4",
                "cFirmwareVersion": f"bms-gateway-emulator {__version__}",
            },
            "Conf": self.model.limits.to_conf(),
        }

    def answer(self, line: str) -> bytes | None:
        line = line.strip()
        if not line.startswith("?"):
            return None
        group = self.groups().get(line[1:])
        if group is None:
            return thingset.encode_response(0xA4, "Not Found", self.with_crc)
        return thingset.encode_response(thingset.STATUS_CONTENT, group, self.with_crc)

    def live_report(self) -> bytes:
        with self.lock:
            self.model.step(self.interval * self.speed)
            meas = self.model.meas()
        return thingset.encode_report(thingset.LIVE_SUBSET, {"Meas": meas}, self.with_crc)

    def serve(self, conn: socket.socket) -> None:
        stop = threading.Event()

        def reader():
            buf = b""
            while not stop.is_set():
                try:
                    chunk = conn.recv(1024)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    reply = self.answer(raw.decode(errors="replace"))
                    if reply:
                        conn.sendall(reply)
            stop.set()

        threading.Thread(target=reader, daemon=True).start()
        try:
            while not stop.is_set():
                conn.sendall(self.live_report())
                stop.wait(self.interval)
        except OSError:
            pass
        finally:
            stop.set()
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.getenv("EMULATOR_PORT", "3333")))
    parser.add_argument("--interval", type=float, default=float(os.getenv("PUBLISH_INTERVAL_S", "5")))
    parser.add_argument("--speed", type=float, default=float(os.getenv("SIM_SPEED", "1")))
    parser.add_argument("--scenario", choices=list(SCENARIOS), default=os.getenv("SIM_SCENARIO", "normal"))
    parser.add_argument("--crc", action="store_true", default=os.getenv("THINGSET_CRC", "false") == "true",
                        help="append CRCs like CONFIG_THINGSET_SERIAL_USE_CRC")
    args = parser.parse_args()

    model = BatteryModel(limits=Limits())
    model.set_scenario(args.scenario)
    emulator = Emulator(model, args.interval, args.speed, args.crc)
    server = socket.create_server(("0.0.0.0", args.port))
    print(f"ThingSet emulator listening on port {args.port}", flush=True)
    while True:
        conn, addr = server.accept()
        print(f"Client connected from {addr[0]}", flush=True)
        emulator.serve(conn)  # one client at a time, like a UART
        print("Client disconnected", flush=True)


if __name__ == "__main__":
    main()
