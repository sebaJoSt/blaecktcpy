"""
BlaeckHub Example: Stress Board with SCPI Power Supply

Combines a BlaeckTCP microcontroller (stress board) with an
Ethernet power supply that speaks SCPI over TCP. The hub polls
the PSU in a background thread and exposes its readings as
local signals alongside the microcontroller's own signals.

  ┌──────────────────┐   ┌──────────────────┐
  │ Stress Board :24 │   │   PSU (SCPI)     │
  │   (BlaeckTCP)    │   │  192.168.1.20    │
  └────────┬─────────┘   └────────┬─────────┘
           │                      │
           ▼                      ▼
  ┌─────────────────────────────────────────┐
  │            BlaeckHub :23                │
  │  PSU_Voltage, PSU_Current (local)       │
  └───────────────────┬─────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │   Loggbok    │
              └──────────────┘

Loggbok sees the stress board signals (relayed) plus PSU
voltage and current (polled via SCPI) in one unified view.

Setup:
  1. Adjust IPs, ports, and SCPI commands for your hardware
  2. Run:  python examples/hub/stress_board.py
  3. Connect Loggbok to 127.0.0.1:23
"""

import socket
import threading
import time

from blaecktcpy import BlaeckHub

EXAMPLE_VERSION = "1.0"

# -- Configuration --
HUB_IP = "127.0.0.1"
HUB_PORT = 23

BOARD_IP = "192.168.1.10"
BOARD_PORT = 24

PSU_IP = "192.168.1.20"
PSU_PORT = 5025  # standard SCPI port
PSU_POLL_INTERVAL = 0.5  # seconds

# -- Hub setup --
hub = BlaeckHub(HUB_IP, HUB_PORT, "Stress Board Hub", "Python Script", EXAMPLE_VERSION)

# Relay all microcontroller signals to Loggbok
hub.add_tcp(BOARD_IP, BOARD_PORT, "StressBoard", interval_ms=500)

# Local signals for PSU readings
psu_voltage = hub.add_signal("PSU_Voltage", "float")
psu_current = hub.add_signal("PSU_Current", "float")


# -- SCPI helper --
def scpi_query(sock, command):
    """Send a SCPI query and return the response as a float."""
    sock.sendall((command + "\n").encode())
    return float(sock.recv(256).decode().strip())


# -- Background PSU polling --
def poll_psu():
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect((PSU_IP, PSU_PORT))
                while True:
                    psu_voltage.value = scpi_query(s, "MEAS:VOLT?")
                    psu_current.value = scpi_query(s, "MEAS:CURR?")
                    time.sleep(PSU_POLL_INTERVAL)
        except (OSError, ValueError) as e:
            print(f"PSU connection error: {e} — retrying in 5s")
            psu_voltage.value = 0.0
            psu_current.value = 0.0
            time.sleep(5)


threading.Thread(target=poll_psu, daemon=True).start()

# -- Run --
hub.start()
print("##LOGGBOK:READY##")

while True:
    hub.tick()
