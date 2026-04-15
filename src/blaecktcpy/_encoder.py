"""BlaeckTCP protocol message encoding.

Stateless functions that build binary protocol frames from signal data.
Used internally by :class:`~blaecktcpy.BlaeckTCPy`.
"""

import binascii

from ._signal import SignalList

# Message type keys (pre-computed bytes for wire encoding)
MSG_SYMBOL_LIST = b"\xb0"
MSG_DATA = b"\xd2"
MSG_DEVICES = b"\xb6"

# Status byte values for data frames
STATUS_OK = 0x00
STATUS_UPSTREAM_LOST = 0x80
STATUS_UPSTREAM_RECONNECTED = 0x81

# MasterSlaveConfig byte values
MSC_MASTER = b"\x01"
MSC_SLAVE = b"\x02"


def build_header(msg_key: bytes, msg_id: int) -> bytes:
    """Build the common message header: ``MSGKEY : MSGID(4) :``."""
    return msg_key + b":" + msg_id.to_bytes(4, "little") + b":"


def wrap_frame(content: bytes) -> bytes:
    """Wrap encoded content in BlaeckTCP frame markers."""
    return b"<BLAECK:" + content + b"/BLAECK>\r\n"


def build_data_frame(
    header: bytes,
    signals: SignalList,
    start: int = 0,
    end: int = -1,
    *,
    schema_hash: int,
    restart_flag: bool,
    timestamp_mode: int = 0,
    timestamp: int | None = None,
    only_updated: bool = False,
    status: int = STATUS_OK,
    status_payload: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    """Build a D2 data frame with CRC32 checksum.

    Args:
        header: Pre-built header bytes from :func:`build_header`.
        signals: Signal list to encode values from.
        start: First signal index (inclusive).
        end: Last signal index (inclusive), ``-1`` means last signal.
        schema_hash: CRC16 schema hash for this signal set.
        restart_flag: Whether the server-restart flag should be set.
        timestamp_mode: Timestamp mode byte (0 = none).
        timestamp: Timestamp in microseconds (uint64), or ``None``.
        only_updated: If ``True``, include only signals with
            ``updated=True`` and clear their flag after encoding.
        status: Status byte (STATUS_OK, STATUS_UPSTREAM_LOST, etc.).
        status_payload: 4-byte status payload.

    Returns:
        Complete frame content (header + meta + payload + status + CRC).
    """
    if end == -1:
        end = len(signals) - 1
    if len(status_payload) != 4:
        raise ValueError(
            f"status_payload must be 4 bytes, got {len(status_payload)}"
        )

    flag_byte = b"\x01" if restart_flag else b"\x00"
    hash_bytes = schema_hash.to_bytes(2, "little")

    if timestamp is not None and timestamp_mode != 0:
        mode_byte = int(timestamp_mode).to_bytes(1, "little")
        meta = (
            flag_byte + b":"
            + hash_bytes + b":"
            + mode_byte
            + timestamp.to_bytes(8, "little")
            + b":"
        )
    else:
        if timestamp is None and timestamp_mode != 0:
            raise ValueError("timestamp required when timestamp_mode != NONE")
        meta = flag_byte + b":" + hash_bytes + b":" + b"\x00" + b":"

    payload = b""
    for idx in range(start, end + 1):
        sig = signals[idx]
        if only_updated and not sig.updated:
            continue
        payload += idx.to_bytes(2, "little") + sig.to_bytes()
        if only_updated:
            sig.updated = False

    frame_no_crc = (
        header + meta + payload
        + status.to_bytes(1, "little") + status_payload
    )
    crc = binascii.crc32(frame_no_crc).to_bytes(4, "little")
    return frame_no_crc + crc


def build_symbol_payload(
    signals: SignalList,
    master_slave_config: bytes,
    slave_id: bytes,
) -> bytes:
    """Build the symbol-list payload for simple (non-hub) server mode.

    Each signal is encoded as: ``MSC + SlaveID + Name\\0 + DtypeByte``.
    """
    result = b""
    for sig in signals:
        result += (
            master_slave_config
            + slave_id
            + sig.signal_name.encode()
            + b"\0"
            + sig.get_dtype_byte()
        )
    return result


def encode_device_entry(
    msc: bytes,
    slave_id: bytes,
    name: bytes,
    hw: bytes,
    fw: bytes,
    lib_ver: bytes,
    lib_name: bytes,
    restarted: bytes,
    device_type: bytes,
    parent: bytes,
) -> bytes:
    """Encode a single B6 device entry (MSC through Parent)."""
    return (
        msc + slave_id
        + name + b"\0"
        + hw + b"\0"
        + fw + b"\0"
        + lib_ver + b"\0"
        + lib_name + b"\0"
        + restarted + b"\0"
        + device_type + b"\0"
        + parent + b"\0"
    )


def build_client_trailer(
    client_id: int,
    data_clients: set[int],
    client_meta: dict[int, dict[str, str]],
) -> bytes:
    """Build B6 client trailer: ClientNo, DataEnabled, ClientName, ClientType."""
    meta = client_meta.get(client_id, {})
    return (
        str(client_id).encode() + b"\0"
        + (b"1" if client_id in data_clients else b"0") + b"\0"
        + meta.get("name", "").encode() + b"\0"
        + meta.get("type", "unknown").encode() + b"\0"
    )
