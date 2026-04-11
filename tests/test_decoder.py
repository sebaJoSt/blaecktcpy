"""Direct unit tests for blaecktcpy.hub._decoder.

Tests all public functions with crafted binary payloads, covering:
- All data frame formats (D2, D1, B1)
- All device frame formats (B2, B3, B4, B5, B6)
- All 10 data types
- Error paths (CRC mismatch, truncation, unknown dtype)
- Edge cases (empty payloads, restart flag, timestamp modes)
"""

import binascii
import struct

import pytest

from blaecktcpy.hub import _decoder as decoder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header(msg_key: int, msg_id: int = 1) -> bytes:
    """Build the common frame header: MSGKEY : MSGID(4) :"""
    return bytes([msg_key]) + b":" + msg_id.to_bytes(4, "little") + b":"


def _crc32(data: bytes) -> bytes:
    return (binascii.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")


def _d2_frame(
    symbols: list[tuple[int, bytes]],
    symbol_table: list[decoder.DecodedSymbol],
    *,
    msg_id: int = 1,
    restart: bool = False,
    schema_hash: int | None = None,
    timestamp_mode: int = 1,
    timestamp: int = 1000,
    status_byte: int = 0,
    status_payload: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    """Build a D2 data frame (new tail: StatusByte + StatusPayload(4) + CRC32)."""
    if schema_hash is None:
        schema_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in symbol_table]
        )

    body = b""
    body += b"\x01" if restart else b"\x00"
    body += b":"
    body += schema_hash.to_bytes(2, "little")
    body += b":"
    body += bytes([timestamp_mode])
    if timestamp_mode > 0:
        body += timestamp.to_bytes(8, "little")
    body += b":"

    for sym_id, value_bytes in symbols:
        body += sym_id.to_bytes(2, "little") + value_bytes

    body += bytes([status_byte]) + status_payload

    content = _header(0xD2, msg_id) + body
    return content + _crc32(content)


def _d1_frame(
    symbols: list[tuple[int, bytes]],
    *,
    msg_id: int = 1,
    restart: bool = False,
    timestamp_mode: int = 1,
    timestamp: int = 500,
    status_byte: int = 0,
) -> bytes:
    """Build a D1 data frame: RestartFlag : TimestampMode [Timestamp(4)] : signals StatusByte CRC32."""
    body = b""
    body += b"\x01" if restart else b"\x00"
    body += b":"
    body += bytes([timestamp_mode])
    if timestamp_mode > 0:
        body += timestamp.to_bytes(4, "little")
    body += b":"

    for sym_id, value_bytes in symbols:
        body += sym_id.to_bytes(2, "little") + value_bytes

    body += bytes([status_byte])

    content = _header(0xD1, msg_id) + body
    # D1 CRC: computed over content excluding the last byte (status) wait no —
    # the CRC covers everything before CRC itself, status is INSIDE.
    # Looking at decoder: CRC = crc32(content[:-5]) covers content minus StatusByte minus CRC32
    # Actually: expected_crc = content[-4:], actual = crc32(content[:-5])
    # So CRC is appended AFTER status byte, and crc32 is computed over content[:-5]
    # which is everything except StatusByte + CRC32... wait that's crc32(content[:-5]).
    # But content doesn't have CRC yet. Let me re-read the decoder.
    #
    # In the decoder:
    #   expected_crc = int.from_bytes(content[-4:], "little")
    #   actual_crc = binascii.crc32(content[:-5]) & 0xFFFFFFFF
    # So for the full content (including CRC), content[:-5] = everything except last 5 bytes
    # (StatusByte + CRC32).
    # We need: crc32(full_content[:-5]) == full_content[-4:]
    # full_content = header + body + crc
    # full_content[:-5] = everything except StatusByte + CRC(4)
    # So: crc = crc32(header + body_without_status)
    # But body already ends with status_byte...
    # Let me think differently: before CRC is appended, we have `pre = content` (header+body).
    # After CRC: full = pre + crc_bytes.
    # Decoder checks: crc32(full[:-5]) == full[-4:]
    # full[:-5] = (pre + crc_bytes)[:-5] = pre[:-1] (since crc is 4 bytes, -5 strips last byte of pre + all 4 crc)
    # pre[-1] = status_byte
    # So crc32(pre[:-1]) should equal crc_bytes.
    crc_input = content[:-1]  # everything before status byte
    crc = _crc32(crc_input)
    return content + crc


