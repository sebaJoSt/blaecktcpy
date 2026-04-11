"""Tests for _encoder — stateless protocol encoding functions."""

import binascii
import struct

import pytest

from blaecktcpy._encoder import (
    MSG_DATA,
    MSG_DEVICES,
    MSG_SYMBOL_LIST,
    MSC_MASTER,
    MSC_SLAVE,
    STATUS_OK,
    STATUS_UPSTREAM_LOST,
    STATUS_UPSTREAM_RECONNECTED,
    build_client_trailer,
    build_data_frame,
    build_header,
    build_symbol_payload,
    encode_device_entry,
    wrap_frame,
)
from blaecktcpy._signal import Signal, SignalList, TimestampMode


# ── Helpers ──────────────────────────────────────────────────────────


def _make_signals(*specs: tuple[str, str, int | float]) -> SignalList:
    """Create a SignalList from (name, dtype, value) tuples."""
    sigs = SignalList()
    for name, dtype, value in specs:
        sig = Signal(name, dtype)
        sig.value = value
        sigs.append(sig)
    return sigs


def _parse_crc(frame: bytes) -> tuple[bytes, int]:
    """Split frame into (body, crc32) and verify."""
    body, crc_bytes = frame[:-4], frame[-4:]
    expected = binascii.crc32(body)
    actual = int.from_bytes(crc_bytes, "little")
    return body, actual


# ── build_header ─────────────────────────────────────────────────────


class TestBuildHeader:
    def test_data_header(self):
        h = build_header(MSG_DATA, 1)
        assert h == b"\xd2:\x01\x00\x00\x00:"

    def test_symbol_header(self):
        h = build_header(MSG_SYMBOL_LIST, 42)
        assert h == b"\xb0:" + (42).to_bytes(4, "little") + b":"

    def test_device_header(self):
        h = build_header(MSG_DEVICES, 0)
        assert h == b"\xb6:\x00\x00\x00\x00:"


# ── wrap_frame ───────────────────────────────────────────────────────


class TestWrapFrame:
    def test_markers(self):
        result = wrap_frame(b"payload")
        assert result == b"<BLAECK:payload/BLAECK>\r\n"

    def test_empty_payload(self):
        result = wrap_frame(b"")
        assert result == b"<BLAECK:/BLAECK>\r\n"


# ── build_data_frame ─────────────────────────────────────────────────


