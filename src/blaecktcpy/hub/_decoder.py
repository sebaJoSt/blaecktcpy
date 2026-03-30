"""BlaeckTCP binary frame decoder.

Parses <BLAECK:…/BLAECK> binary protocol frames from upstream devices.
Supports B0 (symbol list), D1 (v5 data), B1 (v4 legacy data),
and B5 (device info) message types.
"""

import binascii
import struct
from dataclasses import dataclass, field


# Message keys
MSGKEY_SYMBOL_LIST = 0xB0
MSGKEY_DATA_LEGACY = 0xB1  # v4.0.1 or older
MSGKEY_DATA = 0xD1  # v5+
MSGKEY_DEVICES_LEGACY = 0xB2  # BlaeckSerial v3.0.3 or older
MSGKEY_DEVICES_V1 = 0xB3  # BlaeckSerial v3+ / BlaeckTCP v1
MSGKEY_DEVICES_V2 = 0xB4  # BlaeckTCP v2
MSGKEY_DEVICES_V4 = 0xB5  # BlaeckTCP v3
MSGKEY_DEVICES = 0xB6  # BlaeckTCP v4+

# Grouped sets for dispatch
MSGKEY_DATA_ALL = {MSGKEY_DATA, MSGKEY_DATA_LEGACY}
MSGKEY_DEVICES_ALL = {
    MSGKEY_DEVICES,
    MSGKEY_DEVICES_V4,
    MSGKEY_DEVICES_V2,
    MSGKEY_DEVICES_V1,
    MSGKEY_DEVICES_LEGACY,
}

# Datatype code → (name, byte size, struct format)
_DTYPE_INFO = {
    0: ("bool", 1, "<?"),
    1: ("byte", 1, "<B"),
    2: ("short", 2, "<h"),
    3: ("unsigned short", 2, "<H"),
    4: ("int", 2, "<h"),  # AVR 2-byte int
    5: ("unsigned int", 2, "<H"),  # AVR 2-byte unsigned int
    6: ("long", 4, "<i"),
    7: ("unsigned long", 4, "<I"),
    8: ("float", 4, "<f"),
    9: ("double", 8, "<d"),
}

# Maps upstream DTYPE codes to BlaeckServer-compatible datatype strings.
# AVR int/uint (2 bytes) map to short/unsigned short since BlaeckServer
# always treats int as 4 bytes (running on 32/64-bit Python).
DTYPE_TO_SIGNAL_TYPE = {
    0: "bool",
    1: "byte",
    2: "short",
    3: "unsigned short",
    4: "short",  # AVR int (2 bytes) → short
    5: "unsigned short",  # AVR unsigned int (2 bytes) → unsigned short
    6: "long",
    7: "unsigned long",
    8: "float",
    9: "double",
}


@dataclass
class DecodedSymbol:
    """A signal definition from a B0 symbol list frame."""

    name: str
    datatype_code: int
    datatype_name: str
    datatype_size: int
    msc: int = 0
    slave_id: int = 0


@dataclass
class DecodedData:
    """Decoded data from a D1/B1 data frame."""

    msg_id: int
    restart_flag: bool
    timestamp_mode: int
    timestamp: int | None
    status_byte: int = 0
    signals: dict[int, float | int | bool] = field(default_factory=dict)


@dataclass
class DecodedDeviceInfo:
    """Device info from a B3/B6 devices frame."""

    msg_id: int
    device_name: str
    hw_version: str
    fw_version: str
    lib_version: str
    lib_name: str = ""
    assigned_client_id: str = ""
    data_enabled: str = ""
    server_restarted: str = ""
    device_type: str = ""
    parent: str = "0"
    msc: int = 0
    slave_id: int = 0


def _parse_header(content: bytes) -> tuple[int, int, bytes]:
    """Parse common header: MSGKEY : MSGID(4) : rest"""
    msg_key = content[0]
    # content[1] == ':' (0x3A)
    msg_id = int.from_bytes(content[2:6], "little")
    # content[6] == ':'
    return msg_key, msg_id, content[7:]


