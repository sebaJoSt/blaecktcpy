# pyright: reportUnusedCallResult=false
"""
Timestamp Modes — blaecktcpy Example
=====================================
Demonstrates the timestamp modes available for data frames.

Pick one mode by uncommenting the corresponding block below:

  NONE    — no timestamp in the frame (default, backward-compatible)
  UNIX    — microseconds since Unix epoch (wall-clock time)

You can also override the automatic timestamp on any individual write
by passing ``unix_timestamp`` (float seconds or int µs) explicitly.

Usage:
    python timestamps.py
    Connect Loggbok to 127.0.0.1:23
"""

import math
import time

from blaecktcpy import BlaeckTCPy, TimestampMode

bltcp = BlaeckTCPy(
    ip="127.0.0.1",
    port=23,
    device_name="Timestamp Demo",
)

bltcp.add_signal("Sine", "float")
bltcp.add_signal("Cosine", "float")

# ── Choose a timestamp mode ──────────────────────────────────────────

# Mode 1: No timestamp (default)
# bltcp.timestamp_mode = TimestampMode.NONE

# Mode 2: Wall-clock microseconds (Unix epoch)
bltcp.timestamp_mode = TimestampMode.UNIX

# ── Interval & start ─────────────────────────────────────────────────

bltcp.local_interval_ms = 500
bltcp.start()
print(f"Timestamp mode : {bltcp.timestamp_mode.name}")
print(f"Interval       : {bltcp.local_interval_ms} ms")
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

while True:
    t = time.time()
    bltcp.signals[0].value = math.sin(t)
    bltcp.signals[1].value = math.cos(t)

    # The timed_write methods use the auto-timestamp from the chosen mode.
    # To supply your own timestamp instead, pass unix_timestamp explicitly:
    #   bltcp.timed_write_all_data(unix_timestamp=time.time())
    bltcp.tick()
    time.sleep(
        0.001
    )  # Prevent busy loop; reduce or remove if faster response is needed
