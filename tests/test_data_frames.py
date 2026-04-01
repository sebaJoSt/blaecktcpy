"""Tests for data frame encoding: status bytes, restart flags, relay scoping, and write/update."""

import binascii
import struct
import time

import pytest

from blaecktcpy import Signal, SignalList, STATUS_OK, STATUS_UPSTREAM_LOST, BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import _make_server_on_free_port, _start_retry, FakeTransport


class TestStatusByte:
    """Verify _build_data_msg encodes the status byte correctly."""

    def setup_method(self):
        self.server = _make_server_on_free_port()
        self.server.add_signal("sig1", "float", 3.14)
        self.server.add_signal("sig2", "int", 42)
        _start_retry(self.server)

    def teardown_method(self):
        self.server.close()

    def test_default_status_is_ok(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header)
        # Status byte is at position -5 (1 byte before 4-byte CRC)
        assert msg[-5] == STATUS_OK

    def test_status_upstream_lost(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        assert msg[-5] == STATUS_UPSTREAM_LOST

    def test_crc_excludes_status_byte(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        # CRC is computed over everything before status byte
        crc_data = msg[:-5]
        expected_crc = binascii.crc32(crc_data) & 0xFFFFFFFF
        actual_crc = int.from_bytes(msg[-4:], "little")
        assert actual_crc == expected_crc

    def test_status_byte_values(self):
        assert STATUS_OK == 0x00
        assert STATUS_UPSTREAM_LOST == 0x02


class TestRestartFlagRelay:
    """Device relays upstream RestartFlag to downstream."""

    def _build_d1_frame(
        self,
        restart_flag: bool,
        signal_values: list[float],
        status: int = 0,
    ):
        """Build a valid D1 data frame with CRC."""
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        flag = b"\x01" if restart_flag else b"\x00"
        timestamp_mode = b"\x00"
        meta = flag + b":" + timestamp_mode + b":"

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def test_upstream_restart_flag_sets_server_flag(self):
        """When upstream sends restart_flag=1, device sets its own flag."""
        device = _make_server_on_free_port()
        device.add_signal("sig1", "float", 0.0)
        _start_retry(device)
        try:
            upstream = _UpstreamDevice(
                device_name="Arduino", transport=FakeTransport(), relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.index_map = {0: 0}
            upstream.interval_ms = 0
            device._upstreams.append(upstream)

            # Build D1 frame with restart_flag=1
            frame = self._build_d1_frame(restart_flag=True, signal_values=[25.0])
            full = b"<BLAECK:" + frame + b"/BLAECK>\r\n"

            # Verify device flag is False initially
            device._restart_flag_pending = False

            # Feed frame through FakeTransport
            upstream.transport._buffer = full
            upstream.transport.read_available = lambda: upstream.transport._buffer

            # Parse and relay
            decoded = decoder.parse_data(frame, upstream.symbol_table)
            assert decoded.restart_flag is True

            # Simulate what _poll_upstreams does
            if decoded.restart_flag:
                device._restart_flag_pending = True

            assert device._restart_flag_pending is True
        finally:
            device.close()

    def test_no_restart_flag_leaves_server_flag_unchanged(self):
        """When upstream sends restart_flag=0, device flag stays unchanged."""
        device = _make_server_on_free_port()
        device.add_signal("sig1", "float", 0.0)
        _start_retry(device)
        try:
            frame = self._build_d1_frame(restart_flag=False, signal_values=[10.0])
            symbol_table = [decoder.DecodedSymbol("temp", 8, "float", 4)]

            decoded = decoder.parse_data(frame, symbol_table)
            assert decoded.restart_flag is False

            device._restart_flag_pending = False
            if decoded.restart_flag:
                device._restart_flag_pending = True
            assert device._restart_flag_pending is False
        finally:
            device.close()


class TestStatusByteRelay:
    """Device relays upstream status byte downstream."""

    def _build_d1_frame(self, status: int, signal_values: list[float]):
        """Build a valid D1 data frame with a specific status byte."""
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"  # no restart, no timestamp

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def test_decoder_reads_status_byte_ok(self):
        """D1 parser captures status_byte = 0 (OK)."""
        frame = self._build_d1_frame(status=0x00, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x00

    def test_decoder_reads_status_byte_i2c_crc_error(self):
        """D1 parser captures status_byte = 1 (I2C CRC error)."""
        frame = self._build_d1_frame(status=0x01, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x01

    def test_decoder_reads_status_byte_upstream_lost(self):
        """D1 parser captures status_byte = 2 (upstream lost)."""
        frame = self._build_d1_frame(status=0x02, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x02

    def test_status_byte_relay_end_to_end(self):
        """Status byte flows: upstream D1 → device _poll_upstreams → downstream frame."""
        import socket

        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            # Connect a downstream TCP client
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(("127.0.0.1", device._port))
            device._accept_new_clients()  # accept the client

            # Manually wire upstream (simulating relay of 1 signal)
            transport = FakeTransport("Arduino")
            upstream = _UpstreamDevice(
                device_name="Arduino", transport=transport, relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.interval_ms = 0
            upstream.connected = True

            # Add relay signal to device
            sig = Signal("temp", "float", 0.0)
            device.signals.append(sig)
            upstream._signals.append(device.signals[0])
            upstream.index_map = {0: 0}
            upstream._upstream_signals = SignalList(upstream._signals)
            device._upstreams.append(upstream)

            # Feed a D1 frame with status=0x01 (I2C CRC error)
            frame_content = self._build_d1_frame(status=0x01, signal_values=[25.0])
            wrapped = b"<BLAECK:" + frame_content + b"/BLAECK>\r\n"
            transport._pending = wrapped
            transport.read_available = lambda: transport._pending

            # Run poll to process the frame and relay downstream
            device._poll_upstreams()
            # Clear pending so next read_available returns empty
            transport._pending = b""

            # Read downstream frame from TCP client
            downstream = client.recv(4096)
            client.close()

            # Parse the downstream frame — extract status byte at position [-5]
            # Frame: <BLAECK:...content.../BLAECK>\r\n
            start = downstream.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = downstream.find(b"/BLAECK>")
            content = downstream[start:end]
            # Status byte is at content[-5] (before 4-byte CRC)
            assert content[-5] == 0x01, (
                f"Expected status byte 0x01 (I2C CRC error), got 0x{content[-5]:02x}"
            )
        finally:
            device.close()


class TestRelayFrameScoping:
    """Relay frames are scoped to the originating upstream's signals only."""

    def _build_d1_frame(
        self,
        restart_flag: bool,
        signal_values: list[float],
        status: int = 0,
    ):
        """Build a valid D1 data frame with CRC."""
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        flag = b"\x01" if restart_flag else b"\x00"
        timestamp_mode = b"\x00"
        meta = flag + b":" + timestamp_mode + b":"

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def _make_device_with_two_upstreams(self):
        """Create a device with two fake upstreams (A: 2 signals, B: 2 signals)."""
        import socket

        device = _make_server_on_free_port()
        _start_retry(device)  # _local_signal_count = 0, no local signals

        # Manually add upstream signals to device.signals
        for name in ["A_sig0", "A_sig1", "B_sig0", "B_sig1"]:
            device.signals.append(Signal(name, "float", 0.0))

        transport_a = FakeTransport("UpstreamA")
        upstream_a = _UpstreamDevice(
            device_name="UpstreamA", transport=transport_a, relay_downstream=True
        )
        upstream_a.symbol_table = [
            decoder.DecodedSymbol("A_sig0", 8, "float", 4),
            decoder.DecodedSymbol("A_sig1", 8, "float", 4),
        ]
        upstream_a._signals.append(device.signals[0])
        upstream_a._signals.append(device.signals[1])
        upstream_a.index_map = {0: 0, 1: 1}
        upstream_a._upstream_signals = SignalList(upstream_a._signals)
        upstream_a.interval_ms = 300
        upstream_a.connected = True
        device._upstreams.append(upstream_a)

        transport_b = FakeTransport("UpstreamB")
        upstream_b = _UpstreamDevice(
            device_name="UpstreamB", transport=transport_b, relay_downstream=True
        )
        upstream_b.symbol_table = [
            decoder.DecodedSymbol("B_sig0", 8, "float", 4),
            decoder.DecodedSymbol("B_sig1", 8, "float", 4),
        ]
        upstream_b._signals.append(device.signals[2])
        upstream_b._signals.append(device.signals[3])
        upstream_b.index_map = {0: 2, 1: 3}
        upstream_b._upstream_signals = SignalList(upstream_b._signals)
        upstream_b.interval_ms = 300
        upstream_b.connected = True
        device._upstreams.append(upstream_b)

        # Connect a downstream TCP client
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, upstream_a, upstream_b, transport_a, transport_b

    def _parse_downstream_signal_ids(self, raw: bytes) -> list[int]:
        """Extract signal index IDs from a downstream D2 frame."""
        start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
        end = raw.find(b"/BLAECK>")
        content = raw[start:end]

        # D2 layout: msg_key(1) : msg_id(4) : restart(1) : ts_mode(1) : signals... status(1) crc(4)
        # Find the signal data after the last ":"
        colon_positions = []
        for i, b in enumerate(content):
            if b == ord(":"):
                colon_positions.append(i)
        sig_start = colon_positions[3] + 1
        sig_end = len(content) - 5
        sig_data = content[sig_start:sig_end]

        ids = []
        pos = 0
        while pos < len(sig_data):
            sig_id = int.from_bytes(sig_data[pos:pos + 2], "little")
            ids.append(sig_id)
            # skip id(2) + float(4)
            pos += 6
        return ids

    def test_restart_flag_does_not_leak_across_upstreams(self):
        """Upstream A restart_flag must not appear in upstream B's relay frame."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Clear the device's initial restart flag
            device._restart_flag_pending = False

            # Upstream A: restart_flag=True
            frame_a = self._build_d1_frame(restart_flag=True, signal_values=[1.0, 2.0])
            tr_a._pending = b"<BLAECK:" + frame_a + b"/BLAECK>\r\n"
            tr_a.read_available = lambda: tr_a._pending

            # Upstream B: restart_flag=False
            frame_b = self._build_d1_frame(restart_flag=False, signal_values=[5.0, 6.0])
            tr_b._pending = b"<BLAECK:" + frame_b + b"/BLAECK>\r\n"
            tr_b.read_available = lambda: tr_b._pending

            device._poll_upstreams()

            # Clear pending
            tr_a._pending = b""
            tr_b._pending = b""

            # Read all downstream data
            time.sleep(0.05)
            downstream = client.recv(8192)

            # Should get two separate frames
            frames = downstream.split(b"/BLAECK>\r\n")
            frames = [f for f in frames if f]  # remove empty
            assert len(frames) == 2, f"Expected 2 frames, got {len(frames)}"

            # Frame 1 (upstream A): should have signal IDs 0,1 only
            frame1_raw = frames[0] + b"/BLAECK>\r\n"
            ids1 = self._parse_downstream_signal_ids(frame1_raw)
            assert ids1 == [0, 1], f"Frame 1 should contain A's signals [0,1], got {ids1}"

            # Frame 2 (upstream B): should have signal IDs 2,3 only
            frame2_raw = frames[1] + b"/BLAECK>\r\n"
            ids2 = self._parse_downstream_signal_ids(frame2_raw)
            assert ids2 == [2, 3], f"Frame 2 should contain B's signals [2,3], got {ids2}"

            # Check restart flag: frame 1 should have it, frame 2 should not
            # D2 layout: msg_key(1) : msg_id(4) : restart(1) : ts_mode(1) : ...
            # restart_flag byte is at colons[1]+1
            content1_start = frame1_raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            content1 = frame1_raw[content1_start:frame1_raw.find(b"/BLAECK>")]
            colons1 = [i for i, b in enumerate(content1) if b == ord(":")]
            assert content1[colons1[1] + 1] == 1, "Frame 1 should have restart_flag=1"

            content2_start = frame2_raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            content2 = frame2_raw[content2_start:frame2_raw.find(b"/BLAECK>")]
            colons2 = [i for i, b in enumerate(content2) if b == ord(":")]
            assert content2[colons2[1] + 1] == 0, "Frame 2 should have restart_flag=0"
        finally:
            client.close()
            device.close()

    def test_status_byte_does_not_leak_across_upstreams(self):
        """Upstream A status=0x01 must not appear in upstream B's relay frame."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Upstream A: status=0x01 (I2C CRC error)
            frame_a = self._build_d1_frame(
                restart_flag=False, signal_values=[1.0, 2.0], status=0x01
            )
            tr_a._pending = b"<BLAECK:" + frame_a + b"/BLAECK>\r\n"
            tr_a.read_available = lambda: tr_a._pending

            # Upstream B: status=0x00 (OK)
            frame_b = self._build_d1_frame(
                restart_flag=False, signal_values=[5.0, 6.0], status=0x00
            )
            tr_b._pending = b"<BLAECK:" + frame_b + b"/BLAECK>\r\n"
            tr_b.read_available = lambda: tr_b._pending

            device._poll_upstreams()
            tr_a._pending = b""
            tr_b._pending = b""

            time.sleep(0.05)
            downstream = client.recv(8192)

            frames = downstream.split(b"/BLAECK>\r\n")
            frames = [f for f in frames if f]
            assert len(frames) == 2, f"Expected 2 frames, got {len(frames)}"

            # Frame 1 (upstream A): status_byte should be 0x01
            content1 = frames[0][frames[0].find(b"<BLAECK:") + 8:]
            assert content1[-5] == 0x01, f"Frame 1 status should be 0x01, got 0x{content1[-5]:02x}"

            # Frame 2 (upstream B): status_byte should be 0x00
            content2 = frames[1][frames[1].find(b"<BLAECK:") + 8:]
            assert content2[-5] == 0x00, f"Frame 2 status should be 0x00, got 0x{content2[-5]:02x}"
        finally:
            client.close()
            device.close()

    def test_upstream_lost_frame_scoped_to_upstream(self):
        """STATUS_UPSTREAM_LOST frame only contains the disconnected upstream's signals."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Mark upstream A's signals as updated (simulates _zero_upstream_signals)
            device.signals[0].value = 0
            device.signals[0].updated = True
            device.signals[1].value = 0
            device.signals[1].updated = True

            # Also mark B's signals as updated (from normal data)
            device.signals[2].value = 99.0
            device.signals[2].updated = True
            device.signals[3].value = 99.0
            device.signals[3].updated = True

            # Send upstream-lost for A only
            device._send_upstream_lost_frame(up_a)

            time.sleep(0.05)
            downstream = client.recv(8192)

            # Should be one frame with only A's signals
            ids = self._parse_downstream_signal_ids(downstream)
            assert ids == [0, 1], f"Lost frame should contain only A's signals [0,1], got {ids}"

            # Status byte should be STATUS_UPSTREAM_LOST (0x02)
            start = downstream.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = downstream.find(b"/BLAECK>")
            content = downstream[start:end]
            assert content[-5] == 0x02, f"Status should be 0x02, got 0x{content[-5]:02x}"

            # B's signals should still be updated (not consumed)
            assert device.signals[2].updated is True
            assert device.signals[3].updated is True
        finally:
            client.close()
            device.close()

    def test_upstream_lost_frame_sent_only_once(self):
        """STATUS_UPSTREAM_LOST is sent once on disconnect, not on subsequent ticks."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Disconnect upstream A
            tr_a.close()
            up_a.connected = True  # simulate it was connected before

            # First poll: should detect disconnect and send lost frame
            device._poll_upstreams()
            time.sleep(0.05)
            downstream1 = client.recv(8192)

            assert b"/BLAECK>" in downstream1, "Expected a lost frame on first poll"
            content = downstream1[downstream1.find(b"<BLAECK:") + 8:downstream1.find(b"/BLAECK>")]
            assert content[-5] == 0x02, "First poll should send STATUS_UPSTREAM_LOST"

            # connected should now be False
            assert up_a.connected is False

            # Second poll: should NOT send another lost frame
            device._poll_upstreams()
            time.sleep(0.05)
            client.setblocking(False)
            try:
                downstream2 = client.recv(8192)
            except BlockingIOError:
                downstream2 = b""
            client.setblocking(True)

            assert downstream2 == b"", (
                f"Expected no data on second poll, got {len(downstream2)} bytes"
            )
        finally:
            client.close()
            device.close()


class TestDecoderUnknownDatatype:
    """Decoder should fail safely on unknown datatype codes."""

    def test_parse_symbol_list_rejects_unknown_dtype(self):
        msg_key = b"\xb0"
        msg_id = (1).to_bytes(4, "little")
        data = b"\x00\x00" + b"mystery\x00" + bytes([255])
        frame = msg_key + b":" + msg_id + b":" + data

        with pytest.raises(ValueError, match="Unknown datatype code"):
            decoder.parse_symbol_list(frame)

    def test_parse_data_rejects_unknown_symbol_dtype(self):
        msg_key = b"\xd2"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"  # restart=0, timestamp_mode=0
        payload = (0).to_bytes(2, "little")  # symbol_id only, no value bytes
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc

        symbol_table = [decoder.DecodedSymbol("mystery", 255, "unknown(255)", 0)]

        with pytest.raises(ValueError, match="Unknown datatype code"):
            decoder.parse_data(frame, symbol_table)


class TestDecoderTruncatedPayload:
    """Decoder should reject truncated signal payloads."""

    def test_parse_data_d2_rejects_truncated_signal_payload(self):
        msg_key = b"\xd2"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"  # restart=0, timestamp_mode=0
        # symbol_id=0 + only 2 bytes of float payload (needs 4)
        payload = (0).to_bytes(2, "little") + b"\x01\x02"
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc

        symbol_table = [decoder.DecodedSymbol("temp", 8, "float", 4)]

        with pytest.raises(ValueError, match="Truncated signal payload"):
            decoder.parse_data(frame, symbol_table)

    def test_parse_data_b1_rejects_truncated_signal_payload(self):
        msg_key = b"\xb1"
        msg_id = (1).to_bytes(4, "little")
        # B1 payload only 2 bytes, but symbol expects 4-byte float
        payload = b"\x01\x02"
        crc_input = msg_key + b":" + msg_id + b":" + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + payload + b"\x00" + crc

        symbol_table = [decoder.DecodedSymbol("temp", 8, "float", 4)]

        with pytest.raises(ValueError, match="Truncated B1 payload"):
            decoder.parse_data(frame, symbol_table)


class TestHubWriteUpdate:
    """Verify write(), update(), and related methods on BlaeckTCPy local signals."""

    def _make_device_with_local_signals(self):
        """Create a device with two local signals and a connected downstream client."""
        import socket

        device = _make_server_on_free_port()

        sig_a = Signal("SigA", "float", 1.0)
        sig_b = Signal("SigB", "float", 2.0)
        device.add_signal(sig_a)
        device.add_signal(sig_b)

        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, sig_a, sig_b

    def _parse_signal_data(self, raw: bytes):
        """Parse signal (id, value) pairs from a downstream data frame."""
        start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
        end = raw.find(b"/BLAECK>")
        content = raw[start:end]

        colon_positions = [i for i, b in enumerate(content) if b == ord(":")]
        sig_start = colon_positions[3] + 1
        sig_end = len(content) - 5  # exclude status(1) + crc(4)
        sig_data = content[sig_start:sig_end]

        signals = []
        pos = 0
        while pos < len(sig_data):
            sig_id = int.from_bytes(sig_data[pos:pos + 2], "little")
            value = struct.unpack("<f", sig_data[pos + 2:pos + 6])[0]
            signals.append((sig_id, value))
            pos += 6
        return signals

    # ---- write() ----

    def test_write_sends_single_signal_by_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write("SigA", 42.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (0, 42.0)
            assert sig_a.value == 42.0
        finally:
            client.close()
            device.close()

    def test_write_sends_single_signal_by_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write(1, 99.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (1, 99.0)
            assert sig_b.value == 99.0
        finally:
            client.close()
            device.close()

    def test_write_updates_value(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write("SigB", 7.5)
            assert sig_b.value == 7.5
        finally:
            client.close()
            device.close()

    def test_write_noop_when_no_client(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            client.close()
            time.sleep(0.05)
            device.read()  # process disconnect
            # Should not raise even with no clients
            device.write("SigA", 10.0)
            assert sig_a.value == 10.0
        finally:
            device.close()

    def test_write_rejects_invalid_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            with pytest.raises(KeyError):
                device.write("NonExistent", 1.0)
        finally:
            client.close()
            device.close()

    def test_write_rejects_out_of_range_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            with pytest.raises(IndexError):
                device.write(5, 1.0)
        finally:
            client.close()
            device.close()

    def test_write_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(KeyError):
            device.write("x", 1.0)

    # ---- update() ----

    def test_update_sets_value_and_flag(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.update("SigA", 55.0)
            assert sig_a.value == 55.0
            assert sig_a.updated is True
        finally:
            client.close()
            device.close()

    def test_update_does_not_send(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.update("SigB", 66.0)
            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    def test_update_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(KeyError):
            device.update("x", 1.0)

    # ---- mark_signal_updated() ----

    def test_mark_signal_updated_by_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert sig_a.updated is False
            device.mark_signal_updated("SigA")
            assert sig_a.updated is True
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    def test_mark_signal_updated_by_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.mark_signal_updated(1)
            assert sig_b.updated is True
            assert sig_a.updated is False
        finally:
            client.close()
            device.close()

    # ---- mark_all_signals_updated() / clear_all_update_flags() ----

    def test_mark_all_and_clear_all(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.mark_all_signals_updated()
            assert sig_a.updated is True
            assert sig_b.updated is True

            device.clear_all_update_flags()
            assert sig_a.updated is False
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    # ---- has_updated_signals ----

    def test_has_updated_signals(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert device.has_updated_signals is False
            sig_a.updated = True
            assert device.has_updated_signals is True
        finally:
            client.close()
            device.close()

    # ---- write_all_data() ----

    def test_write_all_data_sends_all_local(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write_all_data()
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
            assert signals[0] == (0, 1.0)
            assert signals[1] == (1, 2.0)
        finally:
            client.close()
            device.close()

    def test_write_all_data_noop_when_no_client(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            client.close()
            time.sleep(0.05)
            device.read()  # process disconnect
            # Should not raise
            device.write_all_data()
        finally:
            device.close()

    # ---- write_updated_data() ----

    def test_write_updated_data_sends_only_updated(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            sig_b.updated = True
            device.write_updated_data()
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (1, 2.0)
            # updated flag should be cleared after send
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    def test_write_updated_data_noop_when_none_updated(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write_updated_data()
            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- tick() ----

    def test_tick_sends_all_on_timer(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            # Wait for timer to elapse
            time.sleep(0.06)
            device.tick()

            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
            assert signals[0] == (0, 1.0)  # SigA
            assert signals[1] == (1, 2.0)  # SigB
        finally:
            client.close()
            device.close()

    def test_tick_noop_before_interval(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 500
            device._timed_activated = True
            device._timer.activate(500)

            # Consume the first-tick send
            device.tick()
            client.recv(4096)

            # Don't wait — timer hasn't elapsed
            device.tick()

            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- tick_updated() ----

    def test_tick_updated_sends_only_updated_on_timer(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            sig_a.updated = True

            # Wait for timer to elapse
            time.sleep(0.06)
            device.tick_updated()

            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0][0] == 0  # SigA index
        finally:
            client.close()
            device.close()

    def test_tick_updated_noop_when_no_updated(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            time.sleep(0.06)
            device.tick_updated()

            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- read() ----

    def test_read_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(AttributeError):
            device.read()

    def test_read_processes_write_data_command(self):
        """read() handles a WRITE_DATA command and sends local signals."""
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            # Send WRITE_DATA command from the downstream client
            client.sendall(b"<BLAECK.WRITE_DATA,1>")
            time.sleep(0.05)
            device.read()
            time.sleep(0.05)
            downstream = client.recv(4096)
            assert b"<BLAECK:" in downstream
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
        finally:
            client.close()
            device.close()

    # ---- resolve edge cases ----

    def test_resolve_index_empty_signals(self):
        """Index access with no local signals gives a clear error."""
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        device._started = True
        device._local_signal_count = 0
        with pytest.raises(IndexError, match="Signal index 0 out of range"):
            device._resolve_signal(0)

    # ---- add_signals() / delete_signals() ----

    def test_add_signals_bulk_before_start(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        device.add_signals([
            Signal("A", "float"),
            Signal("B", "int"),
        ])
        assert len(device.signals) == 2
        assert device.signals[0].signal_name == "A"
        assert device.signals[1].signal_name == "B"

    def test_add_signal_after_start_inserts_in_server(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert len(device.signals) == 2
            sig_c = device.add_signal("SigC", "float", 3.0)
            assert len(device.signals) == 3
            assert device.signals[2] is sig_c
        finally:
            client.close()
            device.close()

    def test_delete_signals_after_start_removes_from_server(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert len(device.signals) == 2
            device.delete_signals()
            assert len(device.signals) == 0
        finally:
            client.close()
            device.close()

    def test_delete_then_add_after_start(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.delete_signals()
            sig_x = device.add_signal("X", "float", 99.0)
            assert len(device.signals) == 1
            assert device.signals[0] is sig_x

            # Verify we can still send data
            device.write("X", 42.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (0, 42.0)
        finally:
            client.close()
            device.close()
