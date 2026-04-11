"""Tests for D2 schema hash: computation, mismatch detection, and re-discovery."""

import binascii
import struct

from blaecktcpy import Signal, SignalList
from blaecktcpy._server import _UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import _make_server_on_free_port, _start_retry, FakeTransport, RecordingTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_d2_frame(
    schema_hash: int,
    signal_values: list[float],
    restart_flag: bool = False,
):
    """Build a valid D2 data frame with explicit schema_hash."""
    msg_key = b"\xd2"
    msg_id = (1).to_bytes(4, "little")
    flag = b"\x01" if restart_flag else b"\x00"
    hash_bytes = schema_hash.to_bytes(2, "little")
    ts_mode = b"\x00"
    meta = flag + b":" + hash_bytes + b":" + ts_mode + b":"

    payload = b""
    for idx, val in enumerate(signal_values):
        payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

    crc_input = msg_key + b":" + msg_id + b":" + meta + payload
    crc = binascii.crc32(crc_input).to_bytes(4, "little")
    return msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc


def _build_b0_frame(symbols: list[tuple[str, int]]):
    """Build a B0 symbol list frame from (name, datatype_code) pairs."""
    msg_key = b"\xb0"
    msg_id = (1).to_bytes(4, "little")
    payload = b""
    for name, dtype_code in symbols:
        payload += b"\x00\x00" + name.encode("utf-8") + b"\x00" + bytes([dtype_code])
    return msg_key + b":" + msg_id + b":" + payload


def _wrap(frame: bytes) -> bytes:
    return b"<BLAECK:" + frame + b"/BLAECK>\r\n"


def _make_hub_with_upstream(
    symbols: list[tuple[str, int]],
    relay: bool = True,
    transport_cls=FakeTransport,
):
    """Create a started hub with one upstream. Returns (device, upstream, transport)."""
    device = _make_server_on_free_port()
    _start_retry(device)

    transport = transport_cls("Upstream")
    upstream = _UpstreamDevice(
        device_name="Upstream", transport=transport, relay_downstream=relay
    )

    sym_objs = [
        decoder.DecodedSymbol(name, code, decoder._DTYPE_INFO[code][0], decoder._DTYPE_INFO[code][1])
        for name, code in symbols
    ]
    upstream.symbol_table = sym_objs
    upstream.expected_schema_hash = decoder.compute_schema_hash(symbols)

    offset = device._local_signal_count
    for i, (name, code) in enumerate(symbols):
        sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(code, "float")
        sig = Signal(name, sig_type)
        if relay:
            device.signals.append(sig)
            upstream._signals.append(device.signals[offset])
            upstream.index_map[i] = offset
            offset += 1
        else:
            upstream._signals.append(sig)
            upstream.index_map[i] = i
    upstream._upstream_signals = SignalList(upstream._signals)
    upstream.connected = True
    device._hub._upstreams.append(upstream)
    device._update_schema_hash()

    return device, upstream, transport


# ---------------------------------------------------------------------------
# Tests: compute_schema_hash
# ---------------------------------------------------------------------------

class TestComputeSchemaHash:
    """Verify CRC16 schema hash computation."""

    def test_empty_schema(self):
        assert decoder.compute_schema_hash([]) == 0

    def test_deterministic(self):
        pairs = [("temp", 8), ("humidity", 8)]
        h1 = decoder.compute_schema_hash(pairs)
        h2 = decoder.compute_schema_hash(pairs)
        assert h1 == h2

    def test_different_names_differ(self):
        h1 = decoder.compute_schema_hash([("temp", 8)])
        h2 = decoder.compute_schema_hash([("pressure", 8)])
        assert h1 != h2

    def test_different_types_differ(self):
        h1 = decoder.compute_schema_hash([("val", 8)])   # float
        h2 = decoder.compute_schema_hash([("val", 6)])   # int
        assert h1 != h2

    def test_order_matters(self):
        h1 = decoder.compute_schema_hash([("a", 8), ("b", 6)])
        h2 = decoder.compute_schema_hash([("b", 6), ("a", 8)])
        assert h1 != h2

    def test_returns_16_bit(self):
        h = decoder.compute_schema_hash([("x", 8)])
        assert 0 <= h <= 0xFFFF


