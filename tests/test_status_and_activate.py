"""Tests for I2C skip (StatusByte=1) relay, status_payload round-trip,
D2 decoder with missing signals, and _resend_activate on restart/reconnect."""

import binascii
import socket
import struct
import time

import pytest

from blaecktcpy import (
    Signal,
    SignalList,
    IntervalMode,
    STATUS_OK,
    STATUS_UPSTREAM_LOST,
    STATUS_UPSTREAM_RECONNECTED,
    BlaeckTCPy,
)
from blaecktcpy._server import _UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import (
    _make_server_on_free_port,
    _start_retry,
    FakeTransport,
    RecordingTransport,
)


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def _build_d2_frame(
    schema_hash: int,
    signal_pairs: list[tuple[int, bytes]],
    restart_flag: bool = False,
    status: int = 0x00,
    status_payload: bytes = b"\x00\x00\x00\x00",
):
    """Build a D2 frame with explicit status byte and payload."""
    msg_key = b"\xd2"
    msg_id = (1).to_bytes(4, "little")
    flag = b"\x01" if restart_flag else b"\x00"
    hash_bytes = schema_hash.to_bytes(2, "little")
    ts_mode = b"\x00"
    meta = flag + b":" + hash_bytes + b":" + ts_mode + b":"

    payload = b""
    for sym_id, raw in signal_pairs:
        payload += sym_id.to_bytes(2, "little") + raw

    frame_no_crc = (
        msg_key + b":" + msg_id + b":" + meta + payload
        + status.to_bytes(1, "little") + status_payload
    )
    crc = binascii.crc32(frame_no_crc).to_bytes(4, "little")
    return frame_no_crc + crc


def _build_d2_frame_floats(
    schema_hash: int,
    signal_values: list[float],
    restart_flag: bool = False,
    status: int = 0x00,
    status_payload: bytes = b"\x00\x00\x00\x00",
):
    """Convenience: build D2 with sequential float signals."""
    pairs = [(i, struct.pack("<f", v)) for i, v in enumerate(signal_values)]
    return _build_d2_frame(schema_hash, pairs, restart_flag, status, status_payload)


def _build_d2_frame_sparse(
    schema_hash: int,
    signal_map: dict[int, float],
    status: int = 0x00,
    status_payload: bytes = b"\x00\x00\x00\x00",
):
    """Build D2 with only a subset of signals (sparse SymbolIDs)."""
    pairs = [(sym_id, struct.pack("<f", v)) for sym_id, v in sorted(signal_map.items())]
    return _build_d2_frame(schema_hash, pairs, False, status, status_payload)


# ---------------------------------------------------------------------------
# D2 decoder: StatusByte=1 with missing signals
# ---------------------------------------------------------------------------

