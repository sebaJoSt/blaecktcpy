"""
BlaeckHub Example: Local-Only Hub

A hub with no upstream devices — only local signals.
Demonstrates using BlaeckHub as a standalone BlaeckTCP server
with timing controlled by the downstream client (Loggbok).

  ┌─────────────────────────────────┐
  │         BlaeckHub :23           │
  │  temperature, humidity (local)  │   ← this script
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │              ← downstream client
          └──────────────┘

Setup — run in a single terminal:

  python examples/hub/local_only.py

Then connect a BlaeckTCP client to 127.0.0.1:23.
"""

import math
import time

from blaecktcpy import BlaeckHub

EXAMPLE_VERSION = "1.0"

hub = BlaeckHub("127.0.0.1", 23, "Local Hub", "Python Script", EXAMPLE_VERSION)

# Local signals — timing controlled by Loggbok via <BLAECK.ACTIVATE>
temperature = hub.add_signal("temperature", "float")
humidity = hub.add_signal("humidity", "float")

# Start the hub (signal list is now frozen)
hub.start()

start_time = time.time()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - start_time) * 1000
    temperature.value = 22.0 + math.sin(elapsed_ms * 0.0003) * 3.0
    humidity.value = 55.0 + math.cos(elapsed_ms * 0.0005) * 10.0

    hub.tick()