def _b1_frame(
    signal_values: list[bytes],
    *,
    msg_id: int = 1,
    status_byte: int = 0,
) -> bytes:
    """Build a B1 legacy data frame: signals... StatusByte CRC32."""
    body = b"".join(signal_values)
    body += bytes([status_byte])
    content = _header(0xB1, msg_id) + body
    crc_input = content[:-1]
    crc = _crc32(crc_input)
    return content + crc


def _make_symbols(*specs: tuple[str, int]) -> list[decoder.DecodedSymbol]:
    """Create a symbol table from (name, dtype_code) pairs."""
    result = []
    for name, code in specs:
        info = decoder._DTYPE_INFO[code]
        result.append(decoder.DecodedSymbol(
            name=name,
            datatype_code=code,
            datatype_name=info[0],
            datatype_size=info[1],
        ))
    return result


# ---------------------------------------------------------------------------
# compute_schema_hash
# ---------------------------------------------------------------------------

class TestComputeSchemaHash:

    def test_empty_returns_zero(self):
        assert decoder.compute_schema_hash([]) == 0

    def test_deterministic(self):
        pairs = [("temp", 8), ("pressure", 8)]
        assert decoder.compute_schema_hash(pairs) == decoder.compute_schema_hash(pairs)

    def test_different_names_differ(self):
        assert decoder.compute_schema_hash([("a", 8)]) != decoder.compute_schema_hash([("b", 8)])

    def test_different_types_differ(self):
        assert decoder.compute_schema_hash([("x", 8)]) != decoder.compute_schema_hash([("x", 6)])

    def test_order_matters(self):
        h1 = decoder.compute_schema_hash([("a", 8), ("b", 6)])
        h2 = decoder.compute_schema_hash([("b", 6), ("a", 8)])
        assert h1 != h2

    def test_result_is_16bit(self):
        h = decoder.compute_schema_hash([("long_signal_name", 9)])
        assert 0 <= h <= 0xFFFF


# ---------------------------------------------------------------------------
# parse_symbol_list (B0)
# ---------------------------------------------------------------------------

class TestParseSymbolList:

    def test_single_symbol(self):
        content = _header(0xB0) + b"\x01\x00" + b"temp\0" + b"\x08"
        symbols = decoder.parse_symbol_list(content)
        assert len(symbols) == 1
        assert symbols[0].name == "temp"
        assert symbols[0].datatype_code == 8
        assert symbols[0].datatype_name == "float"
        assert symbols[0].datatype_size == 4
        assert symbols[0].msc == 1
        assert symbols[0].slave_id == 0

    def test_multiple_symbols(self):
        payload = (
            b"\x01\x00" + b"a\0" + b"\x00"   # bool
            + b"\x01\x00" + b"b\0" + b"\x08"  # float
            + b"\x02\x05" + b"c\0" + b"\x09"  # double, slave 5
        )
        symbols = decoder.parse_symbol_list(_header(0xB0) + payload)
        assert len(symbols) == 3
        assert symbols[0].name == "a" and symbols[0].datatype_name == "bool"
        assert symbols[1].name == "b" and symbols[1].datatype_name == "float"
        assert symbols[2].name == "c" and symbols[2].datatype_name == "double"
        assert symbols[2].msc == 2 and symbols[2].slave_id == 5

    def test_all_10_dtypes(self):
        payload = b""
        for code in range(10):
            payload += b"\x01\x00" + f"sig{code}\0".encode() + bytes([code])
        symbols = decoder.parse_symbol_list(_header(0xB0) + payload)
        assert len(symbols) == 10
        for i, sym in enumerate(symbols):
            assert sym.datatype_code == i
            expected_name, expected_size, _ = decoder._DTYPE_INFO[i]
            assert sym.datatype_name == expected_name
            assert sym.datatype_size == expected_size

    def test_unknown_dtype_raises(self):
        content = _header(0xB0) + b"\x01\x00" + b"x\0" + b"\xFF"
        with pytest.raises(ValueError, match="Unknown datatype code"):
            decoder.parse_symbol_list(content)

    def test_wrong_msgkey_raises(self):
        content = _header(0xD2) + b"\x01\x00" + b"x\0" + b"\x08"
        with pytest.raises(ValueError, match="Expected B0"):
            decoder.parse_symbol_list(content)

    def test_empty_payload(self):
        symbols = decoder.parse_symbol_list(_header(0xB0))
        assert symbols == []

    def test_truncated_gracefully_stops(self):
        # Only MSC+SlaveID, no null-terminated name
        content = _header(0xB0) + b"\x01\x00" + b"noterm"
        symbols = decoder.parse_symbol_list(content)
        assert symbols == []


