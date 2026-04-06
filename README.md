# blaecktcpy

A Python TCP server for real-time streaming of named, typed signals in binary format. Use it to turn any Python script into a signal source that Loggbok or any compatible TCP client can connect to, visualize, and log.

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
bltcp.add_signal('Sine_1', 'float', 0.0)       # name, datatype, initial value
bltcp.add_signal(Signal('Temperature', 'double', 0.0))
```

Signals are stored in a `SignalList` and can be accessed by index or name:

```python
bltcp.signals[0].value = 1.0
bltcp.signals["Temperature"].value = 22.5
```

### Start the device

Call `start()` after setup (adding signals, configuring interval) and before using `tick()`, `read()`, or `write()`:

```python
bltcp.start()
```

### Update your variables and don't forget to `tick()`!

`tick()` checks for incoming client commands and sends timed data frames when due:

```python
import math, time

start = time.time()
while True:
    bltcp.signals[0].value = math.sin((time.time() - start) * 0.1)
    bltcp.tick()
```

### Server-controlled interval

By default, connected clients (e.g. Loggbok) control the data rate by sending `ACTIVATE`/`DEACTIVATE` commands.
Use the `local_interval_ms` property to lock the device to a fixed rate instead:

```python
from blaecktcpy import IntervalMode

bltcp.local_interval_ms = 500                  # send every 500 ms, ignore client ACTIVATE/DEACTIVATE
bltcp.local_interval_ms = IntervalMode.CLIENT  # return to client control (default)
bltcp.local_interval_ms = IntervalMode.OFF     # disable timed data entirely
```

## Built-in commands

These commands are handled automatically by the library. Connected clients (e.g. Loggbok) send them — you typically don't need to construct them yourself:

| Command | Description |
|---|---|
| `<BLAECK.GET_DEVICES>` | Returns device name, hardware/firmware version, library version |
| `<BLAECK.WRITE_SYMBOLS>` | Returns the signal list with datatype information |
| `<BLAECK.WRITE_DATA>` | Returns the current signal values as binary data |
| `<BLAECK.ACTIVATE,b1,b2,b3,b4>` | Starts streaming data at the given interval (4 bytes, little-endian milliseconds) |
| `<BLAECK.DEACTIVATE>` | Stops streaming data |

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

Every write method auto-fills the timestamp based on the mode. You can override it per-write:

```python
bltcp.write_all_data(unix_timestamp=time.time())       # float seconds (converted internally)
bltcp.write_all_data(unix_timestamp=1712361600000000)   # or int microseconds directly
```

The `start_time` property exposes the `time.time()` value captured at `start()`:

```python
elapsed = time.time() - bltcp.start_time
```

## Client callbacks

Every TCP client that connects is automatically added to `data_clients` and receives data frames. Use callbacks to react to connections or control which clients receive data:

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
