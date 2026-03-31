"""
BlaeckTCPy Example: Signal Processing

Demonstrates processing upstream data before it reaches Loggbok:
- Transform a relayed signal in-place (Fahrenheit to Celsius)
- Compute a new local signal from upstream values (dew point)

  ┌────────────────────┐
  │  Sensor Server     │   ← upstream (temp_f, humidity) on port 10024
  └─────────┬──────────┘
            │
            ▼
  ┌─────────────────────────────────────────┐
  │            Hub :23                       │
  │  temp_f → transformed to Celsius       │
  │  humidity → relayed as-is              │
  │  dew_point → computed locally          │
  └───────────────────┬─────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │   Loggbok    │   ← sees temp_f (°C), humidity, dew_point
              └──────────────┘

Setup:  python examples/hub/signal_processing.py
Then:   Connect Loggbok to 127.0.0.1:23
"""

import math
import time
import threading

from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

# --- Upstream server simulating a sensor board ---
server = BlaeckTCPy("127.0.0.1", 10024, "Sensor Board", "Python Script", EXAMPLE_VERSION)
server.add_signal("temp_f", "float")       # temperature in Fahrenheit
server.add_signal("humidity", "float")     # relative humidity %
server.start()


def run_server():
    start = time.time()
    while True:
        t = (time.time() - start) * 1000
        server.signals[0].value = 72.0 + math.sin(t * 0.0005) * 5.0   # ~68-77 °F
        server.signals[1].value = 55.0 + math.sin(t * 0.0003) * 15.0  # ~40-70 %
        server.tick()


threading.Thread(target=run_server, daemon=True).start()
time.sleep(0.2)

# --- Hub ---
hub = BlaeckTCPy("127.0.0.1", 23, "Sensor Hub", "Python Script", EXAMPLE_VERSION)

# Computed local signal
dew_point = hub.add_signal("dew_point", "float")

# Upstream — relayed so Loggbok sees temp_f and humidity
hub.add_tcp("127.0.0.1", 10024, "Sensor", interval_ms=500)


@hub.on_data_received("Sensor")
def on_sensor_data(upstream):
    """Transform temp_f to Celsius and compute dew point."""
    temp_sig = upstream.signals["temp_f"]
    rh = upstream.signals["humidity"].value

    # Transform in-place: Loggbok receives Celsius, not Fahrenheit
    temp_c = (temp_sig.value - 32) * 5 / 9
    temp_sig.value = temp_c

    # Compute dew point (Magnus formula)
    if rh > 0:
        a, b = 17.27, 237.7
        alpha = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
        dew_point.value = (b * alpha) / (a - alpha)


hub.start()
print("##LOGGBOK:READY##")

while True:
    hub.tick()
