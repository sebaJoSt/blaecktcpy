# pyright: reportUnusedCallResult=false
"""Command Parser — custom command handling and client connection callbacks.

Run this, then connect with telnet or netcat to send commands:
    telnet 127.0.0.1 23
    <SET_LED,1>
    <MOTOR,255,forward>
"""

import time

from blaecktcpy import BlaeckTCPy

ip = "127.0.0.1"
port = 23

bltcp = BlaeckTCPy(
    ip=ip,
    port=port,
    device_name="Command Parser Example",
)

bltcp.add_signal("LED_State", "bool")
bltcp.add_signal("Motor_Speed", "float")


# -- Client connection callbacks --
# By default all clients receive data frames. Use on_client_connected
# to control which clients get data (e.g. only the first client):


@bltcp.on_client_connected()
def on_connect(client_id: int):
    if client_id > 0:
        bltcp.data_clients.discard(client_id)
        print(f"Client #{client_id} connected (data excluded)")
    else:
        print(f"Client #{client_id} connected (data included)")


@bltcp.on_client_disconnected()
def on_disconnect(client_id: int):
    print(f"Client #{client_id} disconnected")


# -- Custom command handlers --


@bltcp.on_command("SET_LED")
def handle_led(state: str):
    bltcp.signals[0].value = int(state)
    print(f"LED set to {state}")


@bltcp.on_command("MOTOR")
def handle_motor(speed: str, direction: str):
    bltcp.signals[1].value = float(speed)
    print(f"Motor: speed={speed}, direction={direction}")


# Catch-all — fires for every command (built-in and custom)
@bltcp.on_command()
def log_all(command: str, *params: str):
    print(f"[LOG] {command} {params}")


bltcp.start()
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove

while True:
    bltcp.tick()
    time.sleep(
        0.001
    )  # Prevent busy loop; reduce or remove if faster response is needed
