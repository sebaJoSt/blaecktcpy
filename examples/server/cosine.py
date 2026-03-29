"""Cosine Generator — serves cosine signals on port 25."""

import math
import time
from blaecktcpy import BlaeckServer

EXAMPLE_VERSION = "1.0"

bltcp = BlaeckServer(
    "127.0.0.1", 25, "Cosine Generator", "Python Script", EXAMPLE_VERSION
)

for i in range(1, 3):
    bltcp.add_signal(f"Cosine_{i}", "float")

start_time = time.time()
print("##LOGGBOK:READY##")

while True:
    elapsed_ms = (time.time() - start_time) * 1000
    value = math.cos(elapsed_ms * 0.0001)
    for s in bltcp.signals:
        s.value = value
    bltcp.tick()