def parse_symbol_list(content: bytes) -> list[DecodedSymbol]:
    """Parse a B0 symbol list frame.

    Args:
        content: bytes between <BLAECK: and /BLAECK>

    Returns:
        List of decoded signal definitions.
    """
    msg_key, msg_id, data = _parse_header(content)
    if msg_key != MSGKEY_SYMBOL_LIST:
        raise ValueError(f"Expected B0 symbol list, got {msg_key:#x}")

    symbols = []
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data):
            break
        # MasterSlaveConfig (1 byte) + SlaveID (1 byte)
        msc = data[pos]
        sid = data[pos + 1]
        pos += 2

        # Signal name: null-terminated string
        null_pos = data.find(b"\x00", pos)
        if null_pos == -1:
            break
        name = data[pos:null_pos].decode("utf-8", errors="replace")
        pos = null_pos + 1

        # DTYPE (1 byte)
        if pos >= len(data):
            break
        dtype_code = data[pos]
        pos += 1

        info = _DTYPE_INFO.get(dtype_code)
        if info:
            dtype_name, dtype_size, _ = info
        else:
            dtype_name = f"unknown({dtype_code})"
            dtype_size = 0

        symbols.append(
            DecodedSymbol(
                name=name,
                datatype_code=dtype_code,
                datatype_name=dtype_name,
                datatype_size=dtype_size,
                msc=msc,
                slave_id=sid,
            )
        )

    return symbols


def parse_data(content: bytes, symbol_table: list[DecodedSymbol]) -> DecodedData:
    """Parse a D1 (v5) or B1 (v4) data frame.

    Args:
        content: bytes between <BLAECK: and /BLAECK>
        symbol_table: signal definitions from a prior B0 parse

    Returns:
        DecodedData with signal index → value mapping.
    """
    msg_key, msg_id, data = _parse_header(content)
    _validate_data_frame(content)

    match msg_key:
        case 0xD1:  # MSGKEY_DATA (v5+)
            return _parse_data_d1(msg_id, data, symbol_table)
        case 0xB1:  # MSGKEY_DATA_LEGACY (v4)
            return _parse_data_b1(msg_id, data, symbol_table)
        case _:
            raise ValueError(f"Expected D1 or B1 data frame, got {msg_key:#x}")


def _validate_data_frame(content: bytes) -> None:
    """Validate minimum structure and CRC for a D1/B1 data frame."""
    if len(content) < 12:
        raise ValueError(f"Data frame too short: {len(content)} bytes")

    expected_crc = int.from_bytes(content[-4:], "little")
    actual_crc = binascii.crc32(content[:-5]) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError(
            f"CRC mismatch: expected 0x{expected_crc:08x}, got 0x{actual_crc:08x}"
        )


def _parse_data_d1(
    msg_id: int, data: bytes, symbol_table: list[DecodedSymbol]
) -> DecodedData:
    """Parse D1 format: RestartFlag : TimestampMode [Timestamp] : signals... StatusByte CRC32"""
    pos = 0

    # Restart flag (1 byte)
    restart_flag = data[pos] != 0
    pos += 1

    # ':' separator
    pos += 1

    # Timestamp mode (1 byte)
    timestamp_mode = data[pos]
    pos += 1

    # Optional timestamp (4 bytes if mode > 0)
    timestamp = None
    if timestamp_mode > 0:
        timestamp = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4

    # ':' separator
    pos += 1

    # Signal data ends before StatusByte(1) + CRC32(4)
    signal_data_end = len(data) - 5
    status_byte = data[signal_data_end]

    signals = _unpack_signals(data, pos, signal_data_end, symbol_table)

    return DecodedData(
        msg_id=msg_id,
        restart_flag=restart_flag,
        timestamp_mode=timestamp_mode,
        timestamp=timestamp,
        status_byte=status_byte,
        signals=signals,
    )