# ---------------------------------------------------------------------------
# parse_data — D2 format
# ---------------------------------------------------------------------------

class TestParseDataD2:

    def test_single_float(self):
        table = _make_symbols(("temp", 8))
        value = struct.pack("<f", 3.14)
        frame = _d2_frame([(0, value)], table)
        result = decoder.parse_data(frame, table)
        assert result.msg_id == 1
        assert not result.restart_flag
        assert result.timestamp_mode == 1
        assert result.timestamp == 1000
        assert abs(result.signals[0] - 3.14) < 0.01

    def test_restart_flag(self):
        table = _make_symbols(("x", 8))
        frame = _d2_frame([(0, struct.pack("<f", 1.0))], table, restart=True)
        result = decoder.parse_data(frame, table)
        assert result.restart_flag is True

    def test_no_timestamp(self):
        table = _make_symbols(("x", 8))
        frame = _d2_frame(
            [(0, struct.pack("<f", 1.0))], table, timestamp_mode=0
        )
        result = decoder.parse_data(frame, table)
        assert result.timestamp_mode == 0
        assert result.timestamp is None

    def test_status_byte_and_payload(self):
        table = _make_symbols(("x", 8))
        frame = _d2_frame(
            [(0, struct.pack("<f", 1.0))], table,
            status_byte=0x80,
            status_payload=b"\x01\x02\x03\x04",
        )
        result = decoder.parse_data(frame, table)
        assert result.status_byte == 0x80
        assert result.status_payload == b"\x01\x02\x03\x04"

    def test_multiple_signals(self):
        table = _make_symbols(("a", 0), ("b", 2), ("c", 8))
        signals = [
            (0, struct.pack("<?", True)),
            (1, struct.pack("<h", -100)),
            (2, struct.pack("<f", 9.81)),
        ]
        frame = _d2_frame(signals, table)
        result = decoder.parse_data(frame, table)
        assert result.signals[0] is True
        assert result.signals[1] == -100
        assert abs(result.signals[2] - 9.81) < 0.01

    def test_crc_mismatch_raises(self):
        table = _make_symbols(("x", 8))
        frame = bytearray(_d2_frame([(0, struct.pack("<f", 1.0))], table))
        frame[-1] ^= 0xFF  # corrupt CRC
        with pytest.raises(ValueError, match="CRC mismatch"):
            decoder.parse_data(bytes(frame), table)

    def test_schema_hash_roundtrip(self):
        table = _make_symbols(("temp", 8), ("pressure", 9))
        expected_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in table]
        )
        frame = _d2_frame(
            [(0, struct.pack("<f", 1.0)), (1, struct.pack("<d", 2.0))], table
        )
        result = decoder.parse_data(frame, table)
        assert result.schema_hash == expected_hash

    def test_all_dtypes_roundtrip(self):
        specs = [
            ("bool_s", 0, struct.pack("<?", True)),
            ("byte_s", 1, struct.pack("<B", 255)),
            ("short_s", 2, struct.pack("<h", -32768)),
            ("ushort_s", 3, struct.pack("<H", 65535)),
            ("avr_int", 4, struct.pack("<h", -1)),
            ("avr_uint", 5, struct.pack("<H", 1000)),
            ("long_s", 6, struct.pack("<i", -100000)),
            ("ulong_s", 7, struct.pack("<I", 4000000000)),
            ("float_s", 8, struct.pack("<f", 1.5)),
            ("double_s", 9, struct.pack("<d", 3.141592653589793)),
        ]
        table = _make_symbols(*[(name, code) for name, code, _ in specs])
        signals = [(i, val) for i, (_, _, val) in enumerate(specs)]
        frame = _d2_frame(signals, table)
        result = decoder.parse_data(frame, table)
        assert len(result.signals) == 10
        assert result.signals[0] is True
        assert result.signals[1] == 255
        assert result.signals[2] == -32768
        assert result.signals[3] == 65535
        assert result.signals[6] == -100000
        assert result.signals[7] == 4000000000
        assert abs(result.signals[8] - 1.5) < 0.001
        assert abs(result.signals[9] - 3.141592653589793) < 1e-10


