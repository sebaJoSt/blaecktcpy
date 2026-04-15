"""Tests for malformed symbol IDs, multi-upstream topology, and schema-change relay."""

import binascii
import struct

from blaecktcpy import Signal, SignalList
from blaecktcpy._server import UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import _make_server_on_free_port, _start_retry, FakeTransport, RecordingTransport


# ---------------------------------------------------------------------------
# Frame builders (reused from test_schema_hash)
# ---------------------------------------------------------------------------

def _build_d2_frame(
    schema_hash: int,
    signal_pairs: list[tuple[int, bytes]],
    restart_flag: bool = False,
):
    """Build a D2 frame with explicit (symbol_id, raw_bytes) pairs.

    Unlike the simpler helper in test_schema_hash, this accepts raw bytes
    per signal so we can inject malformed IDs and payload sizes.
    """
    msg_key = b"\xd2"
    msg_id = (1).to_bytes(4, "little")
    flag = b"\x01" if restart_flag else b"\x00"
    hash_bytes = schema_hash.to_bytes(2, "little")
    ts_mode = b"\x00"
    meta = flag + b":" + hash_bytes + b":" + ts_mode + b":"

    payload = b""
    for sym_id, raw in signal_pairs:
        payload += sym_id.to_bytes(2, "little") + raw

    crc_input = msg_key + b":" + msg_id + b":" + meta + payload
    crc = binascii.crc32(crc_input).to_bytes(4, "little")
    return msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc


def _build_d2_frame_floats(
    schema_hash: int,
    signal_values: list[float],
    restart_flag: bool = False,
):
    """Convenience: build D2 with sequential float signals."""
    pairs = [(i, struct.pack("<f", v)) for i, v in enumerate(signal_values)]
    return _build_d2_frame(schema_hash, pairs, restart_flag)


