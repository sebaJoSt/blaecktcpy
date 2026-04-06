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
Use the `local_interval_ms` property to lock the device to a fixed rate:

```python
from blaecktcpy import IntervalMode

bltcp.local_interval_ms = 500                  # send every 500 ms, ignore client ACTIVATE/DEACTIVATE
bltcp.local_interval_ms = IntervalMode.CLIENT  # return to client control (default)
bltcp.local_interval_ms = IntervalMode.OFF     # disable timed data entirely
```

## Built-in commands

Here's a full list of the commands handled by this library:

| Command | Description |
|---|---|
| `<BLAECK.GET_DEVICES,b1,b2,b3,b4[,Name,Type]>` | Writes the device information including the device name, hardware version, firmware version and library version. Optional `Name` and `Type` params identify the requesting client (see [Client identity](#client-identity)). |
| `<BLAECK.WRITE_SYMBOLS,b1,b2,b3,b4>` | Writes symbol list including datatype information |
| `<BLAECK.WRITE_DATA,b1,b2,b3,b4>` | Writes the binary data |
| `<BLAECK.ACTIVATE,b1,b2,b3,b4>` | Activates writing the binary data in user-set interval \[ms\] |
| `<BLAECK.DEACTIVATE>` | Deactivates writing in intervals |

`b1,b2,b3,b4` are four bytes encoding a little-endian integer. For `ACTIVATE` this is the interval in milliseconds. For the other commands it is the message ID echoed back in the response.

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

## Timestamps

Data frames can include timestamps. Set the `timestamp_mode` property to enable:

```python
from blaecktcpy import TimestampMode

# Microseconds since Unix epoch (absolute, real-time clock)
bltcp.timestamp_mode = TimestampMode.UNIX
```

Every write method auto-fills the timestamp based on the mode. Use `unix_timestamp` to override per-write:

```python
# UNIX mode — float seconds (converted internally) or int µs
bltcp.write_all_data(unix_timestamp=time.time())
bltcp.write_all_data(unix_timestamp=csv_epoch_seconds)
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

`bool`, `byte`, `short`, `unsigned short`, `int`, `unsigned int`, `long`, `unsigned long`, `float`, `double`

For DTYPE codes, byte sizes, and the binary wire format, see the [Protocol specification](docs/protocol.md).

## Client identity

When a client sends `<BLAECK.GET_DEVICES>`, it may include two optional parameters after the 4-byte message ID:

```
<BLAECK.GET_DEVICES,b1,b2,b3,b4,RequesterDeviceName,RequesterType>
```

| Parameter | Description |
|-----------|-------------|
| `RequesterDeviceName` | Device name of the requesting client (e.g. `Basic Hub`) |
| `RequesterType` | Role of the requesting client (free-form string, e.g. `hub`, `app`, `logger`). Defaults to `unknown` if omitted. |

The server binds the identity to the client connection and uses it in log messages:

```
Client #0 connected: 192.168.1.50:51478
Client #0 identified (hub: Basic Hub)
Client #0 disconnected (hub: Basic Hub)
```

Both parameters are optional — older clients that omit them still work. In hub mode, blaecktcpy automatically sends its device name and `hub` type when connecting to upstreams.

## Hub mode

The same `BlaeckTCPy` class serves as a hub when you add upstream connections with `add_tcp()` or `add_serial()`. The hub aggregates signals from multiple upstream devices and serves them as a single merged device, alongside any local signals.

```python
from blaecktcpy import BlaeckTCPy

hub = BlaeckTCPy("0.0.0.0", 23, "My Hub", "Python", "1.0")

hub.add_tcp("192.168.1.10", 24, name="ESP32")
hub.add_tcp("127.0.0.1", 25, name="Sine")

dew_point = hub.add_signal("DewPoint", "float")

hub.start()

while True:
    dew_point.value = compute_dew_point()
    hub.tick()
```

For upstream data rates, signal relay, schema change detection, command forwarding, and auto-reconnect, see the full [Hub documentation](docs/hub.md).

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
| `timestamps.py` | Timestamp modes (NONE, UNIX) for data frames |

### Hub

| Example | Description |
|---|---|
| `basic.py` | Aggregates two upstream servers and a local signal |
| `schema_change.py` | Runtime signal changes via custom commands with automatic re-discovery |
| `signal_processing.py` | Transform and compute signals via `on_data_received` |
| `mixed_sources.py` | BlaeckTCP microcontroller + SCPI power supply |

## License

MIT