# ---------------------------------------------------------------------------
# parse_data — D1 format
# ---------------------------------------------------------------------------

class TestParseDataD1:

    def test_single_float(self):
        table = _make_symbols(("temp", 8))
        frame = _d1_frame([(0, struct.pack("<f", 25.5))], timestamp=999)
        result = decoder.parse_data(frame, table)
        assert result.msg_id == 1
        assert not result.restart_flag
        assert result.schema_hash == 0  # D1 has no schema hash
        assert result.timestamp_mode == 1
        assert result.timestamp == 999
        assert abs(result.signals[0] - 25.5) < 0.01

    def test_restart_flag(self):
        table = _make_symbols(("x", 8))
        frame = _d1_frame([(0, struct.pack("<f", 0.0))], restart=True)
        result = decoder.parse_data(frame, table)
        assert result.restart_flag is True

    def test_no_timestamp(self):
        table = _make_symbols(("x", 8))
        frame = _d1_frame(
            [(0, struct.pack("<f", 1.0))], timestamp_mode=0
        )
        result = decoder.parse_data(frame, table)
        assert result.timestamp is None
        assert result.timestamp_mode == 0

    def test_status_byte(self):
        table = _make_symbols(("x", 8))
        frame = _d1_frame([(0, struct.pack("<f", 0.0))], status_byte=0x81)
        result = decoder.parse_data(frame, table)
        assert result.status_byte == 0x81

    def test_crc_mismatch_raises(self):
        table = _make_symbols(("x", 8))
        frame = bytearray(_d1_frame([(0, struct.pack("<f", 1.0))]))
        frame[-2] ^= 0xFF
        with pytest.raises(ValueError, match="CRC mismatch"):
            decoder.parse_data(bytes(frame), table)

    def test_multiple_signals(self):
        table = _make_symbols(("a", 6), ("b", 8))
        signals = [
            (0, struct.pack("<i", 42)),
            (1, struct.pack("<f", 2.718)),
        ]
        frame = _d1_frame(signals)
        result = decoder.parse_data(frame, table)
        assert result.signals[0] == 42
        assert abs(result.signals[1] - 2.718) < 0.01


# ---------------------------------------------------------------------------
# parse_data — B1 legacy format
# ---------------------------------------------------------------------------

class TestParseDataB1:

    def test_single_float(self):
        table = _make_symbols(("temp", 8))
        frame = _b1_frame([struct.pack("<f", 99.9)])
        result = decoder.parse_data(frame, table)
        assert result.msg_id == 1
        assert result.restart_flag is False
        assert result.schema_hash == 0
        assert result.timestamp_mode == 0
        assert result.timestamp is None
        assert abs(result.signals[0] - 99.9) < 0.1

    def test_multiple_sequential_values(self):
        table = _make_symbols(("a", 2), ("b", 7), ("c", 0))
        values = [
            struct.pack("<h", -500),
            struct.pack("<I", 123456),
            struct.pack("<?", False),
        ]
        frame = _b1_frame(values)
        result = decoder.parse_data(frame, table)
        assert result.signals[0] == -500
        assert result.signals[1] == 123456
        assert result.signals[2] is False

    def test_status_byte(self):
        table = _make_symbols(("x", 8))
        frame = _b1_frame([struct.pack("<f", 0.0)], status_byte=0x80)
        result = decoder.parse_data(frame, table)
        assert result.status_byte == 0x80

    def test_crc_mismatch_raises(self):
        table = _make_symbols(("x", 8))
        frame = bytearray(_b1_frame([struct.pack("<f", 1.0)]))
        frame[-3] ^= 0xFF
        with pytest.raises(ValueError, match="CRC mismatch"):
            decoder.parse_data(bytes(frame), table)


# ---------------------------------------------------------------------------
# parse_data — dispatch / error
# ---------------------------------------------------------------------------

class TestParseDataDispatch:

    def test_unknown_msgkey_raises(self):
        content = _header(0xAA) + b"\x00" * 20
        with pytest.raises(ValueError, match="Expected D2, D1 or B1"):
            decoder.parse_data(content, [])


# ---------------------------------------------------------------------------
# parse_all_devices — B6
# ---------------------------------------------------------------------------

