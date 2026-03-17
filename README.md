# blaecktcpy

Python implementation of the [BlaeckTCP](https://github.com/sebaJoSt/BlaeckTCP) protocol — compatible with Loggbok.

## Installation

```bash
pip install blaecktcpy
```

## Usage

```python
import math, time
from blaecktcpy import blaecktcpy, Signal

bltcp = blaecktcpy('My Device', '1.0', '1.0', '127.0.0.1', 23)
bltcp.add_signal(Signal('Sine', 'float', 0.0))

start = time.time()
while True:
    bltcp.signals[0].value = math.sin((time.time() - start) * 2 * math.pi)
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