# ---------------------------------------------------------------------------
# Tests: D2 frame round-trip
# ---------------------------------------------------------------------------

class TestD2SchemaHashRoundTrip:
    """Verify schema hash encodes into and decodes from D2 frames."""

    def test_parse_d2_reads_schema_hash(self):
        """Parser extracts schema_hash from a D2 frame."""
        expected_hash = 0xABCD
        frame = _build_d2_frame(schema_hash=expected_hash, signal_values=[1.0])
        sym_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, sym_table)
        assert decoded.schema_hash == expected_hash

    def test_d1_frame_has_zero_schema_hash(self):
        """D1 frames report schema_hash=0 (not present in format)."""
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"
        payload = (0).to_bytes(2, "little") + struct.pack("<f", 1.0)
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc

        sym_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, sym_table)
        assert decoded.schema_hash == 0

    def test_server_includes_schema_hash_in_frame(self):
        """Server's _build_data_msg includes the schema hash."""
        import socket
        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 0.0)
        _start_retry(device)
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(("127.0.0.1", device._port))
            device._accept_new_clients()

            device.write_all_data()
            raw = client.recv(4096)
            start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = raw.find(b"/BLAECK>")
            content = raw[start:end]

            # D2: msg_key(1) : msg_id(4) : restart(1) : schema_hash(2) : ...
            # schema_hash is at content[8:10] (after msg_key:msg_id:restart:)
            parts = content.split(b":", 5)
            schema_hash_bytes = parts[3]  # between 3rd and 4th colon
            schema_hash = int.from_bytes(schema_hash_bytes, "little")
            assert schema_hash == device._schema_hash
            assert schema_hash != 0  # should be computed from "temp"/float
        finally:
            client.close()
            device.close()


# ---------------------------------------------------------------------------
# Tests: Mismatch detection
# ---------------------------------------------------------------------------