class TestD2DecoderMissingSignals:
    """D2 decoder correctly parses frames where I2C slaves are skipped."""

    def _make_symbol_table(self, count: int) -> list[decoder.DecodedSymbol]:
        return [decoder.DecodedSymbol(f"sig{i}", 8, "float", 4) for i in range(count)]

    def test_all_signals_present_status_ok(self):
        """Baseline: all 4 signals present, StatusByte=0."""
        table = self._make_symbol_table(4)
        schema_hash = decoder.compute_schema_hash([(s.name, s.datatype_code) for s in table])
        frame = _build_d2_frame_floats(schema_hash, [1.0, 2.0, 3.0, 4.0])
        decoded = decoder.parse_data(frame, table)
        assert decoded.status_byte == 0x00
        assert len(decoded.signals) == 4
        assert decoded.signals[0] == pytest.approx(1.0)
        assert decoded.signals[3] == pytest.approx(4.0)

    def test_missing_signals_status_i2c_skip(self):
        """StatusByte=1 with only signals 0,1 present (signals 2,3 from skipped slave)."""
        table = self._make_symbol_table(4)
        schema_hash = decoder.compute_schema_hash([(s.name, s.datatype_code) for s in table])
        frame = _build_d2_frame_sparse(
            schema_hash,
            {0: 10.0, 1: 20.0},
            status=0x01,
            status_payload=b"\x02\x08\x03\x00",  # 2 skipped, slaveID=8, reason=3
        )
        decoded = decoder.parse_data(frame, table)
        assert decoded.status_byte == 0x01
        assert decoded.status_payload == b"\x02\x08\x03\x00"
        assert len(decoded.signals) == 2
        assert 0 in decoded.signals
        assert 1 in decoded.signals
        assert 2 not in decoded.signals
        assert 3 not in decoded.signals
        assert decoded.signals[0] == pytest.approx(10.0)
        assert decoded.signals[1] == pytest.approx(20.0)

    def test_single_signal_present_others_skipped(self):
        """Only signal 0 present out of 3 (both slaves skipped)."""
        table = self._make_symbol_table(3)
        schema_hash = decoder.compute_schema_hash([(s.name, s.datatype_code) for s in table])
        frame = _build_d2_frame_sparse(schema_hash, {0: 42.0}, status=0x01)
        decoded = decoder.parse_data(frame, table)
        assert decoded.status_byte == 0x01
        assert len(decoded.signals) == 1
        assert decoded.signals[0] == pytest.approx(42.0)

    def test_non_contiguous_signals(self):
        """Signals 0 and 2 present, signal 1 missing (middle slave skipped)."""
        table = self._make_symbol_table(3)
        schema_hash = decoder.compute_schema_hash([(s.name, s.datatype_code) for s in table])
        frame = _build_d2_frame_sparse(schema_hash, {0: 1.5, 2: 3.5}, status=0x01)
        decoded = decoder.parse_data(frame, table)
        assert len(decoded.signals) == 2
        assert decoded.signals[0] == pytest.approx(1.5)
        assert decoded.signals[2] == pytest.approx(3.5)
        assert 1 not in decoded.signals

    def test_status_payload_round_trip(self):
        """StatusPayload bytes survive D2 decode → re-encode cycle."""
        table = self._make_symbol_table(2)
        schema_hash = decoder.compute_schema_hash([(s.name, s.datatype_code) for s in table])
        payload = b"\x01\x08\x02\x00"
        frame = _build_d2_frame_floats(
            schema_hash, [1.0, 2.0], status=0x01, status_payload=payload
        )
        decoded = decoder.parse_data(frame, table)
        assert decoded.status_byte == 0x01
        assert decoded.status_payload == payload


# ---------------------------------------------------------------------------
# Hub relay: StatusByte=1 with missing signals (D2 → D2)
# ---------------------------------------------------------------------------

