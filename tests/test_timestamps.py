"""Tests for TimestampMode enum, timestamp properties, and timestamps in data frames."""

import time

import pytest

from blaecktcpy import TimestampMode, BlaeckTCPy, SignalList
from conftest import _make_server_on_free_port, _start_retry


class TestTimestampModeEnum:
    """Verify TimestampMode enum values."""

    def test_values(self):
        assert TimestampMode.NONE == 0
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

    def test_set_timestamp_mode_by_int(self):
        device = _make_server_on_free_port()
        device.timestamp_mode = 2
        assert device.timestamp_mode == TimestampMode.UNIX

    def test_invalid_timestamp_mode_raises_with_valid_modes(self):
        device = _make_server_on_free_port()
        with pytest.raises(ValueError, match=r"Invalid timestamp_mode.*Valid modes"):
            device.timestamp_mode = 99

    def test_invalid_timestamp_mode_string_raises(self):
        device = _make_server_on_free_port()
        with pytest.raises((ValueError, TypeError)):
            device.timestamp_mode = "UNIX"

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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            now_us = int(time.time() * 1_000_000)
            assert abs(ts - now_us) < 5_000_000  # within 5 seconds
        finally:
            client.close()
            device.close()

    def test_micros_mode_raises_valueerror(self):
        """Setting mode=1 (MICROS protocol value) should raise ValueError."""
        device, client = self._make_device()
        try:
            with pytest.raises(ValueError, match="Invalid timestamp_mode"):
                device.timestamp_mode = 1
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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
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
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
            assert ts_mode_section[0] == 0x02
        finally:
            client.close()
            device.close()


class TestTimedWriteTimestamps:
    """Verify timed_write_* methods handle explicit timestamps correctly."""

    def _make_device(self):
        import socket

        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 3.14)
        device.timestamp_mode = TimestampMode.UNIX
        device.local_interval_ms = 10
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()
        device._set_timed_data(True, 10)

        return device, client

    def _recv_frame(self, client):
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

    def test_timed_write_all_data_with_explicit_timestamp(self):
        """timed_write_all_data should use explicit unix_timestamp."""
        device, client = self._make_device()
        try:
            time.sleep(0.02)
            explicit_ts = 9999999999_000000
            device.timed_write_all_data(unix_timestamp=explicit_ts)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
            assert ts_mode_section[0] == 0x02
            ts = int.from_bytes(ts_mode_section[1:9], "little")
            assert ts == explicit_ts
        finally:
            client.close()
            device.close()

    def test_timed_write_updated_data_with_explicit_timestamp(self):
        """timed_write_updated_data should use explicit unix_timestamp."""
        device, client = self._make_device()
        try:
            time.sleep(0.02)
            explicit_ts = 8888888888_000000
            device.update("temp", 42.0)
            device.timed_write_updated_data(unix_timestamp=explicit_ts)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
            assert ts_mode_section[0] == 0x02
            ts = int.from_bytes(ts_mode_section[1:9], "little")
            assert ts == explicit_ts
        finally:
            client.close()
            device.close()

    def test_timed_write_all_data_auto_timestamp(self):
        """timed_write_all_data without explicit timestamp should auto-generate."""
        device, client = self._make_device()
        try:
            time.sleep(0.02)
            before = time.time_ns() // 1_000
            device.timed_write_all_data()
            after = time.time_ns() // 1_000
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 5)
            ts_mode_section = parts[4]
            assert ts_mode_section[0] == 0x02
            ts = int.from_bytes(ts_mode_section[1:9], "little")
            assert before <= ts <= after
        finally:
            client.close()
            device.close()


