# pyright: reportUnusedCallResult=false
"""Sine Generator — serves three sine signals over TCP.

The simplest blaecktcpy example. Run this, then connect Loggbok
(or any BlaeckTCP client) to 127.0.0.1:23 to see live data.

A status page is available at http://127.0.0.1:8080.
"""

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
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

while True:
    elapsed_ms = (time.time() - bltcp.start_time) * 1000
    value = math.sin(elapsed_ms * 0.001)  # one full cycle every ~6.3 seconds
    for s in bltcp.signals:
        s.value = value
    bltcp.tick()
    time.sleep(0.001)  # Prevent busy loop; reduce or remove if faster response is needed
