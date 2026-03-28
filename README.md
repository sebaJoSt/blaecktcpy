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

### Update your variables and don't forget to `tick()`!

```python
import math, time

start = time.time()
while True:
    bltcp.signals[0].value = math.sin((time.time() - start) * 0.1)
    bltcp.tick()
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

Custom commands can be registered with the `@bltcp.on()` decorator:

```python
@bltcp.on("SET_LED")
def handle_led(state):
    print(f"LED = {state}")
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

## BlaeckHub

`BlaeckHub` aggregates signals from multiple upstream BlaeckTCP(y) or BlaeckSerial devices and serves them as a single merged device. It can also add local signals computed in Python.

```python
from blaecktcpy import BlaeckHub

hub = BlaeckHub("0.0.0.0", 23, "My Hub", "Python", "1.0")

# Connect to upstream devices
hub.add_tcp("192.168.1.10", 24, name="ESP32")
hub.add_tcp("127.0.0.1", 25, name="Sine")

# Add a local signal
temperature = hub.add_signal("DewPoint", "float")

hub.start()

while True:
    temperature.value = compute_dew_point()
    hub.tick()
```

Serial upstreams are also supported (`pip install blaecktcpy[serial]`):

```python
hub.add_serial("COM3", 115200, name="Arduino")
```

## Examples

See the [examples](examples/) folder:

### Server

| Example | Description |
|---|---|
| `sine.py` | Sine wave generator |
| `cosine.py` | Cosine wave generator |
| `datatype_test.py` | Tests all supported datatypes including edge cases |
| `command_parser.py` | Custom command handling with `@bltcp.on()` |
| `csv_reader.py` | Stream CSV file data as signals |
| `csv_generator.py` | Generate test CSV data for `csv_reader.py` |

### Hub

| Example | Description |
|---|---|
| `basic.py` | Aggregates two upstream servers and a local signal |
| `computed_signal.py` | Serial upstream with a Python-computed dew point signal |
| `local_only.py` | Hub with only local signals (no upstreams) |

## License

MIT
