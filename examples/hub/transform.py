"""
BlaeckHub Example: Signal Transform (relay_downstream=True)

Connects to an Arduino running BlaeckSerial that reports temperature
in Fahrenheit. The hub transforms the value to Celsius in-place
before relaying it to Loggbok — the original signal name and slot
are preserved.

This demonstrates that ``on_data_received`` fires **before** the
data frame is sent downstream, so modifying ``upstream.signals``
in the callback changes what Loggbok receives.

  ┌──────────────┐
  │ Arduino COM3 │   ← upstream (temp_f) — relayed as Celsius
  └──────┬───────┘
         │
         ▼
  ┌─────────────────────────────────┐
  │         BlaeckHub :23           │
  │  on_data_received → °F to °C   │   ← this script
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │              ← sees temp_f with °C values
          └──────────────┘

Setup:
  1. Flash your Arduino with a BlaeckSerial sketch that reports
     a signal named "temp_f" in Fahrenheit
  2. Adjust COM port and baudrate below
  3. Run:  python examples/hub/transform.py
  4. Connect a BlaeckTCP client to 127.0.0.1:23

Requires: pip install blaecktcpy[serial]
"""

from blaecktcpy import BlaeckHub

EXAMPLE_VERSION = "1.0"

hub = BlaeckHub("127.0.0.1", 23, "Transform Hub", "Python Script", EXAMPLE_VERSION)

# Connect to Arduino — relay_downstream=True (default): Loggbok sees the signals
arduino = hub.add_serial("COM3", 9600, "Arduino")


@hub.on_data_received("Arduino")
def fahrenheit_to_celsius(upstream):
    """Convert temperature in-place before it is relayed downstream."""
    sig = upstream.signals["temp_f"]
    sig.value = (sig.value - 32) * 5 / 9


# Start the hub (signal list is now frozen)
hub.start()

print("##LOGGBOK:READY##")

while True:
    hub.tick()
