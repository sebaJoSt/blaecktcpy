"""Tests for typed device commands: 0xE0 Command List discovery, value
validation, SELECT/TEXT normalization, and 0xF0 Command Ack frames.

The byte layouts here mirror the BlaeckTCP firmware and are the exact contract
consumed by Loggbok's ParsingService (0xE0) and LoggingSession (0xF0).
"""

import socket
import struct
import time

import pytest

from blaecktcpy import BlaeckCommandKind
from blaecktcpy._encoder import fnv1a32
from conftest import _make_server_on_free_port, _start_retry


# ── helpers ────────────────────────────────────────────────────────────────
def _make_device_with_client():
    device = _make_server_on_free_port()
    _start_retry(device)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    client.connect(("127.0.0.1", device._port))
    device._accept_new_clients()
    return device, client


def _read_frame(client, msg_key):
    """Read one complete ``<BLAECK:...<key>.../BLAECK>\\r\\n`` frame of msg_key."""
    buf = b""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            chunk = client.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        start = buf.find(b"<BLAECK:")
        end = buf.find(b"/BLAECK>\r\n")
        if start != -1 and end != -1 and buf[start + 8 : start + 9] == msg_key:
            return buf[start : end + 10]
        # frame present but different key: drop up to end and keep scanning
        if start != -1 and end != -1:
            buf = buf[end + 10 :]
    raise AssertionError(f"no frame with key {msg_key!r} received (buf={buf!r})")


def _read_null_str(data, pos):
    end = data.index(0, pos)
    return data[pos:end].decode(), end + 1


def _parse_command_list(frame):
    """Parse a 0xE0 frame into a list of dicts, mirroring Loggbok's parser."""
    payload = frame[15:-10]  # after header(15), before footer(10)
    entries = []
    pos = 0
    while pos + 5 <= len(payload):
        msc = payload[pos]
        slave_id = payload[pos + 1]
        pos += 2
        name, pos = _read_null_str(payload, pos)
        kind = payload[pos]
        flags = payload[pos + 1]
        pos += 2
        e = {"msc": msc, "slave_id": slave_id, "name": name, "kind": kind, "flags": flags}
        if flags & 0x01:
            e["min"], e["max"], e["step"] = struct.unpack_from("<fff", payload, pos)
            pos += 12
        if flags & 0x02:
            e["unit"], pos = _read_null_str(payload, pos)
        if flags & 0x04:
            csv, pos = _read_null_str(payload, pos)
            e["options"] = csv.split(",") if csv else []
        if flags & 0x08:
            e["state_signal"], pos = _read_null_str(payload, pos)
        if flags & 0x10:
            e["max_length"] = payload[pos] | (payload[pos + 1] << 8)
            pos += 2
        entries.append(e)
    return entries


def _parse_ack(frame):
    """Parse a 0xF0 frame -> (msg_id, hash, status, reason)."""
    msg_id = int.from_bytes(frame[10:14], "little")
    cmd_hash = int.from_bytes(frame[15:19], "little")
    status = frame[19]
    reason = frame[20]
    return msg_id, cmd_hash, status, reason


def _request_command_list(device, client):
    client.sendall(b"<BLAECK.WRITE_COMMANDS>")
    time.sleep(0.05)
    device.read()
    return _parse_command_list(_read_frame(client, b"\xe0"))


def _send(device, client, wire):
    client.sendall(b"<" + wire + b">")
    time.sleep(0.05)
    device.read()


# ── registration ────────────────────────────────────────────────────────────
class TestRegistration:
    def setup_method(self):
        self.device = _make_server_on_free_port()

    def test_number_stores_metadata(self):
        @self.device.on_number_command(
            "SET_FREQ", state_signal="Frequency", min_value=0, max_value=50, step=0.01, unit="Hz"
        )
        def _h(v):
            pass

        meta = self.device._command_meta["SET_FREQ"]
        assert meta.kind == BlaeckCommandKind.NUMBER
        assert (meta.min_value, meta.max_value, meta.step) == (0.0, 50.0, 0.01)
        assert meta.unit == "Hz"
        assert meta.state_signal == "Frequency"
        assert "SET_FREQ" in self.device._command_handlers

    def test_select_accepts_csv_or_list(self):
        @self.device.on_select_command("A", options="X,Y,Z")
        def _a(v):
            pass

        @self.device.on_select_command("B", options=["X", "Y"])
        def _b(v):
            pass

        assert self.device._command_meta["A"].options == ["X", "Y", "Z"]
        assert self.device._command_meta["B"].options == ["X", "Y"]

    def test_forward_false_opts_out(self):
        @self.device.on_button_command("PING", forward=False)
        def _p():
            pass

        assert "PING" in self.device._non_forwarded_commands


