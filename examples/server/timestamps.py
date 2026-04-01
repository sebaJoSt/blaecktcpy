"""
Timestamp Modes — blaecktcpy Example
=====================================
Demonstrates the three timestamp modes available for data frames.

Pick one mode by uncommenting the corresponding block below:

  NONE    — no timestamp in the frame (default, backward-compatible)
  MICROS  — microseconds since start() was called
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

EXAMPLE_VERSION = "1.0"

bltcp = BlaeckTCPy(
    "127.0.0.1", 23, "Timestamp Demo", "Python Script", EXAMPLE_VERSION
)

bltcp.add_signal("Sine", "float")
bltcp.add_signal("Cosine", "float")

# ── Choose a timestamp mode ──────────────────────────────────────────

# Mode 1: No timestamp (default)
# bltcp.timestamp_mode = TimestampMode.NONE

# Mode 2: Microseconds since start()
# bltcp.timestamp_mode = TimestampMode.MICROS

# Mode 3: Wall-clock microseconds (Unix epoch)
bltcp.timestamp_mode = TimestampMode.UNIX

# ── Interval & start ─────────────────────────────────────────────────

bltcp.interval_ms = 500
bltcp.start()
print(f"Timestamp mode : {bltcp.timestamp_mode.name}")
print(f"Interval       : {bltcp.interval_ms} ms")
print("##LOGGBOK:READY##")

while True:
    t = time.time()
    bltcp.signals[0].value = math.sin(t)
    bltcp.signals[1].value = math.cos(t)

    # The timed_write methods use the auto-timestamp from the chosen mode.
    # To supply your own timestamp instead, pass unix_timestamp explicitly:
    #   bltcp.timed_write_all_data(unix_timestamp=time.time())
    bltcp.tick()
