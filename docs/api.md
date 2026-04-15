# API Reference — blaecktcpy

## BlaeckTCPy

```python
from blaecktcpy import BlaeckTCPy
```

Unified BlaeckTCP protocol implementation. Works as a standalone server or as a hub that aggregates signals from multiple upstream devices.

### Constructor

```python
BlaeckTCPy(
    *,
    ip: str,
    port: int,
    device_name: str,
    device_hw_version: str | None = None,
    device_fw_version: str | None = None,
    log_level: int | None = logging.INFO,
    http_port: int | None = 8080,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ip` | `str` | — | IP address to bind to (e.g. `'127.0.0.1'` for localhost, `'0.0.0.0'` for all interfaces) |
| `port` | `int` | — | TCP port to listen on |
| `device_name` | `str` | — | Name of the device |
| `device_hw_version` | `str \| None` | `None` | Hardware version string. Defaults to current platform (e.g. `"Windows AMD64"`). |
| `device_fw_version` | `str \| None` | `None` | Firmware version string. Defaults to `"1.0"`. |
| `log_level` | `int \| None` | `logging.INFO` | Logging level (e.g. `logging.DEBUG`, `logging.WARNING`). Pass `None` to silence all output. |
| `http_port` | `int \| None` | `8080` | Port for the HTTP status page. Pass `None` to disable. If the port is occupied, a free port is chosen automatically. |

### Lifecycle

#### `start()`

```python
start() -> None
```

Create socket, bind, listen, register upstream signals, and activate. Must be called after all `add_tcp()` and `add_serial()` calls. Local signals (`add_signal()`) can be added before or after `start()`.

#### `close()`

```python
close() -> None
```

Gracefully close all upstream and downstream connections, stop the HTTP status page, and release resources.

#### Context Manager

`BlaeckTCPy` supports the `with` statement for automatic cleanup — `close()` is called on exit.

### Signal Management

#### `add_signal()`

```python
add_signal(
    signal_or_name: Signal | str,
    datatype: str = "",
    value: int | float = 0,
) -> Signal
```

Add a local signal. Accepts a `Signal` object or individual arguments. Can be called before or after `start()`. Returns the added `Signal`.

#### `add_signals()`

```python
add_signals(signals: Iterable[Signal]) -> None
```

Add multiple local signals at once.

#### `delete_signals()`

```python
delete_signals() -> None
```

Remove all local signals. After `start()`, upstream signals are preserved and their indices are rebuilt.

#### `signals`

```python
signals: SignalList
```

The `SignalList` containing all signals (local and upstream). Supports integer and name-based indexing.

### Value Access

#### `write()`

```python
write(
    key: str | int,
    value: int | float,
    *,
    msg_id: int = 1,
    unix_timestamp: float | int | None = None,
) -> None
```

Update a single local signal's value and immediately send it to connected data clients.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `key` | `str \| int` | — | Signal name or index |
| `value` | `int \| float` | — | New value to set |
| `msg_id` | `int` | `1` | Message ID for the protocol frame |
| `unix_timestamp` | `float \| int \| None` | `None` | Override timestamp. `float` = seconds since epoch, `int` = microseconds since epoch. |

#### `update()`

```python
update(key: str | int, value: int | float) -> None
```

Update a local signal's value and mark it as updated without sending. Use with `tick_updated()` or `write_updated_data()`.

#### `mark_signal_updated()`

```python
mark_signal_updated(key: str | int) -> None
```

Mark a local signal as updated without changing its value.

#### `mark_all_signals_updated()`

```python
mark_all_signals_updated() -> None
```

Mark all local signals as updated.

#### `clear_all_update_flags()`

```python
clear_all_update_flags() -> None
```

Clear the `updated` flag on all local signals.

#### `has_updated_signals`

```python
@property
has_updated_signals: bool
```

`True` if any local signal is marked as updated.

### Main Loop

#### `tick()`

```python
tick(msg_id: int | None = None) -> bool
```

Main loop tick — reads commands, polls upstreams, and sends all local data on timer. Returns `True` if timed local data was sent.

#### `tick_updated()`

```python
tick_updated(msg_id: int | None = None) -> bool
```

Like `tick()` but only transmits local signals marked as updated. Returns `True` if timed local data was sent.

#### `read()`

```python
read() -> None
```

Read and process all pending messages from downstream clients. Called automatically by `tick()` / `tick_updated()`.

### Manual Write

#### `write_all_data()`

```python
write_all_data(msg_id: int = 1, *, unix_timestamp: float | int | None = None) -> None
```

Send all local signal data to data-enabled clients.

#### `write_updated_data()`

