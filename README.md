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

bltcp = BlaeckTCPy('My Device', '1.0', '1.0', '127.0.0.1', 23)
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

## Examples

See the [examples](examples/) folder:

| Example | Description |
|---|---|
| `sine_generator_basic.py` | Basic sine wave generator with 200 signals |
| `datatype_test.py` | Tests all supported datatypes including edge cases |
| `command_parser.py` | Custom command handling with `@bltcp.on()` |
| `csv_reader.py` | Stream CSV file data as signals |
| `csv_generator.py` | Generate test CSV data for `csv_reader.py` |

## License

MIT
