"""
Diagnostic: TreeView test.

Runs two BlaeckServers and a BlaeckHub in one script.
Connect Loggbok to 127.0.0.1:10023 to see the device tree:

  My Hub (hub, master)
  ├─ DewPoint, RoomTemp, CO2Level (local signals on master)
  ├─ Sine Generator (server)
  └─ Cosine Generator (server)

Setup:  python examples/hub/treeview_test.py
Then:   Connect Loggbok to 127.0.0.1:10023
"""

import math
import time
import threading

from blaecktcpy import BlaeckServer, BlaeckHub

# --- Server A: Sine on port 10024 ---
server_a = BlaeckServer("127.0.0.1", 10024, "Sine Generator", "Python Script", "1.0")
for i in range(1, 4):
    server_a.add_signal(f"Sine_{i}", "float")

# --- Server B: Cosine on port 10025 ---
server_b = BlaeckServer("127.0.0.1", 10025, "Cosine Generator", "Python Script", "1.0")
for i in range(1, 3):
    server_b.add_signal(f"Cosine_{i}", "float")


# Run servers in background threads so they respond during hub discovery
def run_server(server, gen_func):
    start = time.time()
    while True:
        t = (time.time() - start) * 1000
        val = gen_func(t)
        for s in server.signals:
            s.value = val
        server.tick()


threading.Thread(
    target=run_server,
    args=(server_a, lambda t: math.sin(t * 0.001)),
    daemon=True,
).start()

threading.Thread(
    target=run_server,
    args=(server_b, lambda t: math.cos(t * 0.0005)),
    daemon=True,
).start()

# Give servers a moment to start ticking
time.sleep(0.2)

# --- Hub on port 10023 ---
hub = BlaeckHub("127.0.0.1", 10023, "My Hub", "Python Script", "1.0")
dew_point = hub.add_signal("DewPoint", "float")
room_temp = hub.add_signal("RoomTemp", "float")
co2_level = hub.add_signal("CO2Level", "unsigned short")
hub.set_local_interval(500)
hub.add_tcp("127.0.0.1", 10024, interval_ms=300)
hub.add_tcp("127.0.0.1", 10025, interval_ms=300)
hub.start()

print("TreeView Test running — connect Loggbok to 127.0.0.1:10023")
print("##LOGGBOK:READY##")

start = time.time()
while True:
    t = (time.time() - start) * 1000
    dew_point.value = 12.0 + math.sin(t * 0.0003) * 3.0
    room_temp.value = 21.5 + math.sin(t * 0.0005) * 2.0
    co2_level.value = int(400 + math.sin(t * 0.0002) * 100)
    hub.tick()
