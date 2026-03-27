"""Sine Generator — serves sine signals on port 24."""

import math
import time
from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

bltcp = BlaeckTCPy("127.0.0.1", 24, "Sine Generator", "Python Script", EXAMPLE_VERSION)

for i in range(1, 4):
    bltcp.add_signal(f"Sine_{i}", "float")

start_time = time.time()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - start_time) * 1000
    value = math.sin(elapsed_ms * 0.001)
    for s in bltcp.signals:
        s.value = value
    bltcp.tick()
