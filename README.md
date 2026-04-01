# blaecktcpy

A Python TCP server for real-time streaming of named, typed signals in binary format. Use it to turn any Python script into a signal source.

## Getting Started

Install the library from PyPI:

```bash
pip install blaecktcpy
```

### Create a BlaeckTCPy instance

```python
from blaecktcpy import BlaeckTCPy, Signal

bltcp = BlaeckTCPy(
    ip='127.0.0.1',
    port=23,
    device_name='My Device',
    device_hw_version='1.0',
    device_fw_version='1.0',
)
```

### Add signals

```python
bltcp.add_signal('Sine_1', 'float', 0.0)
bltcp.add_signal(Signal('Temperature', 'double', 0.0))
```

Signals are stored in a `SignalList` and can be accessed by index or name:

```python
bltcp.signals[0].value = 1.0
bltcp.signals["Temperature"].value = 22.5
```

### Start the device

Call `start()` after setup (adding signals, configuring interval, adding upstreams) and before using `tick()`, `read()`, or `write()`:

```python
bltcp.start()
```

### Update your variables and don't forget to `tick()`!

```python
import math, time

start = time.time()
while True:
    bltcp.signals[0].value = math.sin((time.time() - start) * 0.1)
    bltcp.tick()
```

### Server-controlled interval

By default the client controls the timed data rate via `ACTIVATE`/`DEACTIVATE`.
Use the `interval_ms` property to lock the device to a fixed rate:

```python
from blaecktcpy import IntervalMode

bltcp.interval_ms = 500                  # send every 500 ms, ignore client ACTIVATE/DEACTIVATE
bltcp.interval_ms = IntervalMode.CLIENT  # return to client control (default)
bltcp.interval_ms = IntervalMode.OFF     # disable timed data entirely
```

## Built-in commands

Here's a full list of the commands handled by this library:

| Command | Description |
|---|---|
| `<BLAECK.GET_DEVICES,B1,B2,B3,B4>` | Writes the device information including the device name, hardware version, firmware version and library version |
| `<BLAECK.WRITE_SYMBOLS,B1,B2,B3,B4>` | Writes symbol list including datatype information |
| `<BLAECK.WRITE_DATA,B1,B2,B3,B4>` | Writes the binary data |
| `<BLAECK.ACTIVATE,B1,B2,B3,B4>` | Activates writing the binary data in user-set interval \[ms\] |
| `<BLAECK.DEACTIVATE>` | Deactivates writing in intervals |

`B1,B2,B3,B4` are four bytes encoding a little-endian integer. For `ACTIVATE` this is the interval in milliseconds. For the other commands it is the message ID echoed back in the response.

## Custom commands

Commands are sent as `<COMMAND>` or `<COMMAND,param1,param2,...>`. Register handlers with the `@bltcp.on_command()` decorator — parameters are passed as strings:

```python
@bltcp.on_command("SET_LED")
def handle_led(state):          # <SET_LED,1>  →  state = "1"
    print(f"LED = {state}")

@bltcp.on_command("MOTOR")
def handle_motor(speed, dir):   # <MOTOR,255,forward>  →  speed = "255", dir = "forward"
    print(f"{speed} {dir}")
```

A catch-all handler (no command name) fires for every message, including built-in commands:

```python
@bltcp.on_command()
def log_all(command, *params):  # receives command name + all params
    print(f"{command} {params}")
```

### Forwarding custom commands upstream

In hub mode, custom commands from downstream clients can be forwarded to upstream devices. This requires two things:

1. **Opt-in per upstream** — set `forward_custom_commands=True` when adding the upstream
2. **Mark commands for forwarding** — use `forward=True` on `@on_command()`, or `forward_command()` for forward-only (no local handler)