```python
write_updated_data(msg_id: int = 1, *, unix_timestamp: float | int | None = None) -> None
```

Send only updated local signals to data-enabled clients.

#### `timed_write_all_data()`

```python
timed_write_all_data(msg_id: int | None = None, *, unix_timestamp: float | int | None = None) -> bool
```

Send all local data if the timer interval has elapsed. Returns `True` if data was sent.

#### `timed_write_updated_data()`

```python
timed_write_updated_data(msg_id: int | None = None, *, unix_timestamp: float | int | None = None) -> bool
```

Send only updated local signals if the timer interval has elapsed. Returns `True` if data was sent.

#### `write_symbols()`

```python
write_symbols(msg_id: int = 1) -> None
```

Send the symbol list to all connected clients.

#### `write_devices()`

```python
write_devices(msg_id: int = 1) -> None
```

Send device information to each connected client.

### Properties

#### `connected`

```python
@property
connected: bool
```

`True` if any downstream client is connected.

#### `commanding_client`

```python
@property
commanding_client: socket.socket | None
```

The client socket that sent the most recent command, or `None`.

#### `local_interval_ms`

```python
@property
local_interval_ms: int
```

Local signal timed data interval. Controls the output rate of local signals only. In hub mode, upstream signals are relayed independently at their own rate.

| Value | Meaning |
|---|---|
| `>= 0` | Lock at the given rate (ms). Client `ACTIVATE` / `DEACTIVATE` are ignored. `0` means "as fast as possible." |
| `IntervalMode.OFF` | Timed data disabled. Client `ACTIVATE` is ignored. |
| `IntervalMode.CLIENT` | Client controlled (default). |

#### `start_time`

```python
@property
start_time: float
```

Wall-clock time when `start()` was called (`time.time()`).

#### `timestamp_mode`

```python
@property
timestamp_mode: TimestampMode
```

Timestamp mode for outgoing data frames. Settable. Valid values: `TimestampMode.NONE` (default) and `TimestampMode.UNIX`.

#### `data_clients`

```python
data_clients: set[int]
```

Set of client IDs that receive data frames. All connected clients are added by default. Remove a client ID to exclude it from data broadcasts.

### Hub

#### `add_tcp()`

```python
add_tcp(
    ip: str,
    port: int,
    name: str = "",
    timeout: float = 5.0,
    interval_ms: int = IntervalMode.CLIENT,
    relay_downstream: bool = True,
    forward_custom_commands: bool | list[str] = True,
    auto_reconnect: bool = False,
) -> UpstreamDevice
```