def _build_b0_frame(symbols: list[tuple[str, int]]):
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
    device_name: str = "Upstream",
):
    """Create a started hub with one upstream."""
    device = _make_server_on_free_port()
    _start_retry(device)

    transport = transport_cls(device_name)
    upstream = UpstreamDevice(
        device_name=device_name, transport=transport, relay_downstream=relay
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


def _add_second_upstream(
    device,
    symbols: list[tuple[str, int]],
    relay: bool = True,
    transport_cls=FakeTransport,
    device_name: str = "Upstream2",
):
    """Add a second upstream to an existing hub. Returns (upstream, transport)."""
    transport = transport_cls(device_name)
    upstream = UpstreamDevice(
        device_name=device_name, transport=transport, relay_downstream=relay
    )

    sym_objs = [
        decoder.DecodedSymbol(name, code, decoder._DTYPE_INFO[code][0], decoder._DTYPE_INFO[code][1])
        for name, code in symbols
    ]
    upstream.symbol_table = sym_objs
    upstream.expected_schema_hash = decoder.compute_schema_hash(symbols)

    offset = len(device.signals)
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

    return upstream, transport


# ===========================================================================
# Malformed Symbol IDs
# ===========================================================================

class TestMalformedSymbolIDs:
    """Verify robust handling of out-of-range and edge-case symbol IDs."""

    def test_symbol_id_beyond_table_stops_unpacking(self):
        """Symbol ID >= len(symbol_table) causes _unpack_signals to stop early."""
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]
        # ID 0 valid, ID 5 out of range → only ID 0 decoded
        pairs = [
            (0, struct.pack("<f", 1.0)),
            (5, struct.pack("<f", 2.0)),
        ]
        frame = _build_d2_frame(0, pairs)
        decoded = decoder.parse_data(frame, sym_table)
        assert 0 in decoded.signals
        assert 5 not in decoded.signals
        assert len(decoded.signals) == 1

    def test_large_symbol_id_stops_unpacking(self):
        """Very large symbol ID (0xFFFF) is gracefully ignored."""
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]
        pairs = [
            (0, struct.pack("<f", 3.0)),
            (0xFFFF, struct.pack("<f", 9.0)),
        ]
        frame = _build_d2_frame(0, pairs)
        decoded = decoder.parse_data(frame, sym_table)
        assert decoded.signals[0] == 3.0
        assert len(decoded.signals) == 1

    def test_duplicate_symbol_id_last_wins(self):
        """Duplicate symbol IDs: the later value overwrites the earlier one."""
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]
        pairs = [
            (0, struct.pack("<f", 10.0)),
            (0, struct.pack("<f", 20.0)),
        ]
        frame = _build_d2_frame(0, pairs)
        decoded = decoder.parse_data(frame, sym_table)
        assert decoded.signals[0] == 20.0

    def test_out_of_order_symbol_ids_decode_correctly(self):
        """Non-sequential symbol IDs are decoded by their actual ID."""
        sym_table = [
            decoder.DecodedSymbol("a", 8, "float", 4),
            decoder.DecodedSymbol("b", 8, "float", 4),
            decoder.DecodedSymbol("c", 8, "float", 4),
        ]
        # Send ID 2 before ID 0
        pairs = [
            (2, struct.pack("<f", 30.0)),
            (0, struct.pack("<f", 10.0)),
        ]
        frame = _build_d2_frame(0, pairs)
        decoded = decoder.parse_data(frame, sym_table)
        assert decoded.signals[2] == 30.0
        assert decoded.signals[0] == 10.0
        assert 1 not in decoded.signals

    def test_truncated_payload_raises(self):
        """Payload that ends mid-signal raises ValueError."""
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]
        # Only 2 of 4 data bytes for float
        pairs = [(0, b"\x00\x00")]
        frame = _build_d2_frame(0, pairs)
        try:
            decoder.parse_data(frame, sym_table)
            assert False, "Expected ValueError for truncated payload"
        except ValueError as e:
            assert "Truncated" in str(e) or "need" in str(e).lower()

    def test_empty_payload_decodes_no_signals(self):
        """Frame with no signal data produces empty signals dict."""
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]
        frame = _build_d2_frame(0, [])
        decoded = decoder.parse_data(frame, sym_table)
        assert len(decoded.signals) == 0

    def test_d2_timestamp_mode_requires_full_8_byte_timestamp(self):
        """D2 with timestamp_mode>0 and short timestamp raises ValueError."""
        msg_key = b"\xd2"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x34\x12:\x01" + b"\xAA\xBB\xCC\xDD" + b":"  # only 4/8 bytes ts
        payload = b""
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]

        try:
            decoder.parse_data(frame, sym_table)
            assert False, "Expected ValueError for truncated D2 timestamp"
        except ValueError as e:
            assert (
                "Truncated D2 timestamp" in str(e)
                or "D2 timestamp metadata" in str(e)
            )

    def test_d1_timestamp_mode_requires_full_4_byte_timestamp(self):
        """D1 with timestamp_mode>0 and short timestamp raises ValueError."""
        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x01" + b"\xAA\xBB" + b":"  # only 2/4 bytes ts
        payload = b""
        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        frame = msg_key + b":" + msg_id + b":" + meta + payload + b"\x00" + crc
        sym_table = [decoder.DecodedSymbol("x", 8, "float", 4)]

        try:
            decoder.parse_data(frame, sym_table)
            assert False, "Expected ValueError for truncated D1 timestamp"
        except ValueError as e:
            assert (
                "Truncated D1 timestamp" in str(e)
                or "D1 timestamp metadata" in str(e)
            )

    def test_hub_ignores_extra_symbol_ids_in_relay(self):
        """Hub processes known IDs and ignores out-of-range ones in D2 frames."""
        symbols = [("temp", 8)]
        device, upstream, transport = _make_hub_with_upstream(symbols)
        try:
            h = upstream.expected_schema_hash
            # ID 0 is valid, ID 5 is beyond the symbol table
            pairs = [
                (0, struct.pack("<f", 42.0)),
                (5, struct.pack("<f", 99.0)),
            ]
            frame = _build_d2_frame(h, pairs)
            transport.read_available = lambda: _wrap(frame)

            device._poll_upstreams()

            idx = upstream.index_map[0]
            assert device.signals[idx].value == 42.0
            assert not upstream.schema_stale
        finally:
            device.close()


# ===========================================================================
# Multi-upstream topology mutation
# ===========================================================================