def _parse_data_b1(
    msg_id: int, data: bytes, symbol_table: list[DecodedSymbol]
) -> DecodedData:
    """Parse B1 legacy format: signals... StatusByte CRC32

    B1 packs signal values sequentially in symbol-table order
    (no SymbolID prefix per value, unlike D1).
    """
    signal_data_end = len(data) - 5
    status_byte = data[signal_data_end] if len(data) >= 5 else 0
    signals: dict[int, float | int | bool] = {}
    pos = 0
    for i, symbol in enumerate(symbol_table):
        size = symbol.datatype_size
        info = _DTYPE_INFO.get(symbol.datatype_code)
        if info and pos + size <= signal_data_end:
            _, _, fmt = info
            value = struct.unpack(fmt, data[pos : pos + size])[0]
            signals[i] = value
        pos += size

    return DecodedData(
        msg_id=msg_id,
        restart_flag=False,
        timestamp_mode=0,
        timestamp=None,
        status_byte=status_byte,
        signals=signals,
    )


def _unpack_signals(
    data: bytes, pos: int, end: int, symbol_table: list[DecodedSymbol]
) -> dict[int, float | int | bool]:
    """Unpack SymbolID + DATA pairs from signal payload."""
    signals = {}
    while pos + 2 <= end:
        symbol_id = int.from_bytes(data[pos : pos + 2], "little")
        pos += 2

        if symbol_id >= len(symbol_table):
            break  # unknown signal — can't determine data size

        symbol = symbol_table[symbol_id]
        size = symbol.datatype_size
        info = _DTYPE_INFO.get(symbol.datatype_code)

        if info and pos + size <= end:
            _, _, fmt = info
            value = struct.unpack(fmt, data[pos : pos + size])[0]
            signals[symbol_id] = value
        pos += size

    return signals


def parse_all_devices(content: bytes) -> list[DecodedDeviceInfo]:
    """Parse all device entries from a B2/B3/B5/B6 devices frame.

    Each entry contains MSC + SlaveID + device metadata fields.
    A master/slave upstream may send multiple entries in one frame.

    Args:
        content: bytes between <BLAECK: and /BLAECK>

    Returns:
        List of DecodedDeviceInfo, one per device entry.
    """
    msg_key, msg_id, data = _parse_header(content)

    devices: list[DecodedDeviceInfo] = []
    pos = 0

    def read_string() -> str:
        nonlocal pos
        null_pos = data.find(b"\x00", pos)
        if null_pos == -1:
            s = data[pos:].decode("utf-8", errors="replace")
            pos = len(data)
            return s
        s = data[pos:null_pos].decode("utf-8", errors="replace")
        pos = null_pos + 1
        return s

    while pos + 2 <= len(data):
        msc = data[pos]
        sid = data[pos + 1]
        pos += 2

        if pos >= len(data):
            break

        info = DecodedDeviceInfo(
            msg_id=msg_id,
            device_name=read_string(),
            hw_version=read_string(),
            fw_version=read_string(),
            lib_version=read_string(),
            msc=msc,
            slave_id=sid,
        )

        match msg_key:
            case 0xB2:  # MSGKEY_DEVICES_LEGACY
                pass
            case 0xB3:  # MSGKEY_DEVICES_V1
                info.lib_name = read_string()
            case 0xB4:  # MSGKEY_DEVICES_V2
                info.lib_name = read_string()
                info.assigned_client_id = read_string()
                info.data_enabled = read_string()
            case 0xB5:  # MSGKEY_DEVICES_V4
                info.lib_name = read_string()
                info.assigned_client_id = read_string()
                info.data_enabled = read_string()
                info.server_restarted = read_string()
            case 0xB6:  # MSGKEY_DEVICES
                info.lib_name = read_string()
                info.assigned_client_id = read_string()
                info.data_enabled = read_string()
                info.server_restarted = read_string()
                info.device_type = read_string()
                info.parent = read_string()

        devices.append(info)

    return devices


def parse_devices(content: bytes) -> DecodedDeviceInfo:
    """Parse first device entry from a devices frame.

    For frames with multiple entries (master/slave), only the first is
    returned.  Use :func:`parse_all_devices` to get all entries.

    Args:
        content: bytes between <BLAECK: and /BLAECK>

    Returns:
        DecodedDeviceInfo with device metadata.
    """
    devices = parse_all_devices(content)
    if not devices:
        raise ValueError("No device entries found in frame")
    return devices[0]