class TestI2CSkipRelay:
    """Hub correctly relays D2 frames with StatusByte=1 and partial signals."""

    def _setup_hub_with_upstream(self, signal_names: list[str]):
        """Create a hub device with one upstream having the given signals."""
        device = _make_server_on_free_port()
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        transport = FakeTransport("MasterWithSlaves")
        upstream = _UpstreamDevice(
            device_name="MasterWithSlaves",
            transport=transport,
            relay_downstream=True,
        )
        upstream.symbol_table = [
            decoder.DecodedSymbol(name, 8, "float", 4) for name in signal_names
        ]
        upstream.interval_ms = 0
        upstream.connected = True
        upstream.expected_schema_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in upstream.symbol_table]
        )

        for i, name in enumerate(signal_names):
            sig = Signal(name, "float", 0.0)
            device.signals.append(sig)
            upstream._signals.append(device.signals[i])
            upstream.index_map[i] = i
        upstream._upstream_signals = SignalList(upstream._signals)
        device._hub._upstreams.append(upstream)

        return device, client, upstream, transport

    def _extract_content(self, raw: bytes) -> bytes:
        start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
        end = raw.find(b"/BLAECK>")
        return raw[start:end]

    def _extract_signal_ids(self, content: bytes) -> list[int]:
        """Extract SymbolIDs from a D2 downstream frame."""
        colons = [i for i, b in enumerate(content) if b == ord(":")]
        sig_start = colons[4] + 1
        sig_end = len(content) - 9  # status(1) + payload(4) + crc(4)
        sig_data = content[sig_start:sig_end]
        ids = []
        pos = 0
        while pos < len(sig_data):
            ids.append(int.from_bytes(sig_data[pos:pos + 2], "little"))
            pos += 6  # id(2) + float(4)
        return ids

    def test_status_byte_1_relayed_downstream(self):
        """D2 upstream with StatusByte=1 → hub relay → downstream has StatusByte=1."""
        device, client, upstream, transport = self._setup_hub_with_upstream(
            ["master_sig", "slave1_sig", "slave2_sig"]
        )
        try:
            table = upstream.symbol_table
            schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in table]
            )
            # Master signal present, slave signals missing
            frame = _build_d2_frame_sparse(
                schema_hash, {0: 25.0}, status=0x01,
                status_payload=b"\x02\x08\x03\x00",
            )
            transport._pending = b"<BLAECK:" + frame + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending

            device._poll_upstreams()
            transport._pending = b""

            downstream = client.recv(4096)
            content = self._extract_content(downstream)

            assert content[-9] == 0x01, f"Expected status 0x01, got 0x{content[-9]:02x}"
        finally:
            client.close()
            device.close()

    def test_status_payload_relayed_downstream(self):
        """D2 upstream StatusPayload bytes are forwarded to downstream relay."""
        device, client, upstream, transport = self._setup_hub_with_upstream(
            ["sig_a", "sig_b"]
        )
        try:
            table = upstream.symbol_table
            schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in table]
            )
            payload = b"\x01\x05\x02\x00"
            frame = _build_d2_frame_floats(
                schema_hash, [1.0, 2.0], status=0x01, status_payload=payload,
            )
            transport._pending = b"<BLAECK:" + frame + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending

            device._poll_upstreams()
            transport._pending = b""

            downstream = client.recv(4096)
            content = self._extract_content(downstream)

            assert content[-9] == 0x01
            assert content[-8:-4] == payload, (
                f"Expected payload {payload!r}, got {content[-8:-4]!r}"
            )
        finally:
            client.close()
            device.close()

    def test_only_present_signals_relayed(self):
        """When StatusByte=1, only present (updated) signals appear in downstream frame."""
        device, client, upstream, transport = self._setup_hub_with_upstream(
            ["master_temp", "slave1_temp", "slave2_temp"]
        )
        try:
            table = upstream.symbol_table
            schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in table]
            )
            # Only signal 0 present (slaves skipped)
            frame = _build_d2_frame_sparse(
                schema_hash, {0: 22.5}, status=0x01,
            )
            transport._pending = b"<BLAECK:" + frame + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending

            device._poll_upstreams()
            transport._pending = b""

            downstream = client.recv(4096)
            content = self._extract_content(downstream)
            ids = self._extract_signal_ids(content)

            assert ids == [0], f"Expected only signal 0, got {ids}"
        finally:
            client.close()
            device.close()

    def test_crc_valid_on_relayed_frame(self):
        """Downstream relay frame has valid CRC over status byte + payload."""
        device, client, upstream, transport = self._setup_hub_with_upstream(
            ["temp", "hum"]
        )
        try:
            table = upstream.symbol_table
            schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in table]
            )
            frame = _build_d2_frame_floats(
                schema_hash, [25.0, 60.0], status=0x01,
                status_payload=b"\x01\x03\x01\x00",
            )
            transport._pending = b"<BLAECK:" + frame + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending

            device._poll_upstreams()
            transport._pending = b""

            downstream = client.recv(4096)
            content = self._extract_content(downstream)

            expected_crc = binascii.crc32(content[:-4]) & 0xFFFFFFFF
            actual_crc = int.from_bytes(content[-4:], "little")
            assert actual_crc == expected_crc, (
                f"CRC mismatch: expected 0x{expected_crc:08x}, got 0x{actual_crc:08x}"
            )
        finally:
            client.close()
            device.close()

    def test_ok_frame_after_skip_frame(self):
        """StatusByte returns to 0x00 when all signals arrive again."""
        device, client, upstream, transport = self._setup_hub_with_upstream(
            ["master_sig", "slave_sig"]
        )
        try:
            table = upstream.symbol_table
            schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in table]
            )

            # Frame 1: skip (only signal 0)
            frame1 = _build_d2_frame_sparse(schema_hash, {0: 1.0}, status=0x01)
            transport._pending = b"<BLAECK:" + frame1 + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending
            device._poll_upstreams()
            transport._pending = b""
            client.recv(4096)  # consume frame 1

            # Frame 2: all signals OK
            frame2 = _build_d2_frame_floats(schema_hash, [2.0, 3.0], status=0x00)
            transport._pending = b"<BLAECK:" + frame2 + b"/BLAECK>\r\n"
            transport.read_available = lambda: transport._pending
            device._poll_upstreams()
            transport._pending = b""

            downstream2 = client.recv(4096)
            content2 = self._extract_content(downstream2)
            assert content2[-9] == 0x00, (
                f"Expected status 0x00 on recovery, got 0x{content2[-9]:02x}"
            )
        finally:
            client.close()
            device.close()


