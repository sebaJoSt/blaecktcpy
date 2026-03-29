"""
BlaeckHub Example: Basic Hub

Aggregates signals from two upstream BlaeckServer servers and one local
signal, then serves everything as a single merged device on port 23.

  ┌──────────────┐   ┌──────────────┐
  │   sine :24   │   │ cosine :25   │   ← upstream devices
  └──────┬───────┘   └──────┬───────┘
         │                  │
         ▼                  ▼
  ┌─────────────────────────────────┐
  │         BlaeckHub :23           │
  │  room_temp (local signal)       │   ← this script
  └───────────────┬─────────────────┘
                  │
                  ▼
          ┌──────────────┐
          │   Loggbok    │              ← downstream client
          └──────────────┘

Local signals have their own timing — either a fixed interval set by
the hub (set_local_interval) or controlled by the downstream client
via <BLAECK.ACTIVATE>.  Upstream devices can likewise use a fixed
interval (interval_ms) or follow the client.

Setup — run these three scripts in separate terminals:

  Terminal 1:  python examples/server/sine.py     (port 24, 3 sine signals)
  Terminal 2:  python examples/server/cosine.py   (port 25, 2 cosine signals)
  Terminal 3:  python examples/hub/basic.py       (port 23, merged signals)

Then connect a BlaeckTCP client to 127.0.0.1:23.
"""

import math
import time

from blaecktcpy import BlaeckHub

EXAMPLE_VERSION = "1.0"

hub = BlaeckHub("127.0.0.1", 23, "Basic Hub", "Python Script", EXAMPLE_VERSION)

# Local signals
room_temp = hub.add_signal("room_temp", "float")
hub.set_local_interval(500)  # Comment out to use <BLAECK.ACTIVATE>

# Connect to upstream devices
hub.add_tcp("127.0.0.1", 24, interval_ms=300)
hub.add_tcp("127.0.0.1", 25, "Cosine")  # uses <BLAECK.ACTIVATE>
# hub.add_tcp("172.31.230.22", 23, "Sine_C")
# hub.add_serial("COM24", 9600, "Arduino", dtr=False)

# Start the hub (signal list is now frozen)
hub.start()


# Optional: callbacks for upstream connection events
@hub.on_upstream_disconnected()
def on_disconnect(name):
    print(f"  !! Upstream '{name}' disconnected")


start_time = time.time()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - start_time) * 1000
    room_temp.value = 20.0 + math.sin(elapsed_ms * 0.0005) * 5.0

    hub.tick()