```python
hub.add_tcp("192.168.1.10", 24, name="Arduino", forward_custom_commands=True)
hub.add_tcp("192.168.1.11", 25, name="Sensor")  # won't receive forwarded commands

# Forward + handle locally
@hub.on_command("SET_LED", forward=True)
def handle_led(state):
    print(f"LED = {state}")

# Forward only (no local handler)
hub.forward_command("RESET")

# Local only (default, not forwarded)
@hub.on_command("MOTOR")
def handle_motor(speed):
    print(f"Motor = {speed}")
```

## Timestamps

Data frames can include timestamps. Set the `timestamp_mode` property to enable:

```python
from blaecktcpy import TimestampMode

# Microseconds since start() (relative)
bltcp.timestamp_mode = TimestampMode.MICROS

# Microseconds since Unix epoch (absolute, real-time clock)
bltcp.timestamp_mode = TimestampMode.RTC
```

Every write method auto-fills the timestamp based on the mode. Use the `timestamp_us` parameter to override with an explicit value (e.g. from a CSV file):

```python
bltcp.write_all_data(timestamp_us=int(csv_timestamp * 1_000_000))
```

The `start_time` property exposes the `time.time()` value captured at `start()`:

```python
elapsed = time.time() - bltcp.start_time
```

## Client callbacks

Every connected client is automatically added to `data_clients` and receives data frames. Use `on_client_connected` / `on_client_disconnected` to react to connections or exclude specific clients from data:

```python
@bltcp.on_client_connected()
def on_connect(client_id):
    if client_id > 0:
        bltcp.data_clients.discard(client_id)  # only client #0 receives data

@bltcp.on_client_disconnected()
def on_disconnect(client_id):
    print(f"Client #{client_id} left")
```

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

## Protocol

Messages use the following binary format:

```
|Header|--       Message        --||-- EOT  --|
<BLAECK:<MSGKEY>:<MSGID>:<ELEMENTS>/BLAECK>\r\n
```

### Message Keys

| Type | MSGKEY | Elements | Description |
|------|--------|----------|-------------|
| Symbol List | `B0` | **`<MSC><SlaveID><SymbolName><DTYPE>`** | **Up to n symbols.** Response to `<BLAECK.WRITE_SYMBOLS>` |
| Data | `D1` | `<RestartFlag>:<TimestampMode><Timestamp>:`**`<SymbolID><DATA>`**`<StatusByte><CRC32>` | **Up to n data items.** Response to `<BLAECK.WRITE_DATA>` |
| Data | `B1` | **`<SymbolID><DATA>`**`<StatusByte><CRC32>` | Deprecated; decoded from upstream only |
| Devices | `B6` | `<MSC><SlaveID><DeviceName><DeviceHWVersion><DeviceFWVersion><LibraryVersion><LibraryName><Client#><ClientDataEnabled><ServerRestarted><DeviceType><Parent>` | **Up to n devices.** Response to `<BLAECK.GET_DEVICES>` |

### Elements

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
| `TimestampMode` | byte | Always `0` in blaecktcpy (timestamps not implemented) |
| `Timestamp` | ulong | Timestamp value (only present if TimestampMode > 0); not sent by blaecktcpy |
| `StatusByte` | byte | `0x00` = normal, `0x01` = I2C CRC error, `0x02` = upstream connection lost |
| `CRC32` | bytes | 4 bytes, polynomial `0x04C11DB7`, init `0xFFFFFFFF`, final XOR `0xFFFFFFFF`, reverse in/out |

## Hub mode

The same `BlaeckTCPy` class serves as a hub when you add upstream connections with `add_tcp()` or `add_serial()`. The hub aggregates signals from multiple upstream devices and serves them as a single merged device, alongside any local signals.

```python
from blaecktcpy import BlaeckTCPy

hub = BlaeckTCPy("0.0.0.0", 23, "My Hub", "Python", "1.0")

# Connect to upstream devices
hub.add_tcp("192.168.1.10", 24, name="ESP32")
hub.add_tcp("127.0.0.1", 25, name="Sine")

# Add a local signal
dew_point = hub.add_signal("DewPoint", "float")

hub.start()

while True:
    dew_point.value = compute_dew_point()
    hub.tick()
```

