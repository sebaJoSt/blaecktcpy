import math
import time
from blaecktcpy import blaecktcpy, Signal

EXAMPLE_VERSION = "1.0"

ip = '127.0.0.1'
port = 23

bltcp = blaecktcpy(
    'Basic Sine Number Generator',
    'Python Script',
    EXAMPLE_VERSION,
    ip,
    port
)

# Create 200 sine signals
for i in range(1, 201):
    bltcp.add_signal(Signal(f'Sine_{i}', 'float', 0.0))

start_time = time.time()

print("##LOGGBOK:READY##")

def update_sine():
    elapsed_ms = (time.time() - start_time) * 1000
    value = math.sin(elapsed_ms * 0.00005)
    for s in bltcp.signals:
        s.value = value

while True:
    update_sine()
    bltcp.tick()