class TestSchemaHashMismatch:
    """Hub detects upstream schema changes via hash mismatch."""

    def test_matching_hash_relays_normally(self):
        """D2 frame with matching hash is processed and relayed."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            correct_hash = upstream.expected_schema_hash
            frame = _build_d2_frame(schema_hash=correct_hash, signal_values=[25.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert not upstream.schema_stale
            assert device.signals[0].value == 25.0
        finally:
            device.close()

    def test_mismatch_sets_schema_stale(self):
        """D2 frame with wrong hash sets schema_stale flag."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(
            symbols, transport_cls=RecordingTransport
        )
        try:
            wrong_hash = 0x9999
            frame = _build_d2_frame(schema_hash=wrong_hash, signal_values=[25.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert upstream.schema_stale is True
        finally:
            device.close()

    def test_mismatch_sends_write_symbols(self):
        """On mismatch, hub sends BLAECK.WRITE_SYMBOLS to upstream."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(
            symbols, transport_cls=RecordingTransport
        )
        try:
            frame = _build_d2_frame(schema_hash=0x9999, signal_values=[25.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert any(b"BLAECK.WRITE_SYMBOLS" in s for s in transport.sent)
        finally:
            device.close()

    def test_mismatch_drops_data_no_value_update(self):
        """Mismatched frame's signal values are NOT applied."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            device.signals[0].value = 0.0
            frame = _build_d2_frame(schema_hash=0x9999, signal_values=[99.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert device.signals[0].value == 0.0  # unchanged
        finally:
            device.close()

    def test_stale_upstream_skips_subsequent_data(self):
        """While schema_stale, all further data frames are skipped."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            upstream.schema_stale = True
            correct_hash = upstream.expected_schema_hash
            frame = _build_d2_frame(schema_hash=correct_hash, signal_values=[42.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert device.signals[0].value != 42.0  # skipped
        finally:
            device.close()


# ---------------------------------------------------------------------------
# Tests: B0 re-discovery
# ---------------------------------------------------------------------------

class TestSchemaReDiscovery:
    """Hub rebuilds signals from B0 frame after schema mismatch."""

    def test_b0_clears_schema_stale(self):
        """B0 frame during stale state clears the flag."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8)])
            transport.read_available = lambda: _wrap(b0)

            device._poll_upstreams()

            assert upstream.schema_stale is False
        finally:
            device.close()

    def test_b0_rebuilds_symbol_table(self):
        """B0 with new signals updates symbol_table."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            upstream.schema_stale = True
            new_symbols = [("temp", 8), ("pressure", 8)]
            b0 = _build_b0_frame(new_symbols)
            transport.read_available = lambda: _wrap(b0)

            device._poll_upstreams()

            assert len(upstream.symbol_table) == 2
            assert upstream.symbol_table[1].name == "pressure"
        finally:
            device.close()

    def test_b0_updates_expected_hash(self):
        """After rebuild, expected_schema_hash matches new schema."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            upstream.schema_stale = True
            new_symbols = [("temp", 8), ("pressure", 8)]
            b0 = _build_b0_frame(new_symbols)
            transport.read_available = lambda: _wrap(b0)

            device._poll_upstreams()

            expected = decoder.compute_schema_hash(new_symbols)
            assert upstream.expected_schema_hash == expected
        finally:
            device.close()

    def test_b0_rebuilds_index_map(self):
        """After rebuild, index_map covers all new signals."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            upstream.schema_stale = True
            new_symbols = [("temp", 8), ("pressure", 8)]
            b0 = _build_b0_frame(new_symbols)
            transport.read_available = lambda: _wrap(b0)

            device._poll_upstreams()

            assert len(upstream.index_map) == 2
        finally:
            device.close()

    def test_resume_relay_after_rediscovery(self):
        """After B0 rebuild, subsequent matching D2 frames are relayed."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            # Trigger stale + resolve with B0
            upstream.schema_stale = True
            new_symbols = [("temp", 8), ("pressure", 8)]
            b0 = _build_b0_frame(new_symbols)
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            # Now send a matching D2 frame
            new_hash = upstream.expected_schema_hash
            frame = _build_d2_frame(schema_hash=new_hash, signal_values=[25.0, 101.3])
            transport.read_available = lambda: _wrap(frame)
            device._poll_upstreams()

            assert not upstream.schema_stale
            # temp is at index_map[0], pressure at index_map[1]
            temp_idx = upstream.index_map[0]
            pressure_idx = upstream.index_map[1]
            assert device.signals[temp_idx].value == 25.0
            assert abs(device.signals[pressure_idx].value - 101.3) < 0.001
        finally:
            device.close()


# ---------------------------------------------------------------------------
# Tests: D1/B1 signal count fallback
# ---------------------------------------------------------------------------

class TestD1SignalCountFallback:
    """D1/B1 frames use signal count mismatch as fallback detection."""

    def _build_d1_frame(self, signal_values: list[float]):
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"
        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc

    def test_d1_matching_count_processes_normally(self):
        """D1 frame with correct signal count is processed."""
        symbols = [("temp", 8), ("humidity", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            frame = self._build_d1_frame([20.0, 60.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert not upstream.schema_stale
        finally:
            device.close()

    def test_d1_count_mismatch_sets_stale(self):
        """D1 frame with fewer signals than expected triggers re-discovery."""
        symbols = [("temp", 8), ("humidity", 8)]
        device, upstream, transport = _make_hub_with_upstream(
            symbols, transport_cls=RecordingTransport
        )
        try:
            # Send frame with 1 signal, but symbol_table has 2
            frame = self._build_d1_frame([20.0])
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            assert upstream.schema_stale is True
            assert any(b"BLAECK.WRITE_SYMBOLS" in s for s in transport.sent)
        finally:
            device.close()
