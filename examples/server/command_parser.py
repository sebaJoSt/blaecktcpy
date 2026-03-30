from blaecktcpy import BlaeckServer

EXAMPLE_VERSION = "1.0"

ip = "127.0.0.1"
port = 23

bltcp = BlaeckServer(ip, port, "Command Parser Example", "Python Script", EXAMPLE_VERSION)

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


while True:
    bltcp.tick()