class TestMultiUpstreamTopology:
    """Verify correct behavior with multiple upstreams and schema changes."""

    def test_schema_change_isolates_to_one_upstream(self):
        """Schema mismatch on upstream A doesn't affect upstream B."""
        device, upstream_a, transport_a = _make_hub_with_upstream(
            [("temp", 8)], transport_cls=RecordingTransport, device_name="A"
        )
        upstream_b, transport_b = _add_second_upstream(
            device, [("voltage", 8)], transport_cls=FakeTransport, device_name="B"
        )
        try:
            # Trigger mismatch on A
            bad_frame = _build_d2_frame_floats(0x9999, [1.0])
            transport_a.read_available = lambda: _wrap(bad_frame)

            # Send good frame on B
            h_b = upstream_b.expected_schema_hash
            good_frame = _build_d2_frame_floats(h_b, [5.0])
            transport_b.read_available = lambda: _wrap(good_frame)

            device._poll_upstreams()

            assert upstream_a.schema_stale is True
            assert upstream_b.schema_stale is False
            # B's signal should be updated
            b_idx = upstream_b.index_map[0]
            assert device.signals[b_idx].value == 5.0
        finally:
            device.close()

    def test_rebuild_preserves_other_upstream_index_map(self):
        """Rebuilding upstream A's schema doesn't corrupt upstream B's index_map."""
        device, upstream_a, transport_a = _make_hub_with_upstream(
            [("temp", 8)], device_name="A"
        )
        upstream_b, transport_b = _add_second_upstream(
            device, [("voltage", 8), ("current", 8)], device_name="B"
        )
        try:
            b_map_before = dict(upstream_b.index_map)

            # Rebuild A's schema with a new signal added
            upstream_a.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport_a.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            # B's index_map should be rebuilt but still consistent
            assert len(upstream_b.index_map) == 2
            # B's indices should follow A's
            a_max = max(upstream_a.index_map.values())
            b_min = min(upstream_b.index_map.values())
            assert b_min > a_max, "B's indices must follow A's"
        finally:
            device.close()

    def test_rebuild_updates_total_signal_count(self):
        """Schema rebuild that adds a signal increases hub's total signal count."""
        device, upstream, transport = _make_hub_with_upstream([("temp", 8)])
        try:
            count_before = len(device.signals)

            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            assert len(device.signals) == count_before + 1
        finally:
            device.close()

    def test_rebuild_removes_signals_on_shrink(self):
        """Schema rebuild that removes a signal decreases hub's total signal count."""
        device, upstream, transport = _make_hub_with_upstream(
            [("temp", 8), ("humidity", 8)]
        )
        try:
            count_before = len(device.signals)

            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            assert len(device.signals) == count_before - 1
        finally:
            device.close()

    def test_stale_upstream_skips_while_other_relays(self):
        """Stale upstream's data is skipped while another upstream relays normally."""
        device, upstream_a, transport_a = _make_hub_with_upstream(
            [("temp", 8)], device_name="A"
        )
        upstream_b, transport_b = _add_second_upstream(
            device, [("voltage", 8)], device_name="B"
        )
        try:
            upstream_a.schema_stale = True
            device.signals[upstream_a.index_map[0]].value = 0.0

            # A sends data (should be skipped)
            h_a = upstream_a.expected_schema_hash
            frame_a = _build_d2_frame_floats(h_a, [99.0])
            transport_a.read_available = lambda: _wrap(frame_a)

            # B sends data (should relay)
            h_b = upstream_b.expected_schema_hash
            frame_b = _build_d2_frame_floats(h_b, [7.0])
            transport_b.read_available = lambda: _wrap(frame_b)

            device._poll_upstreams()

            assert device.signals[upstream_a.index_map[0]].value == 0.0  # unchanged
            assert device.signals[upstream_b.index_map[0]].value == 7.0  # updated
        finally:
            device.close()

    def test_both_upstreams_can_go_stale_independently(self):
        """Both upstreams can detect schema mismatch independently."""
        device, upstream_a, transport_a = _make_hub_with_upstream(
            [("temp", 8)], transport_cls=RecordingTransport, device_name="A"
        )
        upstream_b, transport_b = _add_second_upstream(
            device, [("voltage", 8)], transport_cls=RecordingTransport, device_name="B"
        )
        try:
            bad_a = _build_d2_frame_floats(0x1111, [1.0])
            bad_b = _build_d2_frame_floats(0x2222, [2.0])
            transport_a.read_available = lambda: _wrap(bad_a)
            transport_b.read_available = lambda: _wrap(bad_b)

            device._poll_upstreams()

            assert upstream_a.schema_stale is True
            assert upstream_b.schema_stale is True
            assert any(b"BLAECK.WRITE_SYMBOLS" in s for s in transport_a.sent)
            assert any(b"BLAECK.WRITE_SYMBOLS" in s for s in transport_b.sent)
        finally:
            device.close()


# ===========================================================================
# Schema-change relay behavior
# ===========================================================================

