"""
BlaeckTCPy Example: Basic Hub

Aggregates signals from two upstream devices and one local
signal, then serves everything as a single merged device.

  ┌──────────────┐   ┌──────────────┐
  │ Sine :10024  │   │ Cosine :10025│   ← embedded upstream servers
  └──────┬───────┘   └──────┬───────┘
         │                  │
         ▼                  ▼
  ┌─────────────────────────────────┐
  │       Hub :23                   │
  │  Sawtooth_1 (local signal)      │
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │
          └──────────────┘

Setup:  python examples/hub/basic.py
Then:   Connect Loggbok to 127.0.0.1:23
"""

import logging
import math
import time
import threading

from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

# --- Upstream servers ---
sine = BlaeckTCPy(
           ip="127.0.0.1",
           port=10024,
           device_name="Sine Generator",
           device_hw_version="Python Script",
           device_fw_version=EXAMPLE_VERSION,
           log_level=logging.WARNING,
       )
for i in range(1, 4):
    sine.add_signal(f"Sine_{i}", "float")
sine.start()

cosine = BlaeckTCPy(
             ip="127.0.0.1",
             port=10025,
             device_name="Cosine Generator",
             device_hw_version="Python Script",
             device_fw_version=EXAMPLE_VERSION,
             log_level=logging.WARNING,
         )
for i in range(1, 3):
    cosine.add_signal(f"Cosine_{i}", "float")
cosine.start()


def run_server(server, gen_func):
    while True:
        t = (time.time() - server.start_time) * 1000
        for s in server.signals:
            s.value = gen_func(t)
        server.tick()
        time.sleep(0.001)  # Prevent busy loop; reduce or remove if faster response is needed


threading.Thread(target=run_server, args=(sine, lambda t: math.sin(t * 0.001)), daemon=True).start()
threading.Thread(target=run_server, args=(cosine, lambda t: math.cos(t * 0.0005)), daemon=True).start()
time.sleep(0.2)

# --- Hub ---
hub = BlaeckTCPy(
          ip="127.0.0.1",
          port=23,
          device_name="Basic Hub",
          device_hw_version="Python Script",
          device_fw_version=EXAMPLE_VERSION,
      )

# Local signal
sawtooth = hub.add_signal("Sawtooth_1", "float")
hub.local_interval_ms = 500

# Connect to upstream servers
hub.add_tcp("127.0.0.1", 10024, "Sine", interval_ms=300)
hub.add_tcp("127.0.0.1", 10025, "Cosine", interval_ms=300)
# Optional serial upstream (requires: pip install blaecktcpy[serial])
# hub.add_serial("COM3", 115200, "Arduino", interval_ms=300)

hub.start()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - hub.start_time) * 1000
    sawtooth.value = (elapsed_ms % 5000) / 5000.0  # 0..1 over 5 seconds
    hub.tick()
    time.sleep(0.001)  # Prevent busy loop; reduce or remove if faster response is needed
