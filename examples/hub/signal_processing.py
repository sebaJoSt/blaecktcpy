# pyright: reportUnusedCallResult=false
"""
BlaeckTCPy Example: Signal Processing

Demonstrates processing upstream data before it reaches Loggbok:
- Transform a relayed signal in-place (Fahrenheit to Celsius)
- Compute a new local signal from upstream values (dew point)
- Use on_before_write to update a local signal right before transmission

  ┌────────────────────┐
  │  Sensor Server     │   ← upstream (temperature, humidity) on port 10024
  └─────────┬──────────┘
            │
            ▼
  ┌─────────────────────────────────────────┐
  │            Hub :23                       │
  │  temperature → transformed to Celsius       │
  │  humidity → relayed as-is              │
  │  dew_point → computed locally          │
  │  write_count → updated on_before_write │
  └───────────────────┬─────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │   Loggbok    │   ← sees temperature (°C), humidity, dew_point, write_count
              └──────────────┘

Setup:  python examples/hub/signal_processing.py
Then:   Connect Loggbok to 127.0.0.1:23
"""

import logging
import math
import time
import threading

from blaecktcpy import BlaeckTCPy, UpstreamDevice

# --- Upstream server simulating a sensor board ---
server = BlaeckTCPy(
    ip="127.0.0.1",
    port=10024,
    device_name="Sensor Board",
    log_level=logging.WARNING,
    http_port=None,
)
server.add_signal(
    "temperature", "float"
)  # temperature in Fahrenheit (transformed by hub)
server.add_signal("humidity", "float")  # relative humidity %
server.start()


def run_server():
    while True:
        t = (time.time() - server.start_time) * 1000
        server.signals[0].value = 72.0 + math.sin(t * 0.0005) * 5.0  # ~68-77 °F
        server.signals[1].value = 55.0 + math.sin(t * 0.0003) * 15.0  # ~40-70 %
        server.tick()
        time.sleep(
            0.001
        )  # Prevent busy loop; reduce or remove if faster response is needed


threading.Thread(target=run_server, daemon=True).start()
time.sleep(0.2)

# --- Hub ---
hub = BlaeckTCPy(
    ip="127.0.0.1",
    port=23,
    device_name="Sensor Hub",
)

# Computed local signals
dew_point = hub.add_signal("dew_point", "float")
write_count = hub.add_signal("write_count", "unsigned int")

# Upstream — relayed so Loggbok sees temperature and humidity
hub.add_tcp("127.0.0.1", 10024, "Sensor", interval_ms=500)


@hub.on_data_received("Sensor")
def on_sensor_data(upstream: UpstreamDevice):
    """Transform temperature to Celsius and compute dew point."""
    temp_sig = upstream.signals["temperature"]
    rh = upstream.signals["humidity"].value

    # Transform in-place: Loggbok receives Celsius, not Fahrenheit
    temp_c = (temp_sig.value - 32) * 5 / 9
    temp_sig.value = temp_c

    # Compute dew point (Magnus formula)
    if rh > 0:
        a, b = 17.27, 237.7
        alpha = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
        dew_point.value = (b * alpha) / (a - alpha)


@hub.on_before_write()
def before_write():
    """Called right before each data frame is sent to clients."""
    write_count.value += 1


hub.start()
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

while True:
    hub.tick()
    time.sleep(
        0.001
    )  # Prevent busy loop; reduce or remove if faster response is needed
