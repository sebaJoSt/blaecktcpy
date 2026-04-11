"""Sine Generator — serves sine signals on port 23."""

import math
import time

from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

bltcp = BlaeckTCPy(
            ip="127.0.0.1",
            port=23,
            device_name="Sine Generator",
            device_hw_version="Python Script",
            device_fw_version=EXAMPLE_VERSION,
        )

for i in range(1, 4):
    bltcp.add_signal(f"Sine_{i}", "float")

bltcp.start()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - bltcp.start_time) * 1000
    value = math.sin(elapsed_ms * 0.001)
    for s in bltcp.signals:
        s.value = value
    bltcp.tick()
