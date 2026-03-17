# blaecktcpy

Python implementation of the [BlaeckTCP](https://github.com/sebaJoSt/BlaeckTCP) protocol — compatible with Loggbok.

## Installation

```bash
pip install blaecktcpy
```

## Usage

```python
from blaecktcpy import blaecktcpy, Signal

# Create a TCP server
bltcp = blaecktcpy(
    device_name='My Device',
    device_hw_version='1.0',
    device_fw_version='1.0',
    ip='127.0.0.1',
    port=23
)

# Add signals
bltcp.add_signal(Signal('Temperature', 'float', 0.0))
bltcp.add_signal(Signal('Counter',     'int',   0))

# Main loop
while True:
    bltcp.signals[0].value = 23.5
    bltcp.signals[1].value += 1
    bltcp.tick()
```

### Supported datatypes

| Datatype         | Bytes |
|------------------|-------|
| `bool`           | 1     |
| `byte`           | 1     |
| `short`          | 2     |
| `unsigned short` | 2     |
| `int`            | 4     |
| `unsigned int`   | 4     |
| `long`           | 4     |
| `unsigned long`  | 4     |
| `float`          | 4     |
| `double`         | 8     |

## License

MIT
