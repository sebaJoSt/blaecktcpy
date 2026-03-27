"""
BlaeckHub Example: Serial Upstream with Dew Point

Connects to an Arduino running BlaeckSerial that reports temperature
and humidity, then computes the dew point in Python and serves
everything (Arduino signals + dew point) to Loggbok on port 23.

This demonstrates why a hub is useful: the Arduino stays simple
(just reads sensors), while the Python side adds derived signals
without changing the firmware.

  ┌──────────────┐
  │ Arduino COM3 │   ← upstream (temperature, humidity)
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
          │   Loggbok    │              ← downstream client
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

# Connect to Arduino over serial
# Set dtr=False for Arduino Mega to prevent reset on connect
hub.add_serial("COM3", 9600, "Arduino")

# Start the hub (signal list is now frozen)
hub.start()

print("##LOGGBOK:READY##")

while True:
    # After start(), hub.signals contains local signals first, then
    # upstream signals in discovery order.  Adjust the indices to match
    # the temperature and humidity signals from your Arduino sketch.
    temp_idx = 1  # first upstream signal (temperature in °C)
    rh_idx = 2  # second upstream signal (relative humidity in %)

    temp_val = hub.signals[temp_idx].value
    rh_val = hub.signals[rh_idx].value

    if rh_val > 0:
        # Magnus formula for dew point
        a, b = 17.27, 237.7
        alpha = (a * temp_val) / (b + temp_val) + math.log(rh_val / 100.0)
        dew_point.value = (b * alpha) / (a - alpha)

    hub.tick()