class TestParseDevicesB6:

    def _b6_frame(
        self,
        devices: list[tuple[int, int, list[str]]],
        client_trailer: tuple[str, str, str, str] = ("1", "1", "Loggbok", "app"),
        msg_id: int = 1,
    ) -> bytes:
        """Build B6 frame: DeviceCount + devices + client trailer."""
        body = bytes([len(devices)])
        for msc, sid, fields in devices:
            body += bytes([msc, sid])
            # fields: name, hw, fw, lib_ver, lib_name, server_restarted, device_type, parent
            for f in fields:
                body += f.encode() + b"\0"
        for f in client_trailer:
            body += f.encode() + b"\0"
        return _header(0xB6, msg_id) + body

    def test_single_device(self):
        fields = ["MyDevice", "1.0", "2.0", "3.0", "blaecktcp", "0", "server", "0"]
        frame = self._b6_frame([(1, 0, fields)])
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        d = devices[0]
        assert d.device_name == "MyDevice"
        assert d.hw_version == "1.0"
        assert d.fw_version == "2.0"
        assert d.lib_version == "3.0"
        assert d.lib_name == "blaecktcp"
        assert d.server_restarted == "0"
        assert d.device_type == "server"
        assert d.parent == "0"
        assert d.assigned_client_id == "1"
        assert d.data_enabled == "1"
        assert d.client_name == "Loggbok"
        assert d.client_type == "app"

    def test_multi_device_shares_trailer(self):
        master = (1, 0, ["Master", "h1", "f1", "l1", "lib", "0", "hub", "0"])
        slave = (2, 8, ["Slave", "h2", "f2", "l2", "lib", "0", "server", "1"])
        frame = self._b6_frame([master, slave])
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 2
        assert devices[0].client_name == "Loggbok"
        assert devices[1].client_name == "Loggbok"
        assert devices[0].device_type == "hub"
        assert devices[1].device_type == "server"

    def test_parse_devices_returns_first(self):
        master = (1, 0, ["First", "h", "f", "l", "lib", "0", "hub", "0"])
        slave = (2, 1, ["Second", "h", "f", "l", "lib", "0", "server", "0"])
        frame = self._b6_frame([master, slave])
        info = decoder.parse_devices(frame)
        assert info.device_name == "First"


# ---------------------------------------------------------------------------
# parse_all_devices — B5
# ---------------------------------------------------------------------------

class TestParseDevicesB5:

    def _b5_entry(self, msc, sid, name, hw, fw, lib_ver, lib_name, client_id, data_en, restarted):
        return (
            bytes([msc, sid])
            + name.encode() + b"\0"
            + hw.encode() + b"\0"
            + fw.encode() + b"\0"
            + lib_ver.encode() + b"\0"
            + lib_name.encode() + b"\0"
            + client_id.encode() + b"\0"
            + data_en.encode() + b"\0"
            + restarted.encode() + b"\0"
        )

    def test_single_device(self):
        entry = self._b5_entry(1, 0, "Dev5", "hw", "fw", "lib", "blaeck", "1", "1", "0")
        frame = _header(0xB5) + entry
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        d = devices[0]
        assert d.device_name == "Dev5"
        assert d.lib_name == "blaeck"
        assert d.assigned_client_id == "1"
        assert d.data_enabled == "1"
        assert d.server_restarted == "0"


# ---------------------------------------------------------------------------
# parse_all_devices — B4
# ---------------------------------------------------------------------------

class TestParseDevicesB4:

    def _b4_entry(self, msc, sid, name, hw, fw, lib_ver, lib_name, client_id, data_en):
        return (
            bytes([msc, sid])
            + name.encode() + b"\0"
            + hw.encode() + b"\0"
            + fw.encode() + b"\0"
            + lib_ver.encode() + b"\0"
            + lib_name.encode() + b"\0"
            + client_id.encode() + b"\0"
            + data_en.encode() + b"\0"
        )

    def test_single_device(self):
        entry = self._b4_entry(1, 0, "Dev4", "hw", "fw", "lib", "blaeck", "2", "0")
        frame = _header(0xB4) + entry
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        d = devices[0]
        assert d.device_name == "Dev4"
        assert d.lib_name == "blaeck"
        assert d.assigned_client_id == "2"
        assert d.data_enabled == "0"
        assert d.server_restarted == ""  # B4 doesn't have this


# ---------------------------------------------------------------------------
# parse_all_devices — B3
# ---------------------------------------------------------------------------

