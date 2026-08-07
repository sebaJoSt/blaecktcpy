"""Waveform Generator — a dashboard-friendly demo of typed commands.

A Python port of the Arduino ``WaveformGeneratorEthernet`` sketch. It generates
one fully controllable waveform whose frequency, amplitude, offset, shape and
on/off state are all set over typed commands. The commands are registered with
the typed helpers (``on_number_command`` / ``on_select_command`` /
``on_switch_command`` / ``on_button_command`` / ``on_text_command``) so the
device is self-describing: it advertises range, unit, options and the mirrored
signal in a 0xE0 "Command List" frame, which Loggbok turns into Home Assistant
MQTT Discovery entities. Out-of-range values are rejected by the library (and a
0xF0 ack reports the outcome); each accepted value is written back to its
signal, so a dashboard always shows the value the device actually applied.

A read-only string signal (``WaveName``) mirrors the selected shape as
human-readable text, and a writable free-text command (``SET_LABEL`` ->
``DeviceLabel``) shows a Home Assistant text entity round-tripping an arbitrary
string.

--- COMMANDS ---
    SET_FREQ    <0..2>      frequency [Hz]            -> Frequency  (HA number, step 0.01)
    SET_AMP     <0..100>    peak amplitude            -> Amplitude  (HA number, step 0.1)
    SET_OFFSET  <-100..100> DC offset                 -> Offset     (HA number, step 0.1)
    SET_WAVE    <0..3>      Sine/Square/Triangle/Saw  -> Waveform   (HA select; name or index)
    SET_ENABLE  <0|1>       output on/off             -> Enabled    (HA switch)
    SET_LABEL   <text>      free-text device label    -> DeviceLabel (HA text, max 32)
    STATUS                  push an on-demand status line (HA button)

--- STATUS MESSAGES (0x90 frame -> HA text sensors, not logged) ---
    "status"           periodic 10 s heartbeat  (live)
    "status_ondemand"  pushed on the STATUS button press

Run this, then connect Loggbok to 127.0.0.1:23. A status page is available at
http://127.0.0.1:8080.
"""

import math
import time

from blaecktcpy import BlaeckTCPy, TimestampMode

EXAMPLE_VERSION = "1.0"

# Human-readable shape names for the WaveName string signal (-> HA text sensor).
# Single source of truth; keep in sync with the SET_WAVE options CSV below.
WAVE_NAMES = ["Sine", "Square", "Triangle", "Sawtooth"]

bltcp = BlaeckTCPy(
    ip="127.0.0.1",
    port=23,
    device_name="Waveform Generator Demo",
    device_hw_version="Python Script",
    device_fw_version=EXAMPLE_VERSION,
)

# --- Signals (fixed set -> safe to control while logging) ---
bltcp.add_signal("Output", "float")
bltcp.add_signal("Frequency", "float", 1.0)  # [Hz]
bltcp.add_signal("Amplitude", "float", 1.0)
bltcp.add_signal("Offset", "float", 0.0)
bltcp.add_signal("Waveform", "byte", 0)  # 0=Sine 1=Square 2=Triangle 3=Sawtooth
bltcp.add_signal("Enabled", "bool", True)
bltcp.add_signal("WaveName", "string", WAVE_NAMES[0])  # mirrors Waveform as text
bltcp.add_signal("DeviceLabel", "string", "wave-gen")  # free-text label (HA text)


# --- Typed command handlers ---
# Each accepted value is written back to its mirrored signal so the dashboard
# always reflects the value the device actually applied. The library validates
# the value (range / options / length) before the handler runs.


@bltcp.on_number_command(
    "SET_FREQ", state_signal="Frequency", min_value=0.0, max_value=2.0, step=0.01, unit="Hz"
)
def on_set_freq(value: str):
    bltcp.signals["Frequency"].value = round(float(value), 4)


@bltcp.on_number_command(
    "SET_AMP", state_signal="Amplitude", min_value=0.0, max_value=100.0, step=0.1
)
def on_set_amp(value: str):
    bltcp.signals["Amplitude"].value = round(float(value), 4)


@bltcp.on_number_command(
    "SET_OFFSET", state_signal="Offset", min_value=-100.0, max_value=100.0, step=0.1
)
def on_set_offset(value: str):
    bltcp.signals["Offset"].value = round(float(value), 4)


@bltcp.on_select_command(
    "SET_WAVE", state_signal="Waveform", options="Sine,Square,Triangle,Sawtooth"
)
def on_set_wave(value: str):
    # The library normalizes a name or index to the option's index string.
    idx = int(value)
    bltcp.signals["Waveform"].value = idx
    bltcp.signals["WaveName"].value = WAVE_NAMES[idx]


@bltcp.on_switch_command("SET_ENABLE", state_signal="Enabled")
def on_set_enable(value: str):
    bltcp.signals["Enabled"].value = value == "1"


@bltcp.on_text_command("SET_LABEL", state_signal="DeviceLabel", max_length=32)
def on_set_label(value: str):
    # value arrives already percent-decoded.
    bltcp.signals["DeviceLabel"].value = value


def _status_line() -> str:
    """Human-readable one-line status, shared by the heartbeat and STATUS button."""
    state = "running" if bool(bltcp.signals["Enabled"].value) else "stopped"
    shape = bltcp.signals["WaveName"].value
    freq = float(bltcp.signals["Frequency"].value)
    return f"{state} {shape} @ {freq:.2f} Hz"


@bltcp.on_button_command("STATUS")
def on_status(*_params: str):
    line = _status_line()
    print(f"[STATUS] {line}")
    # On-demand: push to a dedicated channel so its HA text sensor updates only
    # on button press (independent of the periodic "status" heartbeat below).
    bltcp.write_message("status_ondemand", line)


bltcp.timestamp_mode = TimestampMode.UNIX
bltcp.start()
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

# Waveform generation state
phase = 0.0  # normalized phase 0..1
last = time.time()
last_status = 0.0  # timestamp of last periodic "status" heartbeat

while True:
    now = time.time()
    dt = now - last
    last = now

    freq = float(bltcp.signals["Frequency"].value)
    amp = float(bltcp.signals["Amplitude"].value)
    offset = float(bltcp.signals["Offset"].value)
    wave = int(bltcp.signals["Waveform"].value)
    enabled = bool(bltcp.signals["Enabled"].value)

    if not enabled:
        bltcp.signals["Output"].value = offset
    else:
        phase = (phase + freq * dt) % 1.0
        if wave == 1:  # Square
            w = 1.0 if phase < 0.5 else -1.0
        elif wave == 2:  # Triangle: +1 at phase 0, -1 at phase 0.5
            w = 1.0 - 4.0 * abs(phase - 0.5)
        elif wave == 3:  # Sawtooth: -1 .. +1 ramp
            w = 2.0 * phase - 1.0
        else:  # Sine
            w = math.sin(2.0 * math.pi * phase)
        bltcp.signals["Output"].value = offset + amp * w

    # Periodic 10 s heartbeat on the "status" channel (live HA text sensor),
    # independent of the on-demand "status_ondemand" STATUS button push.
    if now - last_status >= 10.0:
        last_status = now
        bltcp.write_message("status", _status_line())

    bltcp.tick()
    time.sleep(0.001)  # Prevent busy loop; reduce or remove for faster response
