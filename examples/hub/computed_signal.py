"""
BlaeckHub Example: Serial Upstream with Dew Point

Connects to an Arduino running BlaeckSerial that reports temperature
and humidity, then computes the dew point in Python and serves
only the dew point to Loggbok on port 23.

This demonstrates ``relay=False``: the Arduino's raw signals are
decoded hub-side for computation but hidden from Loggbok.
The ``on_data_received`` callback recomputes the dew point only
when fresh data arrives from the Arduino.

  ┌──────────────┐
  │ Arduino COM3 │   ← upstream (temperature, humidity) — not relayed
  └──────┬───────┘
         │
         ▼
  ┌─────────────────────────────────┐
  │         BlaeckHub :23           │
  │  dew_point (computed locally)   │   ← this script
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │              ← only sees dew_point
          └──────────────┘

Setup:
  1. Flash your Arduino with a BlaeckSerial sketch that reports
     temperature (°C) and relative humidity (%)
  2. Adjust COM port and baudrate below
  3. Run:  python examples/hub/computed_signal.py
  4. Connect a BlaeckTCP client to 127.0.0.1:23

Requires: pip install blaecktcpy[serial]
"""

import math

from blaecktcpy import BlaeckHub

EXAMPLE_VERSION = "1.0"

hub = BlaeckHub("127.0.0.1", 23, "Weather Hub", "Python Script", EXAMPLE_VERSION)

# Local signal — computed from Arduino's temperature and humidity
dew_point = hub.add_signal("dew_point", "float")

# Connect to Arduino over serial (relay=False: Loggbok won't see raw signals)
# Set dtr=False for Arduino Mega to prevent reset on connect
arduino = hub.add_serial("COM3", 9600, "Arduino", relay=False)


@hub.on_data_received("Arduino")
def on_arduino_data(upstream):
    """Recompute dew point when fresh data arrives."""
    temp_val = upstream.signals["temperature"].value
    rh_val = upstream.signals["humidity"].value

    if rh_val > 0:
        # Magnus formula for dew point
        a, b = 17.27, 237.7
        alpha = (a * temp_val) / (b + temp_val) + math.log(rh_val / 100.0)
        dew_point.value = (b * alpha) / (a - alpha)


# Start the hub (signal list is now frozen)
hub.start()

print("##LOGGBOK:READY##")

while True:
    hub.tick()
