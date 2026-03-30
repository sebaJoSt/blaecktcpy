"""Sine & Cosine Generator — serves sine on port 24 and cosine on port 25."""

import math
import time
import threading

from blaecktcpy import BlaeckServer

EXAMPLE_VERSION = "1.0"

sine = BlaeckServer("127.0.0.1", 24, "Sine Generator", "Python Script", EXAMPLE_VERSION)
for i in range(1, 4):
    sine.add_signal(f"Sine_{i}", "float")

cosine = BlaeckServer("127.0.0.1", 25, "Cosine Generator", "Python Script", EXAMPLE_VERSION)
for i in range(1, 3):
    cosine.add_signal(f"Cosine_{i}", "float")

start_time = time.time()


def run_server(server, gen_func):
    while True:
        t = (time.time() - start_time) * 1000
        for s in server.signals:
            s.value = gen_func(t)
        server.tick()


threading.Thread(target=run_server, args=(cosine, lambda t: math.cos(t * 0.0001)), daemon=True).start()

print("##LOGGBOK:READY##")
run_server(sine, lambda t: math.sin(t * 0.001))