class TestSchemaChangeRelay:
    """Verify that schema changes cascade into relayed frames correctly."""

    def test_hub_schema_hash_changes_after_upstream_rebuild(self):
        """Hub's own _schema_hash updates when an upstream's schema changes."""
        device, upstream, transport = _make_hub_with_upstream([("temp", 8)])
        try:
            hash_before = device._schema_hash

            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            assert device._schema_hash != hash_before
        finally:
            device.close()

    def test_relayed_frame_contains_updated_hash(self):
        """After rebuild, relayed D2 frames carry the hub's new schema hash."""
        import socket
        device, upstream, transport = _make_hub_with_upstream([("temp", 8)])
        try:
            # Connect a client
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(("127.0.0.1", device._port))
            device._accept_new_clients()

            # Trigger rebuild: temp → temp + pressure
            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            new_hub_hash = device._schema_hash

            # Now relay a matching D2 frame
            h = upstream.expected_schema_hash
            frame = _build_d2_frame_floats(h, [25.0, 100.0])
            transport.read_available = lambda: _wrap(frame)
            device._poll_upstreams()

            raw = client.recv(4096)
            start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = raw.find(b"/BLAECK>")
            content = raw[start:end]

            # D2: key(1) ':' id(4) ':' restart(1) ':' hash(2) ':' ...
            parts = content.split(b":", 5)
            relayed_hash = int.from_bytes(parts[3], "little")
            assert relayed_hash == new_hub_hash
        finally:
            client.close()
            device.close()

    def test_schema_rebuild_with_local_signals_preserves_locals(self):
        """Local signals survive an upstream schema rebuild."""
        device = _make_server_on_free_port()
        device.add_signal("local_sin", "float", 0.0)
        _start_retry(device)

        transport = FakeTransport("Remote")
        upstream = UpstreamDevice(
            device_name="Remote", transport=transport, relay_downstream=True
        )
        sym_objs = [decoder.DecodedSymbol("temp", 8, "float", 4)]
        upstream.symbol_table = sym_objs
        upstream.expected_schema_hash = decoder.compute_schema_hash([("temp", 8)])
        offset = device._local_signal_count  # 1 (local_sin)
        sig = Signal("temp", "float")
        device.signals.append(sig)
        upstream._signals.append(device.signals[offset])
        upstream.index_map[0] = offset
        upstream._upstream_signals = SignalList(upstream._signals)
        upstream.connected = True
        device._hub._upstreams.append(upstream)
        device._update_schema_hash()

        try:
            assert device.signals[0].signal_name == "local_sin"
            device.signals[0].value = 42.0

            upstream.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            # Local signal preserved at index 0
            assert device.signals[0].signal_name == "local_sin"
            assert device.signals[0].value == 42.0
            # Upstream signals follow
            assert len(device.signals) == 3  # 1 local + 2 upstream
        finally:
            device.close()

    def test_schema_change_with_type_change_updates_signal(self):
        """Upstream replacing float → long creates signal with correct type."""
        device, upstream, transport = _make_hub_with_upstream([("val", 8)])  # float
        try:
            upstream.schema_stale = True
            b0 = _build_b0_frame([("val", 6)])  # long (4-byte int)
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            idx = upstream.index_map[0]
            assert device.signals[idx].datatype == "long"
        finally:
            device.close()

    def test_successive_rebuilds_converge(self):
        """Multiple consecutive schema changes produce consistent state."""
        device, upstream, transport = _make_hub_with_upstream([("a", 8)])
        try:
            # First rebuild: a → a, b
            upstream.schema_stale = True
            b0 = _build_b0_frame([("a", 8), ("b", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()
            assert len(upstream.index_map) == 2

            # Second rebuild: a, b → c
            upstream.schema_stale = True
            b0 = _build_b0_frame([("c", 8)])
            transport.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            assert len(upstream.index_map) == 1
            assert len(device.signals) == 1
            assert device.signals[0].signal_name == "c"

            # Verify relaying works with new schema
            h = upstream.expected_schema_hash
            frame = _build_d2_frame_floats(h, [77.0])
            transport.read_available = lambda: _wrap(frame)
            device._poll_upstreams()
            assert device.signals[0].value == 77.0
        finally:
            device.close()

    def test_rebuild_during_active_relay_no_index_corruption(self):
        """Rebuild between two poll cycles doesn't corrupt signal indices."""
        device, upstream_a, transport_a = _make_hub_with_upstream(
            [("temp", 8)], device_name="A"
        )
        upstream_b, transport_b = _add_second_upstream(
            device, [("voltage", 8)], device_name="B"
        )
        try:
            # Send valid data to both upstreams to establish values
            h_a = upstream_a.expected_schema_hash
            h_b = upstream_b.expected_schema_hash
            frame_a = _build_d2_frame_floats(h_a, [10.0])
            frame_b = _build_d2_frame_floats(h_b, [20.0])
            transport_a.read_available = lambda: _wrap(frame_a)
            transport_b.read_available = lambda: _wrap(frame_b)
            device._poll_upstreams()

            # Rebuild A's schema (adds signal)
            upstream_a.schema_stale = True
            b0 = _build_b0_frame([("temp", 8), ("pressure", 8)])
            transport_a.read_available = lambda: _wrap(b0)
            device._poll_upstreams()

            # After rebuild, send data to both with correct hashes
            h_a_new = upstream_a.expected_schema_hash
            h_b_new = upstream_b.expected_schema_hash
            frame_a2 = _build_d2_frame_floats(h_a_new, [11.0, 22.0])
            frame_b2 = _build_d2_frame_floats(h_b_new, [33.0])

            transport_a.read_available = lambda: _wrap(frame_a2)
            transport_b.read_available = lambda: _wrap(frame_b2)
            device._poll_upstreams()

            a_temp_idx = upstream_a.index_map[0]
            a_press_idx = upstream_a.index_map[1]
            b_volt_idx = upstream_b.index_map[0]

            assert device.signals[a_temp_idx].value == 11.0
            assert device.signals[a_press_idx].value == 22.0
            assert device.signals[b_volt_idx].value == 33.0
            # No index overlap
            all_indices = list(upstream_a.index_map.values()) + list(upstream_b.index_map.values())
            assert len(all_indices) == len(set(all_indices)), "Index overlap detected!"
        finally:
            device.close()

    def test_long_running_single_upstream_schema_churn(self):
        """Repeated schema changes on one upstream converge without index/value drift."""
        device, upstream, transport = _make_hub_with_upstream([("a", 8)])
        try:
            schemas = [
                [("a", 8)],
                [("a", 8), ("b", 8)],
            ]

            for i in range(10):
                schema = schemas[i % 2]
                upstream.schema_stale = True
                b0 = _build_b0_frame(schema)
                transport.read_available = lambda frame=b0: _wrap(frame)
                device._poll_upstreams()

                # Verify mapping shape and index continuity
                assert len(upstream.index_map) == len(schema)
                mapped = sorted(upstream.index_map.values())
                assert mapped == list(range(mapped[0], mapped[0] + len(schema)))

                # Relay a frame with matching hash and verify values land correctly
                expected_hash = upstream.expected_schema_hash
                values = [float(i + j + 1) for j in range(len(schema))]
                frame = _build_d2_frame_floats(expected_hash, values)
                transport.read_available = lambda frame=frame: _wrap(frame)
                device._poll_upstreams()

                for j, expected in enumerate(values):
                    hub_idx = upstream.index_map[j]
                    assert device.signals[hub_idx].value == expected
        finally:
            device.close()

    def test_long_running_multi_upstream_churn_isolated(self):
        """Frequent churn on one upstream does not break steady relay on another."""
        device, upstream_a, transport_a = _make_hub_with_upstream([("temp", 8)], device_name="A")
        upstream_b, transport_b = _add_second_upstream(device, [("volt", 8)], device_name="B")
        try:
            for i in range(8):
                # Churn A: alternate 1-signal and 2-signal schemas
                upstream_a.schema_stale = True
                schema_a = [("temp", 8)] if i % 2 == 0 else [("temp", 8), ("press", 8)]
                b0_a = _build_b0_frame(schema_a)
                transport_a.read_available = lambda frame=b0_a: _wrap(frame)

                # B keeps sending normal data every cycle
                hash_b = upstream_b.expected_schema_hash
                frame_b = _build_d2_frame_floats(hash_b, [100.0 + i])
                transport_b.read_available = lambda frame=frame_b: _wrap(frame)
                device._poll_upstreams()

                # After rebuild, A should accept matching data
                hash_a = upstream_a.expected_schema_hash
                vals_a = [10.0 + i + j for j in range(len(schema_a))]
                frame_a = _build_d2_frame_floats(hash_a, vals_a)
                transport_a.read_available = lambda frame=frame_a: _wrap(frame)
                transport_b.read_available = lambda: b""
                device._poll_upstreams()

                # B keeps correct value progression (isolation)
                b_idx = upstream_b.index_map[0]
                assert device.signals[b_idx].value == 100.0 + i

                # A values mapped correctly post-rebuild
                for j, expected in enumerate(vals_a):
                    a_idx = upstream_a.index_map[j]
                    assert device.signals[a_idx].value == expected

            # Final integrity: no overlapping mapped indices
            all_indices = list(upstream_a.index_map.values()) + list(upstream_b.index_map.values())
            assert len(all_indices) == len(set(all_indices))
        finally:
            device.close()