# ── 0xE0 discovery ───────────────────────────────────────────────────────────
class TestCommandListFrame:
    def test_all_kinds_and_flags(self):
        device, client = _make_device_with_client()
        try:
            @device.on_number_command(
                "SET_FREQ", state_signal="Frequency", min_value=0, max_value=50, step=0.01, unit="Hz"
            )
            def _n(v):
                pass

            @device.on_switch_command("SET_ENABLE", state_signal="Enabled")
            def _s(v):
                pass

            @device.on_select_command(
                "SET_WAVE", options="Sine,Square,Triangle,Sawtooth", state_signal="Waveform"
            )
            def _sel(v):
                pass

            @device.on_button_command("STATUS")
            def _b():
                pass

            @device.on_text_command("SET_LABEL", state_signal="Label", max_length=32)
            def _t(v):
                pass

            @device.on_command("LEGACY")
            def _legacy(*p):
                pass

            entries = _request_command_list(device, client)
            by_name = {e["name"]: e for e in entries}

            assert len(entries) == 6
            # every TCP entry is a single device: msc/slave 0
            assert all(e["msc"] == 0 and e["slave_id"] == 0 for e in entries)

            num = by_name["SET_FREQ"]
            assert num["kind"] == 1 and num["flags"] == 0x0B
            assert (num["min"], num["max"]) == (0.0, 50.0)
            assert abs(num["step"] - 0.01) < 1e-6
            assert num["unit"] == "Hz" and num["state_signal"] == "Frequency"

            sw = by_name["SET_ENABLE"]
            assert sw["kind"] == 2 and sw["flags"] == 0x08

            sel = by_name["SET_WAVE"]
            assert sel["kind"] == 3 and sel["flags"] == 0x0C
            assert sel["options"] == ["Sine", "Square", "Triangle", "Sawtooth"]

            btn = by_name["STATUS"]
            assert btn["kind"] == 4 and btn["flags"] == 0x00

            txt = by_name["SET_LABEL"]
            assert txt["kind"] == 5 and txt["flags"] == 0x18
            assert txt["max_length"] == 32

            legacy = by_name["LEGACY"]
            assert legacy["kind"] == 0 and legacy["flags"] == 0x00
        finally:
            client.close()
            device.close()

    def test_registration_order_preserved(self):
        device, client = _make_device_with_client()
        try:
            @device.on_button_command("C")
            def _c():
                pass

            @device.on_button_command("A")
            def _a():
                pass

            @device.on_button_command("B")
            def _b():
                pass

            entries = _request_command_list(device, client)
            assert [e["name"] for e in entries] == ["C", "A", "B"]
        finally:
            client.close()
            device.close()


# ── validation + 0xF0 ack ────────────────────────────────────────────────────
class TestValidationAndAck:
    def test_number_in_range_accepted(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_number_command("SET_FREQ", state_signal="F", min_value=0, max_value=50)
            def _h(v):
                got.append(v)

            _send(device, client, b"SET_FREQ,25")
            assert got == ["25"]
            _, h, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 0 and reason == 0
            assert h == fnv1a32("SET_FREQ,25")
        finally:
            client.close()
            device.close()

    def test_number_out_of_range_rejected(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_number_command("SET_FREQ", min_value=0, max_value=50)
            def _h(v):
                got.append(v)

            _send(device, client, b"SET_FREQ,999")
            assert got == []  # handler skipped
            _, h, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 1 and reason == 2  # OUT_OF_RANGE
            assert h == fnv1a32("SET_FREQ,999")
        finally:
            client.close()
            device.close()

    def test_switch_bad_value_rejected(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_switch_command("EN")
            def _h(v):
                got.append(v)

            _send(device, client, b"EN,2")
            assert got == []
            _, _, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 1 and reason == 3  # BAD_SWITCH
        finally:
            client.close()
            device.close()

    def test_select_name_normalized_to_index(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_select_command("W", options="Sine,Square,Triangle")
            def _h(v):
                got.append(v)

            _send(device, client, b"W,Triangle")
            assert got == ["2"]  # normalized to index string
            _, _, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 0 and reason == 0
        finally:
            client.close()
            device.close()

    def test_select_bad_value_rejected(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_select_command("W", options="Sine,Square")
            def _h(v):
                got.append(v)

            _send(device, client, b"W,Nope")
            assert got == []
            _, _, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 1 and reason == 4  # BAD_SELECT
        finally:
            client.close()
            device.close()

    def test_text_percent_decoded(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_text_command("LBL", max_length=32)
            def _h(v):
                got.append(v)

            _send(device, client, b"LBL,hello%20world%2C%20ok")
            assert got == ["hello world, ok"]
            _, h, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 0 and reason == 0
            # ack hashes the encoded wire payload, not the decoded value
            assert h == fnv1a32("LBL,hello%20world%2C%20ok")
        finally:
            client.close()
            device.close()

    def test_text_too_long_rejected(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_text_command("LBL", max_length=5)
            def _h(v):
                got.append(v)

            _send(device, client, b"LBL,toolong")
            assert got == []
            _, _, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 1 and reason == 5  # TOO_LONG
        finally:
            client.close()
            device.close()

    def test_button_always_accepted(self):
        device, client = _make_device_with_client()
        calls = []
        try:
            @device.on_button_command("PING")
            def _h():
                calls.append(True)

            _send(device, client, b"PING")
            assert calls == [True]
            _, h, status, reason = _parse_ack(_read_frame(client, b"\xf0"))
            assert status == 0 and reason == 0
            assert h == fnv1a32("PING")
        finally:
            client.close()
            device.close()

    def test_plain_command_not_acked(self):
        device, client = _make_device_with_client()
        got = []
        try:
            @device.on_command("LEGACY")
            def _h(*p):
                got.append(p)

            _send(device, client, b"LEGACY,1")
            assert got == [("1",)]
            with pytest.raises(AssertionError):
                _read_frame(client, b"\xf0")
        finally:
            client.close()
            device.close()