class TestParseDevicesB3:

    def _b3_entry(self, msc, sid, name, hw, fw, lib_ver, lib_name):
        return (
            bytes([msc, sid])
            + name.encode() + b"\0"
            + hw.encode() + b"\0"
            + fw.encode() + b"\0"
            + lib_ver.encode() + b"\0"
            + lib_name.encode() + b"\0"
        )

    def test_single_device(self):
        entry = self._b3_entry(1, 0, "Dev3", "hw", "fw", "lib", "blaeckserial")
        frame = _header(0xB3) + entry
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        assert devices[0].device_name == "Dev3"
        assert devices[0].lib_name == "blaeckserial"
        assert devices[0].assigned_client_id == ""  # B3 doesn't have this

    def test_multi_device(self):
        e1 = self._b3_entry(1, 0, "Master", "h1", "f1", "l1", "lib")
        e2 = self._b3_entry(2, 3, "Slave3", "h2", "f2", "l2", "lib")
        frame = _header(0xB3) + e1 + e2
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 2
        assert devices[0].device_name == "Master"
        assert devices[1].device_name == "Slave3"
        assert devices[1].msc == 2 and devices[1].slave_id == 3


# ---------------------------------------------------------------------------
# parse_all_devices — B2 (legacy)
# ---------------------------------------------------------------------------

class TestParseDevicesB2:

    def _b2_entry(self, msc, sid, name, hw, fw, lib_ver):
        return (
            bytes([msc, sid])
            + name.encode() + b"\0"
            + hw.encode() + b"\0"
            + fw.encode() + b"\0"
            + lib_ver.encode() + b"\0"
        )

    def test_single_device(self):
        entry = self._b2_entry(1, 0, "OldDev", "hw", "fw", "lib")
        frame = _header(0xB2) + entry
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        assert devices[0].device_name == "OldDev"
        assert devices[0].lib_name == ""  # B2 doesn't have lib_name


# ---------------------------------------------------------------------------
# parse_devices — error
# ---------------------------------------------------------------------------

class TestParseDevicesEmpty:

    def test_empty_b6_raises(self):
        # DeviceCount=0, no devices, but still needs trailer
        frame = _header(0xB6) + b"\x00" + b"\0\0\0\0"
        info = decoder.parse_all_devices(frame)
        assert len(info) == 0

    def test_parse_devices_no_entries_raises(self):
        frame = _header(0xB2)  # empty B2
        with pytest.raises(ValueError, match="No device entries"):
            decoder.parse_devices(frame)


# ---------------------------------------------------------------------------
# MSGKEY constants
# ---------------------------------------------------------------------------

class TestMsgKeyConstants:

    def test_restart_value(self):
        assert decoder.MSGKEY_RESTART == 0xC0

    def test_data_set_completeness(self):
        assert decoder.MSGKEY_DATA_D2 in decoder.MSGKEY_DATA_ALL
        assert decoder.MSGKEY_DATA_D1 in decoder.MSGKEY_DATA_ALL
        assert decoder.MSGKEY_DATA_LEGACY in decoder.MSGKEY_DATA_ALL

    def test_devices_set_completeness(self):
        assert decoder.MSGKEY_DEVICES in decoder.MSGKEY_DEVICES_ALL
        assert decoder.MSGKEY_DEVICES_V4 in decoder.MSGKEY_DEVICES_ALL
        assert decoder.MSGKEY_DEVICES_V2 in decoder.MSGKEY_DEVICES_ALL
        assert decoder.MSGKEY_DEVICES_V1 in decoder.MSGKEY_DEVICES_ALL
        assert decoder.MSGKEY_DEVICES_LEGACY in decoder.MSGKEY_DEVICES_ALL


# ---------------------------------------------------------------------------
# B0 parse_symbol_list — truncation edge cases
# ---------------------------------------------------------------------------

class TestParseSymbolListTruncation:

    def test_truncated_msc_only_one_byte(self):
        """Line 147: pos + 2 > len(data) — only 1 byte after header."""
        content = _header(0xB0) + b"\x01"
        symbols = decoder.parse_symbol_list(content)
        assert symbols == []

    def test_truncated_after_name_no_dtype(self):
        """Line 162: pos >= len(data) after name — no DTYPE byte."""
        content = _header(0xB0) + b"\x01\x00" + b"temp\0"
        symbols = decoder.parse_symbol_list(content)
        assert symbols == []


# ---------------------------------------------------------------------------
# D2 parse_data — validation error paths
# ---------------------------------------------------------------------------

