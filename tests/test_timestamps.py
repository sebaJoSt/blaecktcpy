"""Tests for TimestampMode enum, timestamp properties, and timestamps in data frames."""

import time

import pytest

from blaecktcpy import TimestampMode, BlaeckTCPy
from conftest import _make_server_on_free_port, _start_retry


class TestTimestampModeEnum:
    """Verify TimestampMode enum values."""

    def test_values(self):
        assert TimestampMode.NONE == 0
        assert TimestampMode.MICROS == 1
        assert TimestampMode.UNIX == 2

    def test_is_int(self):
        assert isinstance(TimestampMode.NONE, int)


class TestTimestampProperties:
    """Verify timestamp_mode, start_time properties."""

    def test_default_timestamp_mode_is_none(self):
        device = _make_server_on_free_port()
        assert device.timestamp_mode == TimestampMode.NONE

    def test_set_timestamp_mode(self):
        device = _make_server_on_free_port()
        device.timestamp_mode = TimestampMode.UNIX
        assert device.timestamp_mode == TimestampMode.UNIX

    def test_start_time_set_at_start(self):
        device = _make_server_on_free_port()
        before = time.time()
        _start_retry(device)
        after = time.time()
        try:
            assert before <= device.start_time <= after
        finally:
            device.close()

    def test_start_time_zero_before_start(self):
        device = _make_server_on_free_port()
        assert device.start_time == 0.0


class TestTimestampInDataFrames:
    """Verify timestamps are encoded in outgoing data frames."""

    def _make_device(self):
        import socket

        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 3.14)
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client

    def _recv_frame(self, client):
        """Receive one full BlaeckTCP frame."""
        time.sleep(0.05)
        data = b""
        while True:
            try:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"/BLAECK>" in data:
                    break
            except Exception:
                break
        return data

    def test_no_timestamp_mode_sends_mode_zero(self):
        """Default NO_TIMESTAMP mode should send mode byte 0x00."""
        device, client = self._make_device()
        try:
            device.write_all_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x00
        finally:
            client.close()
            device.close()

    def test_rtc_mode_sends_8_byte_timestamp(self):
        """UNIX mode should include an 8-byte timestamp."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.UNIX
            device.write_all_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            now_us = int(time.time() * 1_000_000)
            assert abs(ts - now_us) < 5_000_000  # within 5 seconds
        finally:
            client.close()
            device.close()

    def test_micros_mode_sends_relative_timestamp(self):
        """Setting MICROS mode should raise ValueError."""
        device, client = self._make_device()
        try:
            with pytest.raises(ValueError, match="MICROS is not supported"):
                device.timestamp_mode = TimestampMode.MICROS
        finally:
            client.close()
            device.close()

    def test_explicit_timestamp_overrides_auto(self):
        """Explicit unix_timestamp (int µs) should override auto-generated value."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.UNIX
            explicit_ts = 1234567890_000000
            device.write_all_data(unix_timestamp=explicit_ts)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            assert ts == explicit_ts
        finally:
            client.close()
            device.close()

    def test_explicit_unix_timestamp_float_converts_to_us(self):
        """Explicit unix_timestamp (float seconds) should be converted to µs."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.UNIX
            device.write_all_data(unix_timestamp=1234567890.5)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            assert ts == 1234567890_500000
        finally:
            client.close()
            device.close()

    def test_unix_timestamp_rejected_in_none_mode(self):
        """unix_timestamp should raise ValueError in NONE mode."""
        device, client = self._make_device()
        try:
            assert device.timestamp_mode == TimestampMode.NONE
            with pytest.raises(ValueError, match="UNIX"):
                device.write_all_data(unix_timestamp=time.time())
        finally:
            client.close()
            device.close()

    def test_write_updated_data_includes_timestamp(self):
        """write_updated_data should also include timestamps."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.UNIX
            device.update("temp", 42.0)
            device.write_updated_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
        finally:
            client.close()
            device.close()

    def test_write_single_signal_includes_timestamp(self):
        """write() should also include timestamps."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.UNIX
            device.write("temp", 42.0)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
        finally:
            client.close()
            device.close()
