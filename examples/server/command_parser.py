from blaecktcpy import BlaeckTCPy

EXAMPLE_VERSION = "1.0"

ip = "127.0.0.1"
port = 23

bltcp = BlaeckTCPy(ip, port, "Command Parser Example", "Python Script", EXAMPLE_VERSION)

bltcp.add_signal("LED_State", "bool")
bltcp.add_signal("Motor_Speed", "float")


# Handle a specific command — parameters are unpacked as positional string args
@bltcp.on("SET_LED")
def handle_led(state):
    led_value = int(state)
    bltcp.signals[0].value = led_value
    print(f"LED set to {led_value}")


# Multiple parameters
@bltcp.on("MOTOR")
def handle_motor(speed, direction):
    bltcp.signals[1].value = float(speed)
    print(f"Motor: speed={speed}, direction={direction}")


# Hook into a built-in command — fires AFTER the protocol handles it internally
@bltcp.on("BLAECK.ACTIVATE")
def on_activated(*params):
    print("Client started timed data transmission")


# Exclude debug clients from receiving data — only first client gets BlaeckTCP data
@bltcp.on_client_connected
def on_connect(client_no):
    if client_no > 0:
        bltcp.data_clients.discard(client_no)
        print(f"Client #{client_no} excluded from data (debug mode)")


# Catch-all — fires for every message (built-in and custom)
@bltcp.on_read
def log_all(command, *params):
    print(f"[LOG] {command} {params}")


print("##LOGGBOK:READY##")
print("Send commands like: <SET_LED,1>  <MOTOR,255,forward>")

while True:
    bltcp.tick()
