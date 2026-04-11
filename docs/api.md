# API Reference — blaecktcpy

## BlaeckTCPy

```python
from blaecktcpy import BlaeckTCPy
```

Unified BlaeckTCP protocol implementation. Works as a standalone server or as a hub that aggregates signals from multiple upstream devices.

### Constructor

```python
BlaeckTCPy(
    ip: str,
    port: int,
    device_name: str,
    device_hw_version: str,
    device_fw_version: str,
    log_level: int | None = logging.INFO,
    http_port: int | None = 8080,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ip` | `str` | — | IP address to bind to (e.g. `'127.0.0.1'` for localhost, `'0.0.0.0'` for all interfaces) |
| `port` | `int` | — | TCP port to listen on |
| `device_name` | `str` | — | Name of the device |
| `device_hw_version` | `str` | — | Hardware version string |
| `device_fw_version` | `str` | — | Firmware version string |
| `log_level` | `int \| None` | `logging.INFO` | Logging level (e.g. `logging.DEBUG`, `logging.WARNING`). Pass `None` to silence all output. |
| `http_port` | `int \| None` | `8080` | Port for the HTTP status page. Pass `None` to disable. If the port is occupied, a free port is chosen automatically. |

### Lifecycle

#### `start()`

```python
start() -> None
```

Create socket, bind, listen, register upstream signals, and activate. Must be called after all `add_signal()`, `add_tcp()`, and `add_serial()` calls (though `add_signal()` also works after start).

#### `close()`

```python
close() -> None
```

Gracefully close all upstream and downstream connections, stop the HTTP status page, and release resources.

#### Context Manager

`BlaeckTCPy` supports the `with` statement for automatic cleanup:

```python
with BlaeckTCPy("127.0.0.1", 8081, "MyDevice", "1.0", "1.0") as bltcp:
    bltcp.start()
    # ... use bltcp ...
# close() is called automatically
```

### Signal Management

#### `add_signal()`

```python
add_signal(
    signal_or_name: Signal | str,
    datatype: str = "",
    value: int | float = 0,
) -> Signal
```

Add a local signal. Can be called with a `Signal` object or with individual arguments. Can be called before or after `start()`. Returns the added `Signal`.

```python
bltcp.add_signal(Signal("temp", "float", 0.0))
bltcp.add_signal("temp", "float", 0.0)  # shorthand
```

#### `add_signals()`

```python
add_signals(signals) -> None
```

Add multiple local signals at once. Accepts any iterable of `Signal` objects.

```python
bltcp.add_signals([
    Signal("temp", "float", 0.0),
    Signal("led",  "bool",  False),
])
```

#### `delete_signals()`

```python
delete_signals() -> None
```

Remove all local signals. After `start()`, upstream signals are preserved and their indices are rebuilt.

#### `signals`

```python
signals: SignalList
```

The `SignalList` containing all signals (local and upstream). Supports integer and name-based indexing:

```python
bltcp.signals[0].value
bltcp.signals["temperature"].value
```

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
| `unix_timestamp` | `float \| int \| None` | `None` | Override timestamp for UNIX mode. `float` = seconds since epoch, `int` = microseconds since epoch. |

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

Main loop tick — reads commands, polls upstreams, and sends all local data on timer. Call this repeatedly in your main loop. Returns `True` if timed local data was sent.

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

Local signal timed data interval mode. Controls the output rate of local signals only. In hub mode, upstream signals are relayed independently at their own rate.

| Value | Meaning |
|---|---|
| `>= 0` | Lock at the given rate (ms). Client `ACTIVATE` / `DEACTIVATE` commands are ignored. `0` means "as fast as possible." |
| `IntervalMode.OFF` | Timed data is off. Client `ACTIVATE` is ignored. |
| `IntervalMode.CLIENT` | Client controlled (default). The client's `ACTIVATE` / `DEACTIVATE` commands determine the rate. |

#### `start_time`

```python
@property
start_time: float
```

Wall-clock time when `start()` was called (`time.time()`). Useful as a reference point for elapsed-time calculations.

#### `timestamp_mode`

```python
@property
timestamp_mode: TimestampMode
```

Timestamp mode for outgoing data frames. Settable. Assigning `TimestampMode.MICROS` raises `ValueError` (not supported for blaecktcpy servers).