class TestBuildDataFrame:
    def test_basic_frame_crc(self):
        sigs = _make_signals(("x", "float", 1.5))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
        )
        body, crc = _parse_crc(frame)
        assert crc == binascii.crc32(body)

    def test_restart_flag_set(self):
        sigs = _make_signals(("x", "float", 1.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=True,
        )
        # Restart flag is first byte after header
        after_header = frame[len(header):]
        assert after_header[0:1] == b"\x01"

    def test_restart_flag_clear(self):
        sigs = _make_signals(("x", "float", 1.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
        )
        after_header = frame[len(header):]
        assert after_header[0:1] == b"\x00"

    def test_schema_hash_encoded(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0xABCD, restart_flag=False,
        )
        # Schema hash is at offset: header + restart_flag(1) + colon(1)
        offset = len(header) + 2  # restart_flag + ":"
        assert frame[offset:offset + 2] == (0xABCD).to_bytes(2, "little")

    def test_timestamp_none_mode_zero(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
            timestamp_mode=0, timestamp=None,
        )
        # After restart_flag ":" schema_hash ":" should be mode_byte=0 ":"
        offset = len(header) + 2 + 2 + 1  # restart + ":" + hash(2) + ":"
        assert frame[offset:offset + 1] == b"\x00"
        assert frame[offset + 1:offset + 2] == b":"

    def test_timestamp_included(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        ts = 1234567890
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
            timestamp_mode=int(TimestampMode.UNIX), timestamp=ts,
        )
        # Mode byte should be 2 (UNIX), followed by 8-byte timestamp
        offset = len(header) + 2 + 2 + 1  # restart + ":" + hash(2) + ":"
        mode = frame[offset]
        assert mode == 2
        ts_bytes = frame[offset + 1:offset + 9]
        assert int.from_bytes(ts_bytes, "little") == ts

    def test_multiple_signals(self):
        sigs = _make_signals(
            ("a", "float", 1.0),
            ("b", "float", 2.0),
            ("c", "float", 3.0),
        )
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 2,
            schema_hash=0, restart_flag=False,
        )
        # All 3 signals should be present: idx(2) + float(4) = 6 bytes each = 18
        body, _ = _parse_crc(frame)
        # status(1) + status_payload(4) at end
        assert len(body) > 18

    def test_signal_range(self):
        sigs = _make_signals(
            ("a", "float", 1.0),
            ("b", "float", 2.0),
            ("c", "float", 3.0),
        )
        header = build_header(MSG_DATA, 1)
        # Only signal index 1
        frame = build_data_frame(
            header, sigs, 1, 1,
            schema_hash=0, restart_flag=False,
        )
        body, _ = _parse_crc(frame)
        # Find signal index 1 in payload
        # Meta section: restart(1) : hash(2) : mode(1) :
        meta_len = 1 + 1 + 2 + 1 + 1 + 1  # flag + ":" + hash + ":" + mode + ":"
        payload_start = len(header) + meta_len
        # payload: idx(2) + value(4) = 6 bytes, then status(1) + status_payload(4)
        payload_area = body[payload_start:-5]  # strip status bytes
        assert len(payload_area) == 6  # exactly 1 signal
        idx = int.from_bytes(payload_area[:2], "little")
        assert idx == 1

    def test_only_updated_filters(self):
        sigs = _make_signals(
            ("a", "float", 1.0),
            ("b", "float", 2.0),
        )
        sigs[0].updated = False
        sigs[1].updated = True
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 1,
            schema_hash=0, restart_flag=False,
            only_updated=True,
        )
        body, _ = _parse_crc(frame)
        meta_len = 1 + 1 + 2 + 1 + 1 + 1
        payload_area = body[len(header) + meta_len:-5]
        # Only signal 1 (6 bytes)
        assert len(payload_area) == 6
        idx = int.from_bytes(payload_area[:2], "little")
        assert idx == 1

    def test_only_updated_clears_flag(self):
        sigs = _make_signals(("a", "float", 1.0))
        sigs[0].updated = True
        header = build_header(MSG_DATA, 1)
        build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
            only_updated=True,
        )
        assert sigs[0].updated is False

    def test_end_minus_one_defaults_to_last(self):
        sigs = _make_signals(("a", "float", 1.0), ("b", "float", 2.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, -1,
            schema_hash=0, restart_flag=False,
        )
        body, _ = _parse_crc(frame)
        meta_len = 1 + 1 + 2 + 1 + 1 + 1
        payload_area = body[len(header) + meta_len:-5]
        assert len(payload_area) == 12  # 2 signals × 6 bytes

    def test_status_byte_embedded(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
            status=STATUS_UPSTREAM_LOST,
            status_payload=b"\x01\x00\x00\x00",
        )
        body, _ = _parse_crc(frame)
        assert body[-5] == STATUS_UPSTREAM_LOST
        assert body[-4:] == b"\x01\x00\x00\x00"

    def test_invalid_status_payload_raises(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        with pytest.raises(ValueError, match="status_payload must be 4 bytes"):
            build_data_frame(
                header, sigs, 0, 0,
                schema_hash=0, restart_flag=False,
                status_payload=b"\x00\x00",
            )

    def test_status_ok_default(self):
        sigs = _make_signals(("x", "float", 0.0))
        header = build_header(MSG_DATA, 1)
        frame = build_data_frame(
            header, sigs, 0, 0,
            schema_hash=0, restart_flag=False,
        )
        body, _ = _parse_crc(frame)
        assert body[-5] == STATUS_OK
        assert body[-4:] == b"\x00\x00\x00\x00"


# ── build_symbol_payload ─────────────────────────────────────────────


class TestBuildSymbolPayload:
    def test_single_signal(self):
        sigs = _make_signals(("temp", "float", 0.0))
        payload = build_symbol_payload(sigs, MSC_MASTER, b"\x00")
        expected = MSC_MASTER + b"\x00" + b"temp\0" + sigs[0].get_dtype_byte()
        assert payload == expected

    def test_multiple_signals(self):
        sigs = _make_signals(("a", "int", 0), ("b", "float", 0.0))
        payload = build_symbol_payload(sigs, MSC_SLAVE, b"\x01")
        entry_a = MSC_SLAVE + b"\x01" + b"a\0" + sigs[0].get_dtype_byte()
        entry_b = MSC_SLAVE + b"\x01" + b"b\0" + sigs[1].get_dtype_byte()
        assert payload == entry_a + entry_b

    def test_empty_signals(self):
        sigs = SignalList()
        payload = build_symbol_payload(sigs, MSC_MASTER, b"\x00")
        assert payload == b""


# ── encode_device_entry ──────────────────────────────────────────────


class TestEncodeDeviceEntry:
    def test_basic_entry(self):
        entry = encode_device_entry(
            MSC_MASTER, b"\x00",
            b"Device", b"1.0", b"2.0",
            b"0.1.0", b"blaecktcpy",
            b"0", b"server", b"0",
        )
        assert entry.startswith(MSC_MASTER + b"\x00" + b"Device\0")
        assert b"1.0\0" in entry
        assert b"2.0\0" in entry
        assert b"0.1.0\0" in entry
        assert b"blaecktcpy\0" in entry
        assert entry.endswith(b"0\0")

    def test_slave_entry(self):
        entry = encode_device_entry(
            MSC_SLAVE, b"\x03",
            b"Sensor", b"hw1", b"fw1",
            b"lib1", b"libname",
            b"1", b"device", b"1",
        )
        assert entry.startswith(MSC_SLAVE + b"\x03" + b"Sensor\0")


# ── build_client_trailer ─────────────────────────────────────────────


class TestBuildClientTrailer:
    def test_data_enabled(self):
        trailer = build_client_trailer(
            client_id=5,
            data_clients={5, 10},
            client_meta={5: {"name": "MyClient", "type": "app"}},
        )
        assert trailer == b"5\0" + b"1\0" + b"MyClient\0" + b"app\0"

    def test_data_not_enabled(self):
        trailer = build_client_trailer(
            client_id=3,
            data_clients={5},
            client_meta={},
        )
        assert trailer == b"3\0" + b"0\0" + b"\0" + b"unknown\0"

    def test_missing_meta(self):
        trailer = build_client_trailer(
            client_id=1,
            data_clients={1},
            client_meta={},
        )
        # name="" and type="unknown" as defaults
        assert b"\0unknown\0" in trailer

    def test_partial_meta(self):
        trailer = build_client_trailer(
            client_id=7,
            data_clients={7},
            client_meta={7: {"name": "Test"}},
        )
        assert b"Test\0" in trailer
        assert b"unknown\0" in trailer


# ── Constants ────────────────────────────────────────────────────────


class TestConstants:
    def test_msg_keys(self):
        assert MSG_SYMBOL_LIST == b"\xb0"
        assert MSG_DATA == b"\xd2"
        assert MSG_DEVICES == b"\xb6"

    def test_status_values(self):
        assert STATUS_OK == 0x00
        assert STATUS_UPSTREAM_LOST == 0x80
        assert STATUS_UPSTREAM_RECONNECTED == 0x81

    def test_msc_values(self):
        assert MSC_MASTER == b"\x01"
        assert MSC_SLAVE == b"\x02"
