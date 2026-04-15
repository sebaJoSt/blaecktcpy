"""Command Parser — custom command handling with @bltcp.on_command().

Run this, then connect with telnet or netcat to send commands:
    telnet 127.0.0.1 23
    <SET_LED,1>
    <MOTOR,255,forward>
"""

import time

from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

ip = "127.0.0.1"
port = 23

bltcp = BlaeckTCPy(
            ip=ip,
            port=port,
            device_name="Command Parser Example",
            device_hw_version="Python Script",
            device_fw_version=EXAMPLE_VERSION,
        )

bltcp.add_signal("LED_State", "bool")
bltcp.add_signal("Motor_Speed", "float")


@bltcp.on_command("SET_LED")
def handle_led(state):
    bltcp.signals[0].value = int(state)
    print(f"LED set to {state}")


@bltcp.on_command("MOTOR")
def handle_motor(speed, direction):
    bltcp.signals[1].value = float(speed)
    print(f"Motor: speed={speed}, direction={direction}")


# Catch-all — fires for every command (built-in and custom)
@bltcp.on_command()
def log_all(command, *params):
    print(f"[LOG] {command} {params}")


bltcp.start()
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

while True:
    bltcp.tick()
    time.sleep(0.001)  # Prevent busy loop; reduce or remove if faster response is needed