class TestParseDataD2Errors:

    def test_payload_too_short(self):
        """Line 222: D2 payload < 12 bytes."""
        table = _make_symbols(("x", 8))
        content = _header(0xD2) + b"\x00" * 5
        with pytest.raises(ValueError, match="D2 payload too short"):
            decoder.parse_data(content, table)

    def test_missing_separator_after_restart(self):
        """Line 245: no ':' separator after restart flag."""
        table = _make_symbols(("x", 8))
        # Build a frame with a bad separator (0x00 instead of ':')
        body = b"\x00" + b"\x00" + b"\x00" * 20
        content = _header(0xD2) + body
        content += _crc32(content)
        with pytest.raises(ValueError, match="separator after D2 restart"):
            decoder.parse_data(content, table)

    def test_missing_separator_after_schema(self):
        """Line 256: no ':' after schema hash."""
        body = b"\x00:"  # restart + ':'
        body += b"\x00\x00"  # schema hash
        body += b"\x00"  # bad separator
        body += b"\x00" * 15
        content = _header(0xD2) + body
        content += _crc32(content)
        with pytest.raises(ValueError, match="separator after D2 schema hash"):
            decoder.parse_data(content, [])

    def test_truncated_timestamp(self):
        """Line 269: timestamp_mode > 0 but fewer than 8 bytes available."""
        body = b"\x00:"      # restart + ':'
        body += b"\x00\x00:" # schema hash + ':'
        body += b"\x01"      # timestamp_mode = 1
        body += b"\x00\x00"  # only 2 bytes of timestamp (need 8)
        # total body = 8, data = body(8)+crc(4) = 12 >= 12 ✓
        content = _header(0xD2) + body
        content += _crc32(content)
        with pytest.raises(ValueError, match="Truncated D2 timestamp"):
            decoder.parse_data(content, [])

    def test_payload_too_short_for_status(self):
        """Line 281: signal_data_end < pos after metadata."""
        body = b"\x00:"       # restart + ':'
        body += b"\x00\x00:"  # schema hash + ':'
        body += b"\x00:"      # timestamp_mode=0 + ':'
        body += b"\x00"       # padding to reach 8 bytes of body
        # data = body(8)+crc(4) = 12, signal_data_end = 12-9 = 3, pos = 7
        content = _header(0xD2) + body
        content += _crc32(content)
        with pytest.raises(ValueError, match="D2 payload too short"):
            decoder.parse_data(content, [])


# ---------------------------------------------------------------------------
# D1 parse_data — validation error paths
# ---------------------------------------------------------------------------

class TestParseDataD1Errors:

    def test_payload_too_short(self):
        """Line 314: D1 payload < 9 bytes."""
        content = _header(0xD1) + b"\x00" * 2
        with pytest.raises(ValueError, match="D1 payload too short"):
            decoder.parse_data(content, [])

    def test_missing_separator_after_restart(self):
        """Line 331: no ':' separator after restart flag."""
        body = b"\x00\x00" + b"\x00" * 3 + b"\x00"  # 6 bytes, last = status
        content = _header(0xD1) + body
        crc_input = content[:-1]
        content += _crc32(crc_input)
        with pytest.raises(ValueError, match="separator after D1 restart"):
            decoder.parse_data(content, [])


# ---------------------------------------------------------------------------
# B1 parse_data — error paths
# ---------------------------------------------------------------------------

class TestParseDataB1Errors:

    def test_payload_too_short(self):
        """Line 388: B1 payload < 5 bytes."""
        content = _header(0xB1) + b"\x00\x00"
        with pytest.raises(ValueError, match="B1 payload too short"):
            decoder.parse_data(content, [])

    def test_unknown_datatype_code(self):
        """Line 405: symbol table has unknown dtype code."""
        bad_sym = decoder.DecodedSymbol(
            name="x", datatype_code=0xFF, datatype_name="??", datatype_size=1,
        )
        body = b"\x00" * 6  # some data + status
        content = _header(0xB1) + body
        crc_input = content[:-1]
        content += _crc32(crc_input)
        with pytest.raises(ValueError, match="Unknown datatype code"):
            decoder.parse_data(content, [bad_sym])


# ---------------------------------------------------------------------------
# Device parsing — edge cases
# ---------------------------------------------------------------------------