class TestHubTimestampRelay:
    """Verify hub relay timestamp pass-through and dropping."""

    def _build_d2_frame(self, signal_values, schema_hash=0,
                        timestamp_mode=0, timestamp=None):
        """Build a raw D2 data frame for injection into FakeTransport."""
        import binascii
        import struct

        msg_key = b"\xd2"
        msg_id = (1).to_bytes(4, "little")
        flag = b"\x00"
        hash_bytes = schema_hash.to_bytes(2, "little")

        if timestamp is not None and timestamp_mode != 0:
            mode_byte = int(timestamp_mode).to_bytes(1, "little")
            meta = (flag + b":" + hash_bytes + b":"
                    + mode_byte + timestamp.to_bytes(8, "little") + b":")
        else:
            meta = flag + b":" + hash_bytes + b":" + b"\x00" + b":"

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        status = b"\x00" + b"\x00\x00\x00\x00"
        # content = everything between <BLAECK: and /BLAECK>
        content_no_crc = msg_key + b":" + msg_id + b":" + meta + payload + status
        crc = binascii.crc32(content_no_crc).to_bytes(4, "little")
        return content_no_crc + crc

    def _make_hub_with_upstream(self, *, local_signals=0, num_upstreams=1):
        """Create a hub with fake upstream(s), optionally with local signals."""
        import socket
        from blaecktcpy import Signal
        from blaecktcpy._server import UpstreamDevice
        from blaecktcpy.hub import _decoder as decoder
        from conftest import FakeTransport

        hub = _make_server_on_free_port()
        for i in range(local_signals):
            hub.add_signal(f"local_{i}", "float")
        _start_retry(hub)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", hub._port))
        hub._accept_new_clients()

        transports = []
        for i in range(num_upstreams):
            transport = FakeTransport(f"upstream_{i}")
            upstream = UpstreamDevice(
                device_name=f"Upstream_{i}",
                transport=transport,
                relay_downstream=True,
                symbol_table=[
                    decoder.DecodedSymbol("sig", 8, "float", 4),
                ],
            )
            offset = len(hub.signals)
            sig = Signal(f"up{i}_sig", "float")
            hub.signals.append(sig)
            upstream._signals.append(hub.signals[offset])
            upstream.index_map = {0: offset}
            upstream._upstream_signals = SignalList(upstream._signals)
            hub._hub._upstreams.append(upstream)
            transports.append(transport)

        return hub, client, transports

    def _recv_frame(self, client):
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

    def test_single_upstream_no_local_passes_timestamp(self):
        """Single relayed upstream, no local signals: timestamp passed through."""
        hub, client, transports = self._make_hub_with_upstream(
            local_signals=0, num_upstreams=1
        )
        try:
            explicit_ts = 1700000000_000000
            frame = self._build_d2_frame(
                [25.0], timestamp_mode=2, timestamp=explicit_ts
            )
            transports[0].inject_frame(frame)
            hub._poll_upstreams()

            relay = self._recv_frame(client)
            start = relay.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = relay[start:]
            parts = content.split(b":", 5)
            ts_section = parts[4]
            assert ts_section[0] == 0x02
            ts = int.from_bytes(ts_section[1:9], "little")
            assert ts == explicit_ts
        finally:
            client.close()
            hub.close()

    def test_multi_upstream_drops_timestamp(self):
        """Multiple relayed upstreams: timestamps are dropped."""
        hub, client, transports = self._make_hub_with_upstream(
            local_signals=0, num_upstreams=2
        )
        try:
            explicit_ts = 1700000000_000000
            frame = self._build_d2_frame(
                [25.0], timestamp_mode=2, timestamp=explicit_ts
            )
            transports[0].inject_frame(frame)
            hub._poll_upstreams()

            relay = self._recv_frame(client)
            start = relay.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = relay[start:]
            parts = content.split(b":", 5)
            ts_section = parts[4]
            assert ts_section[0] == 0x00  # mode NONE — timestamp dropped
        finally:
            client.close()
            hub.close()

    def test_single_upstream_with_local_drops_timestamp(self):
        """Single upstream + local signals: timestamps are dropped."""
        hub, client, transports = self._make_hub_with_upstream(
            local_signals=1, num_upstreams=1
        )
        try:
            explicit_ts = 1700000000_000000
            frame = self._build_d2_frame(
                [25.0], timestamp_mode=2, timestamp=explicit_ts
            )
            transports[0].inject_frame(frame)
            hub._poll_upstreams()

            relay = self._recv_frame(client)
            start = relay.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = relay[start:]
            parts = content.split(b":", 5)
            ts_section = parts[4]
            assert ts_section[0] == 0x00  # mode NONE — timestamp dropped
        finally:
            client.close()
            hub.close()

    def test_ts_drop_warning_fires_once(self):
        """Runtime warning fires once per upstream when timestamps are dropped."""
        import logging

        hub, client, transports = self._make_hub_with_upstream(
            local_signals=0, num_upstreams=2
        )
        try:
            warnings = []
            handler = logging.Handler()
            handler.emit = lambda record: warnings.append(record.getMessage())
            hub._logger.addHandler(handler)

            frame = self._build_d2_frame(
                [25.0], timestamp_mode=2, timestamp=1700000000_000000
            )
            # Inject twice from same upstream
            transports[0].inject_frame(frame)
            hub._poll_upstreams()
            transports[0].inject_frame(frame)
            hub._poll_upstreams()

            ts_warnings = [w for w in warnings if "timestamps" in w.lower()]
            assert len(ts_warnings) == 1  # only once
            assert "not forwarded" in ts_warnings[0]
        finally:
            client.close()
            hub.close()

    def test_no_timestamp_no_warning(self):
        """No warning when upstream doesn't send timestamps."""
        import logging

        hub, client, transports = self._make_hub_with_upstream(
            local_signals=0, num_upstreams=2
        )
        try:
            warnings = []
            handler = logging.Handler()
            handler.emit = lambda record: warnings.append(record.getMessage())
            hub._logger.addHandler(handler)

            frame = self._build_d2_frame([25.0], timestamp_mode=0)
            transports[0].inject_frame(frame)
            hub._poll_upstreams()

            ts_warnings = [w for w in warnings if "timestamps" in w.lower()]
            assert len(ts_warnings) == 0
        finally:
            client.close()
            hub.close()