#### `data_clients`

```python
data_clients: set[int]
```

Set of client IDs that receive data frames. By default all connected clients are added. Remove a client ID to exclude it from data broadcasts.

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

Register an upstream TCP device. Must be called before `start()`. Connection and discovery happen in `start()`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ip` | `str` | — | IP address of the upstream device |
| `port` | `int` | — | TCP port of the upstream device |
| `name` | `str` | `""` | Optional friendly name; defaults to the upstream device name |
| `timeout` | `float` | `5.0` | Connection and discovery timeout in seconds |
| `interval_ms` | `int` | `IntervalMode.CLIENT` | Interval in milliseconds, or an `IntervalMode` member |
| `relay_downstream` | `bool` | `True` | If `False`, signals are decoded but not exposed to downstream clients |
| `forward_custom_commands` | `bool \| list[str]` | `True` | `True` forwards all custom commands, `False` forwards none, or a list of command names to forward selectively |
| `auto_reconnect` | `bool` | `False` | If `True`, automatically reconnect when the upstream TCP connection is lost |

Returns an upstream handle for accessing signal values.

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
| `name` | `str` | `""` | Optional friendly name; defaults to the upstream device name |
| `timeout` | `float` | `5.0` | Connection and discovery timeout in seconds |
| `dtr` | `bool` | `True` | Enable DTR (set `False` for Arduino Mega to prevent reset) |
| `interval_ms` | `int` | `IntervalMode.CLIENT` | Interval in milliseconds, or an `IntervalMode` member |
| `relay_downstream` | `bool` | `True` | If `False`, signals are decoded but not exposed to downstream clients |
| `forward_custom_commands` | `bool \| list[str]` | `True` | `True` forwards all custom commands, `False` forwards none, or a list of command names to forward selectively |

Returns an upstream handle for accessing signal values.

#### `upstream_status()`

```python
upstream_status(name: str | None = None) -> dict
```

Get connection status for upstream devices.

- If `name` is provided, returns a dict with `'connected'`, `'last_seen'`, and `'signals'` keys for that upstream.
- If `name` is `None`, returns `{name: status_dict, ...}` for all upstreams.

### Decorators

#### `on_command()`

Register a handler for a specific command or a catch-all for all messages.

```python
@bltcp.on_command("SET_LED")
def handle_led(state):
    print(f"LED = {state}")

@bltcp.on_command()
def log_all(command, *params):
    print(f"{command}: {params}")
```

**Signature:**

```python
on_command(command: str | None = None, *, forward: bool = True)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `command` | `str \| None` | `None` | Command name to handle. `None` registers a catch-all. |
| `forward` | `bool` | `True` | Whether the command is also forwarded to upstreams. Set to `False` for local-only handling. |

With a command name, the handler receives the command parameters as positional string arguments. Without a command name (catch-all), the handler receives the command name as the first argument followed by parameters.

#### `on_client_connected()`

Register a callback when a new client connects. Receives the client ID.

```python
@bltcp.on_client_connected()
def on_connect(client_id):
    if client_id > 0:
        bltcp.data_clients.discard(client_id)
```

#### `on_client_disconnected()`

Register a callback when a client disconnects. Receives the client ID.

```python
@bltcp.on_client_disconnected()
def on_disconnect(client_id):
    print(f"Client #{client_id} left")
```

#### `on_before_write()`

Register a callback that fires before data is written. Use this to update signal values right before they are transmitted.

```python
@bltcp.on_before_write()
def refresh_signals():
    bltcp.signals[0].value = read_sensor()
```

#### `on_data_received()`

Register a callback when upstream data arrives.

```python
@bltcp.on_data_received("Arduino")
def handle(upstream):
    temp = upstream.signals["temperature"].value
```

**Signature:**

```python
on_data_received(upstream_name: str | None = None)
```

If `upstream_name` is provided, the callback only fires for that upstream. If `None`, it fires for any upstream. The callback receives the upstream device handle.

#### `on_upstream_disconnected()`

Register a callback when an upstream device disconnects. Receives the upstream device name.

```python
@bltcp.on_upstream_disconnected()
def handle(name):
    print(f"Lost connection to {name}")
```

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

### Fields

