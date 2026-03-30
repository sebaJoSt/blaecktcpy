"""
BlaeckHub Example: Basic Hub

Aggregates signals from two upstream BlaeckServers and one local
signal, then serves everything as a single merged device.

  ┌──────────────┐   ┌──────────────┐
  │ Sine :10024  │   │ Cosine :10025│   ← embedded upstream servers
  └──────┬───────┘   └──────┬───────┘
         │                  │
         ▼                  ▼
  ┌─────────────────────────────────┐
  │       BlaeckHub :10023          │
  │  room_temp (local signal)       │
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │
          └──────────────┘

Setup:  python examples/hub/basic.py
Then:   Connect Loggbok to 127.0.0.1:10023
"""

import math
import time
import threading

from blaecktcpy import BlaeckServer, BlaeckHub

EXAMPLE_VERSION = "1.0"

# --- Upstream servers ---
sine = BlaeckServer("127.0.0.1", 10024, "Sine Generator", "Python Script", EXAMPLE_VERSION)
for i in range(1, 4):
    sine.add_signal(f"Sine_{i}", "float")

cosine = BlaeckServer("127.0.0.1", 10025, "Cosine Generator", "Python Script", EXAMPLE_VERSION)
for i in range(1, 3):
    cosine.add_signal(f"Cosine_{i}", "float")


def run_server(server, gen_func):
    start = time.time()
    while True:
        t = (time.time() - start) * 1000
        for s in server.signals:
            s.value = gen_func(t)
        server.tick()


threading.Thread(target=run_server, args=(sine, lambda t: math.sin(t * 0.001)), daemon=True).start()
threading.Thread(target=run_server, args=(cosine, lambda t: math.cos(t * 0.0005)), daemon=True).start()
time.sleep(0.2)

# --- Hub ---
hub = BlaeckHub("127.0.0.1", 10023, "Basic Hub", "Python Script", EXAMPLE_VERSION)

# Local signal
room_temp = hub.add_signal("room_temp", "float")
hub.set_local_interval(500)

# Connect to upstream servers
hub.add_tcp("127.0.0.1", 10024, interval_ms=300)
hub.add_tcp("127.0.0.1", 10025, "Cosine", interval_ms=300)

hub.start()
print("##LOGGBOK:READY##")

start_time = time.time()
while True:
    elapsed_ms = (time.time() - start_time) * 1000
    room_temp.value = 20.0 + math.sin(elapsed_ms * 0.0005) * 5.0
    hub.tick()
