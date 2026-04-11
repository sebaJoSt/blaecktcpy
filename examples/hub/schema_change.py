"""
BlaeckTCPy Example: Schema Change via Custom Command

Demonstrates an upstream device that adds/removes signals at runtime
in response to custom commands. The hub detects the schema hash change
and triggers automatic re-discovery.

  ┌──────────────────────────────┐
  │ Sensor :10024                │   <- upstream: signals change at runtime
  │  Temp (always)               │
  │  Pressure (added/removed)    │
  └──────────────┬───────────────┘
                 │
                 ▼
  ┌──────────────────────────────┐
  │ Hub :23                      │
  │  forwards ADD / REMOVE cmds  │
  └──────────────┬───────────────┘
                 │
                 ▼
         ┌──────────────┐
         │   Loggbok    │
         └──────────────┘

Setup:  python examples/hub/schema_change.py
Then:   Connect Loggbok to 127.0.0.1:23

From a second terminal, send commands via netcat/telnet:
  - Send "<ADD_PRESSURE>"    to add the Pressure signal
  - Send "<REMOVE_PRESSURE>" to remove it (back to Temp only)

The hub's schema hash changes automatically; Loggbok detects the
mismatch and stops logging (by design — prevents data corruption).
"""

import logging
import math
import time
import threading

from blaecktcpy import BlaeckTCPy, TimestampMode

EXAMPLE_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Upstream: a sensor that can dynamically add/remove signals
# ---------------------------------------------------------------------------
sensor = BlaeckTCPy(
             ip="127.0.0.1",
             port=10024,
             device_name="Sensor",
             device_hw_version="Python Script",
             device_fw_version=EXAMPLE_VERSION,
             log_level=logging.WARNING,
         )
sensor.timestamp_mode = TimestampMode.UNIX

temp = sensor.add_signal("Temp", "float")
has_pressure = False
pressure = None


@sensor.on_command("ADD_PRESSURE")
def handle_add_pressure():
    global has_pressure, pressure
    if not has_pressure:
        pressure = sensor.add_signal("Pressure", "float")
        has_pressure = True
        print("[Sensor] Added Pressure signal")
    else:
        print("[Sensor] Pressure already exists")


@sensor.on_command("REMOVE_PRESSURE")
def handle_remove_pressure():
    global has_pressure, pressure
    if has_pressure:
        sensor.delete_signals()  # removes all local signals
        sensor.add_signal("Temp", "float")  # re-add Temp
        has_pressure = False
        pressure = None
        print("[Sensor] Removed Pressure signal")
    else:
        print("[Sensor] Pressure not present")


sensor.start()


def run_sensor():
    while True:
        t = time.time()
        sensor.signals[0].value = 20.0 + 5.0 * math.sin(t * 0.5)  # Temp
        if has_pressure and len(sensor.signals) > 1:
            sensor.signals[1].value = 1013.25 + 10.0 * math.cos(t * 0.3)
        sensor.tick()


threading.Thread(target=run_sensor, daemon=True).start()
time.sleep(0.2)

# ---------------------------------------------------------------------------
# Hub: custom commands are forwarded to upstreams automatically
# ---------------------------------------------------------------------------
hub = BlaeckTCPy(
          ip="127.0.0.1",
          port=23,
          device_name="Schema Change Hub",
          device_hw_version="Python Script",
          device_fw_version=EXAMPLE_VERSION,
      )
hub.timestamp_mode = TimestampMode.UNIX
hub.local_interval_ms = 500

hub.add_tcp("127.0.0.1", 10024, "Sensor", interval_ms=300)

hub.start()

print("Hub running on 127.0.0.1:23")
print("Send <ADD_PRESSURE> or <REMOVE_PRESSURE> to the hub to change signals at runtime.")
print("##LOGGBOK:READY##")

while True:
    hub.tick()