Register an upstream TCP device. Must be called before `start()`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ip` | `str` | — | IP address of the upstream device |
| `port` | `int` | — | TCP port of the upstream device |
| `name` | `str` | `""` | Friendly name; defaults to the upstream device name |
| `timeout` | `float` | `5.0` | Connection and discovery timeout in seconds |
| `interval_ms` | `int` | `IntervalMode.CLIENT` | Interval in milliseconds, or an `IntervalMode` member |
| `relay_downstream` | `bool` | `True` | If `False`, signals are decoded but not exposed downstream |
| `forward_custom_commands` | `bool \| list[str]` | `True` | `True` = all, `False` = none, or a list of command names |
| `auto_reconnect` | `bool` | `False` | Automatically reconnect on connection loss |

#### `add_serial()`

```python
add_serial(
    port: str,
    baudrate: int = 115200,
    name: str = "",
    timeout: float = 5.0,
    dtr: bool = True,
    interval_ms: int = IntervalMode.CLIENT,
    relay_downstream: bool = True,
    forward_custom_commands: bool | list[str] = True,
) -> UpstreamDevice
```

Register an upstream serial device. Must be called before `start()`. Requires pyserial: `pip install blaecktcpy[serial]`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `port` | `str` | — | Serial port (e.g. `'COM3'`, `'/dev/ttyUSB0'`) |
| `baudrate` | `int` | `115200` | Serial baud rate |
| `name` | `str` | `""` | Friendly name; defaults to the upstream device name |
| `timeout` | `float` | `5.0` | Connection and discovery timeout in seconds |
| `dtr` | `bool` | `True` | Enable DTR (set `False` for Arduino Mega to prevent reset) |
| `interval_ms` | `int` | `IntervalMode.CLIENT` | Interval in milliseconds, or an `IntervalMode` member |
| `relay_downstream` | `bool` | `True` | If `False`, signals are decoded but not exposed downstream |
| `forward_custom_commands` | `bool \| list[str]` | `True` | `True` = all, `False` = none, or a list of command names |

#### `upstream_status()`

```python
upstream_status(name: str | None = None) -> dict
```

Get connection status for upstream devices. If `name` is provided, returns status for that upstream. If `None`, returns `{name: status_dict, ...}` for all upstreams.

### Decorators

#### `on_command()`

```python
on_command(command: str | None = None, *, forward: bool = True)
```

Register a handler for a specific command or a catch-all. With a command name, the handler receives parameters as positional strings. Without (catch-all), receives command name as first argument followed by parameters. Set `forward=False` for local-only handling.

#### `on_client_connected()`

```python
on_client_connected()
```

Register a callback when a new client connects. Receives the client ID.

#### `on_client_disconnected()`

```python
on_client_disconnected()
```

Register a callback when a client disconnects. Receives the client ID.

#### `on_before_write()`

```python
on_before_write()
```

Register a callback that fires before data is written. Use this to update signal values right before transmission.

#### `on_data_received()`

```python
on_data_received(upstream_name: str | None = None)
```

Register a callback when upstream data arrives. If `upstream_name` is provided, only fires for that upstream. The callback receives the upstream device handle.

#### `on_upstream_disconnected()`

```python
on_upstream_disconnected()
```

Register a callback when an upstream device disconnects. Receives the upstream device name.

---

## Signal

```python
from blaecktcpy import Signal
```

A dataclass representing a BlaeckTCP signal with typed data.

### Constructor

```python
Signal(
    signal_name: str,
    datatype: str,
    value: int | float = 0,
    updated: bool = False,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `signal_name` | `str` | — | Name of the signal |
| `datatype` | `str` | — | One of: `'bool'`, `'byte'`, `'short'`, `'unsigned short'`, `'int'`, `'unsigned int'`, `'long'`, `'unsigned long'`, `'float'`, `'double'` |
| `value` | `int \| float` | `0` | Initial value (validated against the datatype range) |
| `updated` | `bool` | `False` | Whether the signal has been updated |

### Properties

#### `value`

```python
@property
value: int | float | bool
```

The signal's current value. The setter validates against the signal's datatype and range. Raises `ValueError` for invalid values.

### Methods

#### `to_bytes()`

```python
to_bytes() -> bytes
```

Convert the signal value to bytes using little-endian encoding.

#### `get_dtype_byte()`

```python
get_dtype_byte() -> bytes
```

Get the datatype code as a single byte.

### Class Attributes

| Attribute | Type | Description |
|---|---|---|
| `DATATYPE_TO_CODE` | `dict[str, int]` | Maps datatype names to protocol code values |
| `DATATYPE_SIZES` | `dict[str, int]` | Maps datatype names to byte sizes |
| `SIGNED_TYPES` | `set[str]` | Signed integer types: `{"short", "int", "long"}` |
| `FLOAT_TYPES` | `set[str]` | Floating-point types: `{"float", "double"}` |

---

## SignalList

```python
from blaecktcpy import SignalList
```

A `list[Signal]` with name-based access. Name lookups use an internal dict cache (O(1) amortised), lazily rebuilt after any mutation.

### Methods

#### `index_of()`

```python
index_of(name: str) -> int | None
```

Return the index of a signal by name, or `None` if not found. O(1).

All standard `list` mutating methods (`append`, `extend`, `insert`, `remove`, `pop`, `clear`) are supported and automatically invalidate the name cache.

---

## IntervalMode

```python
from blaecktcpy import IntervalMode
```

An `IntEnum` for timed data interval modes.

| Member | Value | Description |
|---|---|---|
| `OFF` | `-1` | Timed data disabled; client `ACTIVATE` ignored |
| `CLIENT` | `-2` | Client controlled (default) |

---

## TimestampMode

```python
from blaecktcpy import TimestampMode
```

An `IntEnum` for data frame timestamp modes.

| Member | Value | Description |
|---|---|---|
| `NONE` | `0` | No timestamp (default) |
| `UNIX` | `2` | Microseconds since Unix epoch |

---

## Constants

```python
from blaecktcpy import LIB_VERSION, LIB_NAME, STATUS_OK, STATUS_UPSTREAM_LOST, STATUS_UPSTREAM_RECONNECTED
```

| Constant | Type | Value | Description |
|---|---|---|---|
| `LIB_VERSION` | `str` | *(dynamic)* | Package version from metadata |
| `LIB_NAME` | `str` | `"blaecktcpy"` | Library name |
| `STATUS_OK` | `int` | `0x00` | Normal status byte |
| `STATUS_UPSTREAM_LOST` | `int` | `0x80` | Upstream connection lost |
| `STATUS_UPSTREAM_RECONNECTED` | `int` | `0x81` | Upstream connection restored |
