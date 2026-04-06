# Protocol

## Supported datatypes

| Datatype         | DTYPE | Bytes |
|------------------|-------|-------|
| `bool`           | 0     | 1     |
| `byte`           | 1     | 1     |
| `short`          | 2     | 2     |
| `unsigned short` | 3     | 2     |
| `int`            | 6     | 4     |
| `unsigned int`   | 7     | 4     |
| `long`           | 6     | 4     |
| `unsigned long`  | 7     | 4     |
| `float`          | 8     | 4     |
| `double`         | 9     | 8     |

## Binary frame format

Messages use the following binary format:

```
|Header|--       Message        --||-- EOT  --|
<BLAECK:<MSGKEY>:<MSGID>:<ELEMENTS>/BLAECK>\r\n
```

## Message Keys

| Type | MSGKEY | Elements | Description |
|------|--------|----------|-------------|
| Symbol List | `B0` | **`<MSC><SlaveID><SymbolName><DTYPE>`** | **Up to n symbols.** Response to `<BLAECK.WRITE_SYMBOLS>` |
| Data | `D2` | `<RestartFlag>:<SchemaHash>:<TimestampMode><Timestamp>:`**`<SymbolID><DATA>`**`<StatusByte><StatusPayload><CRC32>` | **Up to n data items.** Response to `<BLAECK.WRITE_DATA>` |
| Devices | `B6` | `<MSC><SlaveID><DeviceName><DeviceHWVersion><DeviceFWVersion><LibraryVersion><LibraryName><Client#><ClientDataEnabled><ServerRestarted><DeviceType><Parent>` | **Up to n devices.** Response to `<BLAECK.GET_DEVICES>` |
| Restart | `C0` | `<MSC><SlaveID><DeviceName><DeviceHWVersion><DeviceFWVersion><LibraryVersion><LibraryName>` | Upstream device restart notification (hub mode only) |

## Elements

| Element | Type | Description |
|---------|------|-------------|
| `MSGKEY` | byte | Message key identifying the type of message |
| `MSGID` | ulong | Message ID echoed back to identify the response (4 bytes, little-endian) |
| `MSC` | byte | MasterSlaveConfig: `0x01` = master, `0x02` = slave |
| `SlaveID` | byte | Slave address: `0` for master, `1`–`n` for slaves |
| `SymbolName` | String0 | Signal name, null-terminated |
| `DTYPE` | byte | Datatype code (see [Supported datatypes](#supported-datatypes)) |
| `SymbolID` | uint | Signal index (2 bytes, little-endian) |
| `DATA` | (varying) | Signal value, size depends on datatype |
| `DeviceName` | String0 | Device name, null-terminated |
| `DeviceHWVersion` | String0 | Hardware version |
| `DeviceFWVersion` | String0 | Firmware version |
| `LibraryVersion` | String0 | Library version |
| `LibraryName` | String0 | Library name (`blaecktcpy`) |
| `Client#` | String0 | Client number of the connected client |
| `ClientDataEnabled` | String0 | `0` or `1`: client is allowed to receive data |
| `ServerRestarted` | String0 | `0` or `1`: first response after a restart is `1` |
| `DeviceType` | String0 | `server` or `hub` |
| `Parent` | String0 | SlaveID of the parent device (`0` = master) |
| `RestartFlag` | byte | `1` on the first data frame after startup, `0` otherwise |
| `SchemaHash` | uint16 | CRC16-CCITT of (name bytes + datatype code byte) for each signal in order (2 bytes, little-endian). Used to detect signal layout changes at runtime. |
| `TimestampMode` | byte | `0` = NONE (default), `1` = MICROS (µs since start; upstream/Arduino devices only), `2` = UNIX (µs since epoch) |
| `Timestamp` | uint64 | 8-byte microsecond timestamp (only present if TimestampMode > 0) |
| `StatusByte` | byte | `0x00`–`0x7F` = device-level (e.g. `0x00` normal, `0x01` I2C CRC error); `0x80`–`0xFF` = hub-level (e.g. `0x80` upstream lost, `0x81` upstream reconnected) |
| `StatusPayload` (StatusByte `0x00`–`0x7F`) | bytes | 4-byte upstream-provided status payload relayed by hub |
| `StatusPayload` (StatusByte=0x80) | bytes | `[AutoReconnect, 0, 0, 0]` — byte 0: `0x01` if hub auto-reconnect is enabled, else `0x00` |
| `StatusPayload` (StatusByte=0x81) | bytes | 4 bytes, unused (`0x00000000`) |
| `CRC32` | uint32 | CRC32 of all content bytes before CRC, including `StatusByte` and `StatusPayload` |

> **StatusByte range convention:** `0x00`–`0x7F` is reserved for device-level
> status codes defined by device libraries and servers (BlaeckTCP, BlaeckSerial,
> blaecktcpy in server mode). `0x80`–`0xFF` is reserved for hub-level status
> codes defined by blaecktcpy in hub mode. This split allows both sides to add
> new codes without collisions.

## Schema hash

Every D2 data frame includes a `SchemaHash` — a CRC16-CCITT hash computed from the signal names and datatype codes. Clients such as Loggbok use this to detect when the signal layout changes during a session, allowing them to stop logging and notify the user.