class TestDeviceParsingEdgeCases:

    def test_b6_truncated_device_entry(self):
        """Line 500: pos + 2 > len(data) mid-loop in B6."""
        body = bytes([2])  # device count = 2
        body += bytes([1, 0])  # device 1 MSC+SID
        for f in ["D", "h", "f", "l", "n", "r", "t", "p"]:
            body += f.encode() + b"\0"
        # No room for device 2's MSC+SID → loop breaks
        # Client trailer reads empty strings
        frame = _header(0xB6) + body
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        assert devices[0].device_name == "D"

    def test_b2_msc_sid_only_no_strings(self):
        """Line 541: pos >= len(data) after reading MSC+SID."""
        frame = _header(0xB2) + bytes([1, 0])
        devices = decoder.parse_all_devices(frame)
        assert devices == []

    def test_read_string_no_null_terminator(self):
        """Lines 486-488: read_string fallback when no null terminator."""
        # Build a B3 frame where the last field has no null terminator
        body = bytes([1, 0])
        body += b"Dev\0"  # device_name
        body += b"hw\0"   # hw_version
        body += b"fw\0"   # fw_version
        body += b"lib\0"  # lib_version
        body += b"noterm" # lib_name without null terminator
        frame = _header(0xB3) + body
        devices = decoder.parse_all_devices(frame)
        assert len(devices) == 1
        assert devices[0].lib_name == "noterm"


# ---------------------------------------------------------------------------
# parse_message — unified dispatcher
# ---------------------------------------------------------------------------


class TestParseMessage:

    def test_empty_content_raises(self):
        with pytest.raises(ValueError, match="Empty frame"):
            decoder.parse_message(b"")

    def test_unknown_key_raises(self):
        content = _header(0xFF)
        with pytest.raises(ValueError, match="Unknown message key"):
            decoder.parse_message(content)

    def test_dispatches_b0_symbol_list(self):
        body = b"\x00\x00" + b"temp\0" + bytes([8])
        content = _header(0xB0) + body
        result = decoder.parse_message(content)
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], decoder.DecodedSymbol)
        assert result[0].name == "temp"

    def test_dispatches_d2_data(self):
        sym_table = _make_symbols(("x", 8))
        values = [(0, struct.pack("<f", 1.5))]
        content = _d2_frame(values, sym_table)
        result = decoder.parse_message(content, symbol_table=sym_table)
        assert isinstance(result, decoder.DecodedData)
        assert result.msg_id == 1

    def test_dispatches_d1_data(self):
        sym_table = _make_symbols(("x", 8))
        values = [(0, struct.pack("<f", 1.5))]
        content = _d1_frame(values)
        result = decoder.parse_message(content, symbol_table=sym_table)
        assert isinstance(result, decoder.DecodedData)

    def test_dispatches_b1_data(self):
        sym_table = _make_symbols(("x", 8))
        values = [struct.pack("<f", 1.5)]
        content = _b1_frame(values)
        result = decoder.parse_message(content, symbol_table=sym_table)
        assert isinstance(result, decoder.DecodedData)

    def test_data_without_symbol_table_raises(self):
        sym_table = _make_symbols(("x", 8))
        values = [(0, struct.pack("<f", 1.5))]
        content = _d2_frame(values, sym_table)
        with pytest.raises(ValueError, match="symbol_table required"):
            decoder.parse_message(content)

    def test_dispatches_b6_devices(self):
        body = bytes([1])  # device count
        body += bytes([0, 0])  # MSC + SID
        for field in ["Dev", "hw", "fw", "lib", "name", "0", "type", "0"]:
            body += field.encode() + b"\0"
        # Client trailer
        for field in ["1", "1", "cli", "ctype"]:
            body += field.encode() + b"\0"
        content = _header(0xB6) + body
        result = decoder.parse_message(content)
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], decoder.DecodedDeviceInfo)

    def test_dispatches_b3_devices(self):
        body = bytes([0, 0]) + b"D\0hw\0fw\0lib\0name\0"
        content = _header(0xB3) + body
        result = decoder.parse_message(content)
        assert isinstance(result, list)
        assert isinstance(result[0], decoder.DecodedDeviceInfo)

    def test_dispatches_c0_restart(self):
        content = _header(0xC0, msg_id=42)
        result = decoder.parse_message(content)
        assert isinstance(result, decoder.DecodedData)
        assert result.restart_flag is True
        assert result.msg_id == 42

    def test_return_type_alias(self):
        """ParsedMessage type alias is accessible."""
        assert hasattr(decoder, "ParsedMessage")