class TestHubTimestampStartValidation:
    """Verify start() refuses UNIX timestamps with mixed sources."""

    def test_unix_mode_with_upstream_and_local_raises(self):
        """UNIX mode + relayed upstream + local signals → ValueError at start()."""
        from blaecktcpy._server import UpstreamDevice
        from conftest import FakeTransport

        hub = _make_server_on_free_port()
        hub.add_signal("local", "float")
        hub.timestamp_mode = TimestampMode.UNIX
        hub._hub._upstreams.append(
            UpstreamDevice(
                device_name="Arduino",
                transport=FakeTransport("Arduino"),
                relay_downstream=True,
            )
        )
        with pytest.raises(ValueError, match="not supported"):
            hub.start()
        hub.close()

    def test_none_mode_with_upstream_and_local_ok(self):
        """NONE mode + relayed upstream + local signals → no error."""
        from blaecktcpy._server import UpstreamDevice
        from conftest import FakeTransport

        hub = _make_server_on_free_port()
        hub.add_signal("local", "float")
        hub._hub._upstreams.append(
            UpstreamDevice(
                device_name="Arduino",
                transport=FakeTransport("Arduino"),
                relay_downstream=True,
            )
        )
        try:
            _start_retry(hub)
        finally:
            hub.close()

    def test_unix_mode_no_upstreams_ok(self):
        """UNIX mode with no upstreams (pure server) → no error."""
        hub = _make_server_on_free_port()
        hub.add_signal("local", "float")
        hub.timestamp_mode = TimestampMode.UNIX
        try:
            _start_retry(hub)
        finally:
            hub.close()

    def test_unix_mode_non_relayed_upstream_ok(self):
        """UNIX mode + non-relayed upstream + local signals → no error."""
        from blaecktcpy._server import UpstreamDevice
        from conftest import FakeTransport

        hub = _make_server_on_free_port()
        hub.add_signal("local", "float")
        hub.timestamp_mode = TimestampMode.UNIX
        hub._hub._upstreams.append(
            UpstreamDevice(
                device_name="Arduino",
                transport=FakeTransport("Arduino"),
                relay_downstream=False,
            )
        )
        try:
            _start_retry(hub)
        finally:
            hub.close()


class TestRequireStarted:
    """Verify public methods raise RuntimeError before start()."""

    def test_tick_before_start(self):
        server = _make_server_on_free_port()
        server.add_signal("x", "float")
        with pytest.raises(RuntimeError, match="not started"):
            server.tick()

    def test_read_before_start(self):
        server = _make_server_on_free_port()
        with pytest.raises(RuntimeError, match="not started"):
            server.read()

    def test_write_before_start(self):
        server = _make_server_on_free_port()
        server.add_signal("x", "float")
        with pytest.raises(RuntimeError, match="not started"):
            server.write("x", 1.0)

    def test_write_all_data_before_start(self):
        server = _make_server_on_free_port()
        server.add_signal("x", "float")
        with pytest.raises(RuntimeError, match="not started"):
            server.write_all_data()

    def test_timed_write_all_data_before_start(self):
        server = _make_server_on_free_port()
        server.add_signal("x", "float")
        with pytest.raises(RuntimeError, match="not started"):
            server.timed_write_all_data()