# ---------------------------------------------------------------------------
# _resend_activate: 3 modes
# ---------------------------------------------------------------------------

class TestResendActivate:
    """_resend_activate sends the correct command for each interval mode."""

    def _make_upstream(self, transport: RecordingTransport, interval_ms: int):
        upstream = _UpstreamDevice(
            device_name="TestDevice",
            transport=transport,
            relay_downstream=True,
        )
        upstream.interval_ms = interval_ms
        return upstream

    def test_hub_managed_sends_activate(self):
        """Hub-managed interval (>=0) sends BLAECK.ACTIVATE with interval bytes."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=500)
            device._hub._upstreams.append(upstream)

            device._resend_activate(upstream)

            assert len(transport.sent) == 1
            cmd = transport.sent[0].decode()
            # 500 = 0x01F4 → bytes: 0xF4, 0x01, 0x00, 0x00
            assert cmd == "<BLAECK.ACTIVATE,244,1,0,0>"
        finally:
            device.close()

    def test_hub_managed_zero_interval(self):
        """Hub-managed interval=0 (fastest) sends ACTIVATE with all zeros."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=0)
            device._hub._upstreams.append(upstream)

            device._resend_activate(upstream)

            assert len(transport.sent) == 1
            cmd = transport.sent[0].decode()
            assert cmd == "<BLAECK.ACTIVATE,0,0,0,0>"
        finally:
            device.close()

    def test_off_sends_deactivate(self):
        """IntervalMode.OFF sends BLAECK.DEACTIVATE."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=IntervalMode.OFF)
            device._hub._upstreams.append(upstream)

            device._resend_activate(upstream)

            assert len(transport.sent) == 1
            cmd = transport.sent[0].decode()
            assert cmd == "<BLAECK.DEACTIVATE>"
        finally:
            device.close()

    def test_client_managed_restores_last_command(self):
        """IntervalMode.CLIENT restores the last client ACTIVATE command."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=IntervalMode.CLIENT)
            device._hub._upstreams.append(upstream)

            device._last_client_activate_cmd = "BLAECK.ACTIVATE,232,3,0,0"
            device._resend_activate(upstream)

            assert len(transport.sent) == 1
            cmd = transport.sent[0].decode()
            assert cmd == "<BLAECK.ACTIVATE,232,3,0,0>"
        finally:
            device.close()

    def test_client_managed_no_prior_command_sends_nothing(self):
        """IntervalMode.CLIENT with no prior client command sends nothing."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=IntervalMode.CLIENT)
            device._hub._upstreams.append(upstream)

            device._last_client_activate_cmd = None
            device._resend_activate(upstream)

            assert len(transport.sent) == 0
        finally:
            device.close()

    def test_hub_managed_large_interval(self):
        """Hub-managed interval=60000 (1 min) encodes correctly."""
        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            transport = RecordingTransport("test")
            upstream = self._make_upstream(transport, interval_ms=60000)
            device._hub._upstreams.append(upstream)

            device._resend_activate(upstream)

            # 60000 = 0xEA60 → bytes: 0x60, 0xEA, 0x00, 0x00
            cmd = transport.sent[0].decode()
            assert cmd == "<BLAECK.ACTIVATE,96,234,0,0>"
        finally:
            device.close()