| Field | Type | Description |
|---|---|---|
| `signal_name` | `str` | Name of the signal |
| `datatype` | `str` | Datatype string |
| `updated` | `bool` | Whether the signal has been updated since last send |

### Properties

#### `value`

```python
@property
value: int | float | bool
```

The signal's current value. The setter validates the value against the signal's datatype and range. Raises `ValueError` for out-of-range or type-incompatible values.

### Methods

#### `to_bytes()`

```python
to_bytes() -> bytes
```

Convert the signal value to bytes based on its datatype using little-endian encoding.

#### `get_dtype_byte()`

```python
get_dtype_byte() -> bytes
```

Get the datatype code as a single byte.

### Class Attributes

#### `DATATYPE_TO_CODE`

```python
DATATYPE_TO_CODE: dict[str, int]
```

Maps datatype names to their protocol code values:

| Datatype | Code |
|---|---|
| `'bool'` | `0` |
| `'byte'` | `1` |
| `'short'` | `2` |
| `'unsigned short'` | `3` |
| `'int'` | `6` |
| `'unsigned int'` | `7` |
| `'long'` | `6` |
| `'unsigned long'` | `7` |
| `'float'` | `8` |
| `'double'` | `9` |

#### `DATATYPE_SIZES`

```python
DATATYPE_SIZES: dict[str, int]
```

Maps datatype names to their byte sizes (e.g. `'bool'` → `1`, `'float'` → `4`, `'double'` → `8`).

#### `SIGNED_TYPES`

```python
SIGNED_TYPES: set[str]  # {"short", "int", "long"}
```

Set of signed integer datatype names.

#### `FLOAT_TYPES`

```python
FLOAT_TYPES: set[str]  # {"float", "double"}
```

Set of floating-point datatype names.

---

## SignalList

```python
from blaecktcpy import SignalList
```

A list of `Signal` objects with name-based access. Extends `list[Signal]`.

Supports indexing by integer or signal name:

```python
signals[0].value
signals["temperature"].value
```

Name-based lookups use an internal dict cache (O(1) amortised). The cache is lazily rebuilt after any list mutation.

### Methods

#### `index_of()`

```python
index_of(name: str) -> int | None
```

Return the index of a signal by name, or `None` if not found. O(1).

### Mutating Methods

All standard `list` mutating methods are supported and automatically invalidate the name cache:

- `append(item: Signal)`
- `extend(items: Iterable[Signal])`
- `insert(index, item: Signal)`
- `remove(item: Signal)`
- `pop(index=-1) -> Signal`
- `clear()`

---

## IntervalMode

```python
from blaecktcpy import IntervalMode
```

An `IntEnum` for timed data interval modes.

| Member | Value | Description |
|---|---|---|
| `IntervalMode.OFF` | `-1` | Timed data disabled; client `ACTIVATE` ignored |
| `IntervalMode.CLIENT` | `-2` | Client controlled (default); the client's `ACTIVATE` / `DEACTIVATE` commands determine the rate |

---

## TimestampMode

```python
from blaecktcpy import TimestampMode
```

An `IntEnum` for data frame timestamp modes.

| Member | Value | Description |
|---|---|---|
| `TimestampMode.NONE` | `0` | No timestamp in data frames (default) |
| `TimestampMode.MICROS` | `1` | Microseconds since start (protocol-level only; not available for blaecktcpy servers) |
| `TimestampMode.UNIX` | `2` | Microseconds since Unix epoch (1970-01-01 UTC) |

---

## Constants

```python
from blaecktcpy import LIB_VERSION, LIB_NAME, STATUS_OK, STATUS_UPSTREAM_LOST, STATUS_UPSTREAM_RECONNECTED
```

| Constant | Type | Value | Description |
|---|---|---|---|
| `LIB_VERSION` | `str` | *(dynamic)* | Package version from metadata |
| `LIB_NAME` | `str` | `"blaecktcpy"` | Library name |
| `STATUS_OK` | `int` | `0x00` | Normal status byte for data frames |
| `STATUS_UPSTREAM_LOST` | `int` | `0x80` | Status byte indicating an upstream connection was lost |
| `STATUS_UPSTREAM_RECONNECTED` | `int` | `0x81` | Status byte indicating an upstream connection was restored |
