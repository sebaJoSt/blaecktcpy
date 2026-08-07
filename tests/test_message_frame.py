"""Tests for the 0x90 Message frame: a named free-text status/log channel
broadcast from the device to every connected host.

The byte layout mirrors the BlaeckTCP firmware and is the exact contract
consumed by Loggbok's ParsingService.ProcessMessage:
    header: <BLAECK: 0x90 ':' msgId(4, LE) ':'
    payload: channelName(NUL-terminated) + textLen(2, LE) + text(UTF-8)
    footer: /BLAECK>\\r\\n
"""

import socket
import time

from blaecktcpy._encoder import MSG_MESSAGE, build_message
from conftest import _make_server_on_free_port, _start_retry


def _make_device_with_client():
    device = _make_server_on_free_port()
    _start_retry(device)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    client.connect(("127.0.0.1", device._port))
    device._accept_new_clients()
    return device, client


def _read_frame(client, msg_key):
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
        if start != -1 and end != -1:
            buf = buf[end + 10 :]
    raise AssertionError(f"no frame with key {msg_key!r} received (buf={buf!r})")


def _parse_message(frame):
    """Parse a 0x90 frame -> (msg_id, channel_name, text), like Loggbok."""
    assert frame[8:9] == MSG_MESSAGE
    msg_id = int.from_bytes(frame[10:14], "little")
    payload = frame[15:-10]  # after header(15), before footer(10)
    nul = payload.index(0)
    channel_name = payload[:nul].decode("utf-8")
    text_len = payload[nul + 1] | (payload[nul + 2] << 8)
    text = payload[nul + 3 : nul + 3 + text_len].decode("utf-8")
    return msg_id, channel_name, text


# ── encoder (pure byte layout) ───────────────────────────────────────────────
def test_build_message_byte_layout():
    frame = build_message(42, "status", "booting up")
    assert frame.startswith(b"<BLAECK:")
    assert frame.endswith(b"/BLAECK>\r\n")
    assert frame[8:9] == b"\x90"
    assert frame[9:10] == b":"
    assert frame[10:14] == (42).to_bytes(4, "little")
    assert frame[14:15] == b":"
    msg_id, channel, text = _parse_message(frame)
    assert (msg_id, channel, text) == (42, "status", "booting up")


def test_build_message_empty_text():
    _, channel, text = _parse_message(build_message(0, "log", ""))
    assert channel == "log"
    assert text == ""


def test_build_message_utf8_round_trip():
    _, channel, text = _parse_message(build_message(1, "status", "Grüße 🚀"))
    assert channel == "status"
    assert text == "Grüße 🚀"


def test_build_message_caps_text_at_65535_bytes():
    long_text = "x" * 70000
    _, _, text = _parse_message(build_message(0, "big", long_text))
    assert len(text) == 65535


# ── server broadcast ─────────────────────────────────────────────────────────
def test_write_message_broadcasts_frame():
    device, client = _make_device_with_client()
    try:
        device.write_message("status", "hello")
        msg_id, channel, text = _parse_message(_read_frame(client, b"\x90"))
        assert (channel, text) == ("status", "hello")
        assert msg_id == 0
    finally:
        client.close()
        device.close()


def test_write_message_auto_increments_msg_id():
    device, client = _make_device_with_client()
    try:
        device.write_message("ch", "a")
        first = _parse_message(_read_frame(client, b"\x90"))[0]
        device.write_message("ch", "b")
        second = _parse_message(_read_frame(client, b"\x90"))[0]
        assert first == 0
        assert second == 1
    finally:
        client.close()
        device.close()


def test_write_message_explicit_msg_id_does_not_increment():
    device, client = _make_device_with_client()
    try:
        device.write_message("ch", "a", msg_id=99)
        assert _parse_message(_read_frame(client, b"\x90"))[0] == 99
        device.write_message("ch", "b")
        assert _parse_message(_read_frame(client, b"\x90"))[0] == 0
    finally:
        client.close()
        device.close()
