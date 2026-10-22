"""ThingSet text mode over a serial line, as spoken by the Libre Solar BMS.

Format (thingset-zephyr-sdk src/serial.c, thingset-node-c src/thingset_txt.c):

    #mLive {"Meas":{"rPackVoltage_V":26.41,...}}      live report, sent periodically
    ?Device                                           request: get a group
    :85 {"cManufacturer":"Libre Solar",...}           response: 0x85 = Content
    :A4 "Not Found"                                   response: error

Each line ends with CRLF. With CONFIG_THINGSET_SERIAL_USE_CRC, the message is
followed by " XXXXXXXX#", the CRC-32 (IEEE) of the message in hex.
"""

import json
import re
import zlib
from dataclasses import dataclass

LIVE_SUBSET = "mLive"
STATUS_CONTENT = 0x85

_CRC_SUFFIX = re.compile(r"^(.*) ([0-9A-Fa-f]{8})#$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Start of a report or response, possibly after a shell prompt. Response codes
# are 0x80 or higher, which keeps log timestamps like "00:00:12 " from matching.
_MESSAGE_START = re.compile(r"(#[A-Za-z_]\w* |:[89A-Fa-f][0-9A-Fa-f]( |$))")


class ThingSetError(ValueError):
    pass


@dataclass
class Report:
    path: str
    data: dict


@dataclass
class Response:
    status: int
    data: object  # payload (dict for a group), or the error text

    @property
    def ok(self) -> bool:
        return self.status < 0xA0


def crc32(message: str) -> int:
    return zlib.crc32(message.encode()) & 0xFFFFFFFF


def strip_crc(line: str) -> str:
    """Remove and check a trailing CRC if present."""
    match = _CRC_SUFFIX.match(line)
    if not match:
        return line
    message, crc = match.group(1), int(match.group(2), 16)
    if crc32(message) != crc:
        raise ThingSetError(f"CRC mismatch: got {crc:08X}, expected {crc32(message):08X}")
    return message


def parse_line(line: str) -> Report | Response | None:
    """Parse one line. Returns None for anything that is not ThingSet (e.g. log output).

    Colour codes and a leading shell prompt (Zephyr shell on USB) are skipped.
    """
    line = _ANSI.sub("", line).strip()
    match = _MESSAGE_START.search(line)
    if not match:
        return None
    line = line[match.start():]
    line = strip_crc(line)
    head, _, payload = line.partition(" ")
    try:
        if head.startswith("#"):
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ThingSetError(f"Report {head} is not an object")
            return Report(head[1:], data)
        status = int(head[1:], 16)
        return Response(status, json.loads(payload) if payload else None)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ThingSetError(f"Malformed ThingSet line: {line[:80]!r} ({exc})") from None


def frame(message: str, with_crc: bool = False) -> bytes:
    if with_crc:
        message = f"{message} {crc32(message):08X}#"
    return (message + "\r\n").encode()


def encode_report(path: str, data: dict, with_crc: bool = False) -> bytes:
    return frame(f"#{path} {json.dumps(data, separators=(',', ':'))}", with_crc)


def encode_response(status: int, data=None, with_crc: bool = False) -> bytes:
    payload = "" if data is None else " " + json.dumps(data, separators=(",", ":"))
    return frame(f":{status:02X}{payload}", with_crc)


def encode_get(path: str) -> bytes:
    return frame(f"?{path}")
