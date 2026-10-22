import socket
import threading
import time

import pytest

from bms_gateway import contract, thingset
from bms_gateway.relay import INFO_RETRY_S, LineSource, Relay
from bms_gateway.simulator import BatteryModel
from bms_gateway.thingset_emulator import Emulator

# Same shape as the firmware's output, including null for NaN right after boot.
BOOT_LINE = (
    '#mLive {"Meas":{"rPackVoltage_V":0.00,"rStackVoltage_V":0.00,"rPackCurrent_A":0.00,'
    '"rCellTemps_degC":[-273.1,0.0],"rICTemp_degC":-273.1,"rMOSFETTemp_degC":-273.1,"rSOC_pct":0.0,'
    '"rErrorFlags":0,"rBmsState":3,"rCellVoltages_V":[0.000,0.000,0.000,0.000],"rCellAvgVoltage_V":null,'
    '"rCellMinVoltage_V":10.000,"rCellMaxVoltage_V":0.000,"rBalancingStatus":0}}'
)
LIVE_LINE = (
    '#mLive {"Meas":{"rPackVoltage_V":13.21,"rPackCurrent_A":-2.50,"rCellTemps_degC":[24.5],'
    '"rICTemp_degC":27.0,"rSOC_pct":71.5,"rErrorFlags":512,"rBmsState":2,'
    '"rCellVoltages_V":[3.301,3.298,3.305,3.306],"rCellAvgVoltage_V":3.302,"rCellMinVoltage_V":3.298,'
    '"rCellMaxVoltage_V":3.306,"rBalancingStatus":0,"rShuntTemp_degC":null}}'
)


# --- ThingSet text codec ---------------------------------------------------------

def test_parse_live_report():
    report = thingset.parse_line(LIVE_LINE + "\r\n")
    assert report.path == "mLive"
    assert report.data["Meas"]["rSOC_pct"] == 71.5


def test_parse_responses():
    ok = thingset.parse_line(':85 {"cManufacturer":"Libre Solar"}')
    assert ok.ok and ok.data == {"cManufacturer": "Libre Solar"}
    err = thingset.parse_line(':A4 "Not Found"')
    assert not err.ok and err.data == "Not Found"


def test_non_thingset_lines_are_ignored():
    assert thingset.parse_line("[00:00:01.000,000] <inf> bms: CHG -> NORMAL") is None
    assert thingset.parse_line("") is None


def test_crc_round_trip_and_mismatch():
    line = thingset.encode_report("mLive", {"Meas": {"rSOC_pct": 50.0}}, with_crc=True).decode()
    assert line.endswith("#\r\n")
    assert thingset.parse_line(line).data == {"Meas": {"rSOC_pct": 50.0}}
    corrupted = line.replace("50.0", "51.0")
    with pytest.raises(thingset.ThingSetError, match="CRC"):
        thingset.parse_line(corrupted)


def test_crc_matches_zephyr_crc32_ieee():
    # crc32_ieee("123456789") == 0xCBF43926 (standard check value)
    assert thingset.crc32("123456789") == 0xCBF43926


def test_malformed_json_raises():
    with pytest.raises(thingset.ThingSetError):
        thingset.parse_line('#mLive {"Meas":')


# --- Relay mapping -------------------------------------------------------------

def test_live_report_becomes_valid_meas():
    relay = Relay("site-01")
    [(kind, message)] = relay.handle_line(LIVE_LINE)
    assert kind == "meas"
    contract.validate("meas", message)
    assert message["device_id"] == "site-01"
    assert message["meas"]["rErrorFlags"] == 512
    assert "rShuntTemp_degC" not in message["meas"]  # null optional field dropped


def test_boot_report_with_null_required_field_is_held_back():
    relay = Relay("site-01")
    assert relay.handle_line(BOOT_LINE) == []
    assert relay.seq == 0


def test_seq_counts_published_reports():
    relay = Relay("site-01")
    relay.handle_line(LIVE_LINE)
    [(_, second)] = relay.handle_line(LIVE_LINE)
    assert second["seq"] == 1


def test_info_from_device_and_conf_responses():
    relay = Relay("site-01")
    requests = relay.requests(now=1000.0)
    assert requests == [b"?Device\r\n", b"?Conf\r\n"]
    assert relay.handle_line(':85 {"cManufacturer":"Libre Solar","cDeviceType":"BMS C1",'
                             '"cFirmwareVersion":"v24.0","xShutdown":[]}') == []
    [(kind, info)] = relay.handle_line(
        ':85 {"sNominalCapacity_Ah":50.0,"sCellOvervoltage_V":3.8,"sCellUndervoltage_V":2.5,'
        '"sDisMaxTemp_degC":45.0,"sChgMaxTemp_degC":45.0,"xPresetLFP":[]}'
    )
    assert kind == "info"
    contract.validate("info", info)
    assert info["source"] == "serial"
    assert "xShutdown" not in info["device"] and "xPresetLFP" not in info["conf"]
    assert relay.requests(now=5000.0) == []  # complete: no more requests