Serial upstreams are also supported (`pip install blaecktcpy[serial]`):

```python
hub.add_serial("COM3", 115200, name="Arduino")
```

### Upstream data rate

The hub sends `BLAECK.DEACTIVATE` to every upstream on connect (to ensure a clean state) and again on `close()`.

By default (`interval_ms=IntervalMode.CLIENT`), each upstream starts streaming when a downstream client (e.g. Loggbok) sends `BLAECK.ACTIVATE`. The hub forwards `ACTIVATE` and `DEACTIVATE` commands from the client to these upstreams.

Set `interval_ms` to a value ≥ 0 to start streaming at a fixed rate immediately on `start()`. Client `ACTIVATE`/`DEACTIVATE` commands are **not** forwarded to hub-managed upstreams:

```python
from blaecktcpy import IntervalMode

# Hub-managed: always stream at 500 ms, ignore client ACTIVATE/DEACTIVATE
hub.add_tcp("192.168.1.10", 24, name="ESP32", interval_ms=500)

# Client-managed (default): hub forwards ACTIVATE/DEACTIVATE from Loggbok
hub.add_tcp("127.0.0.1", 25, name="Sine")

# Disabled: no timed data from this upstream
hub.add_tcp("192.168.1.20", 24, name="Sensor", interval_ms=IntervalMode.OFF)
```

### Local signal interval

Use the `interval_ms` property to stream local signals at a fixed rate:

```python
hub.interval_ms = 500                  # local signals every 500 ms
hub.interval_ms = IntervalMode.CLIENT  # follow client ACTIVATE/DEACTIVATE (default)
hub.interval_ms = IntervalMode.OFF     # disable timed local data
```

### Relaying upstream signals

By default (`relay_downstream=True`), all upstream signals are relayed to downstream clients.

Set `relay_downstream=False` to decode upstream signals hub-side without exposing them to downstream clients. This is useful when you want to read raw values, compute derived signals, and only expose those:

```python
# Hidden: raw signals decoded hub-side but not visible to Loggbok
arduino = hub.add_tcp("192.168.1.10", 24, name="Arduino", relay_downstream=False)

# Expose a computed signal instead
dew_point = hub.add_signal("DewPoint", "float")
```

The hub can decode upstream frames using older protocol versions (`B2`–`B5` for devices, `B1` for legacy data) but always sends `B6`/`D1` downstream to clients.

### Signal list caching

Upstream signal lists are cached when the hub discovers each upstream at startup. Loggbok discovers the signal list once at logging start.

```
Server              Hub                  Loggbok
┌────────────┐     ┌────────────────┐   ┌─────────────┐
│ locals:    │     │ upstream:      │   │ (logging)   │
│   frozen   │────>│   frozen       │──>│             │
│            │     │ locals:        │   │  discovers   │
│            │     │   mutable      │   │  signals     │
└────────────┘     └────────────────┘   └─────────────┘
```

If an upstream device needs to change its signals, stop logging in Loggbok first, then restart the Hub to re-discover the new layout. The locals on the Hub can always be changed (e.g., by sending a custom command with putty/terminal program). But when you start Logging all the signals are discovered and no more changes are allowed.

## Examples

See the [examples](examples/) folder:

### Server

| Example | Description |
|---|---|
| `sine.py` | Sine wave generator |
| `datatype_test.py` | Tests all supported datatypes including edge cases |
| `command_parser.py` | Custom command handling with `@bltcp.on_command()` |
| `csv_reader.py` | Stream CSV file data as signals |
| `csv_generator.py` | Generate test CSV data for `csv_reader.py` |

### Hub

| Example | Description |
|---|---|
| `basic.py` | Aggregates two upstream servers and a local signal |
| `signal_processing.py` | Transform and compute signals via `on_data_received` |
| `mixed_sources.py` | BlaeckTCP microcontroller + SCPI power supply |

## License

MIT