def test_info_requests_are_retried_until_answered():
    relay = Relay("site-01")
    assert relay.requests(now=0.0)
    assert relay.requests(now=INFO_RETRY_S / 2) == []
    assert relay.requests(now=INFO_RETRY_S + 1) == [b"?Device\r\n", b"?Conf\r\n"]


def test_error_response_does_not_produce_info():
    relay = Relay("site-01")
    relay.requests(now=0.0)
    assert relay.handle_line(':A4 "Not Found"') == []
    assert relay.device is None


# --- End to end over TCP against the emulator ----------------------------------

def test_relay_reads_emulator_over_tcp():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    emulator = Emulator(BatteryModel(seed=1), interval=0.05, speed=60, with_crc=True)

    def serve_once():
        conn, _ = server.accept()
        emulator.serve(conn)

    threading.Thread(target=serve_once, daemon=True).start()
    source = LineSource(f"tcp://127.0.0.1:{port}", baudrate=115200, replay_interval=0)
    relay = Relay("bms-ts-01", source_kind="simulator")
    published = []
    deadline = time.time() + 5
    while time.time() < deadline and not ({"info", "meas"} <= {k for k, _ in published} and len(published) > 3):
        for request in relay.requests(time.time()):
            source.write(request)
        line = source.readline()
        assert line is not None
        published += relay.handle_line(line)
    source.close()
    server.close()

    kinds = [k for k, _ in published]
    assert "info" in kinds and kinds.count("meas") >= 3
    for kind, message in published:
        contract.validate(kind, message)
    info = next(m for k, m in published if k == "info")
    assert info["source"] == "simulator"
    assert info["conf"]["sNominalCapacity_Ah"] == 100.0


def test_file_replay(tmp_path):
    capture = tmp_path / "capture.txt"
    capture.write_text("boot log line\n" + BOOT_LINE + "\n" + LIVE_LINE + "\n" + LIVE_LINE + "\n")
    source = LineSource(f"file:{capture}", baudrate=115200, replay_interval=0)
    relay = Relay("site-01")
    out = []
    while (line := source.readline()) is not None:
        out += relay.handle_line(line)
    assert [k for k, _ in out] == ["meas", "meas"]


# --- BMS C1 USB shell and older firmware ---------------------------------------

# Response from the BMS C1 manual (manual/5-operation.md), an older firmware with rBatTemp_degC.
MANUAL_MEAS = (
    ':85 {"rPackVoltage_V":13.71,"rStackVoltage_V":13.91,"rPackCurrent_A":0.05,"rBatTemp_degC":29.4,'
    '"rICTemp_degC":34.2,"rMOSFETTemp_degC":31.8,"rSOC_pct":100.0,"rErrorFlags":0,"rBmsState":3,'
    '"rCellVoltages_V":[3.495,3.497,3.486,3.495,0.000,0.000,0.000,0.000,0.000,0.000,0.000,0.000,0.000,'
    '0.000,0.000,0.000],"rCellAvgVoltage_V":3.493,"rCellMinVoltage_V":3.486,"rCellMaxVoltage_V":3.497,'
    '"rBalancingStatus":0}'
)


def test_poll_mode_requests_meas_periodically():
    relay = Relay("site-01", poll_interval=5)
    assert b"?Meas\r\n" in relay.requests(now=0.0)
    assert b"?Meas\r\n" not in relay.requests(now=2.0)
    assert b"?Meas\r\n" in relay.requests(now=5.0)


def test_meas_response_from_older_firmware_maps_battery_temperature():
    [(kind, message)] = Relay("site-01", poll_interval=5).handle_line(MANUAL_MEAS)
    assert kind == "meas"
    assert message["meas"]["rCellTemps_degC"] == [29.4]
    assert "rBatTemp_degC" not in message["meas"]


def test_shell_prompt_and_colour_codes_are_skipped():
    line = "\x1b[1;32mthingset:~$ \x1b[m" + MANUAL_MEAS
    [(kind, _)] = Relay("site-01").handle_line(line)
    assert kind == "meas"


@pytest.mark.parametrize("line", [
    "[00:00:12.345,000] <inf> bms: CHG -> NORMAL (error flags: 0x00000000)",
    "uart:~$ select thingset",
    "?Meas",  # shell echo of our own request
])
def test_shell_noise_is_ignored(line):
    assert thingset.parse_line(line) is None


def test_responses_are_matched_by_content_not_order():
    relay = Relay("site-01")
    relay.handle_line(MANUAL_MEAS)  # unsolicited Meas response must not be taken as Device
    assert relay.device is None
    relay.handle_line(':85 {"sNominalCapacity_Ah":50.0,"sCellOvervoltage_V":3.8,"sCellUndervoltage_V":2.5,'
                      '"sDisMaxTemp_degC":45.0,"sChgMaxTemp_degC":45.0}')
    [(kind, _)] = relay.handle_line(':85 {"cManufacturer":"Libre Solar","cDeviceType":"BMS C1",'
                                    '"cFirmwareVersion":"v24.0"}')
    assert kind == "info"
