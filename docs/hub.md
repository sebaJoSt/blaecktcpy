# Hub mode

The same `BlaeckTCPy` class serves as a hub when you add upstream connections with `add_tcp()` or `add_serial()`. The hub aggregates signals from multiple upstream devices and serves them as a single merged device, alongside any local signals.

```python
from blaecktcpy import BlaeckTCPy

hub = BlaeckTCPy("0.0.0.0", 23, "My Hub", "Python", "1.0")

# Connect to upstream devices
hub.add_tcp("192.168.1.10", 24, name="ESP32")
hub.add_tcp("127.0.0.1", 25, name="Sine")

# Add a local signal
dew_point = hub.add_signal("DewPoint", "float")

hub.start()

while True:
    dew_point.value = compute_dew_point()
    hub.tick()
```

Serial upstreams are also supported (`pip install blaecktcpy[serial]`):

```python
hub.add_serial("COM3", 115200, name="Arduino")
```

## Upstream data rate

The hub sends `BLAECK.DEACTIVATE` to every upstream on connect (to ensure a clean state) and again on `close()`.

By default (`interval_ms=IntervalMode.CLIENT`), each upstream starts streaming when a downstream client (e.g. Loggbok) sends `BLAECK.ACTIVATE`. The hub forwards `ACTIVATE` and `DEACTIVATE` commands from the client to these upstreams.

Set `interval_ms` to a value ≥ 0 to start streaming at a fixed rate immediately on `start()`. Client `ACTIVATE`/`DEACTIVATE` commands are **not** forwarded to hub-managed upstreams:

```python
from blaecktcpy import IntervalMode

# Hub-managed: always stream at 500 ms, ignore client ACTIVATE/DEACTIVATE
hub.add_tcp("192.168.1.10", 24, name="ESP32", interval_ms=500)

# Client-managed (default): hub forwards ACTIVATE/DEACTIVATE from Loggbok
hub.add_tcp("127.0.0.1", 25, name="Sine")

# Disabled: no timed data from this upstream
hub.add_tcp("192.168.1.20", 24, name="Sensor", interval_ms=IntervalMode.OFF)
```

## Local signal interval

Use the `local_interval_ms` property to stream local signals at a fixed rate:

```python
hub.local_interval_ms = 500                  # local signals every 500 ms
hub.local_interval_ms = IntervalMode.CLIENT  # follow client ACTIVATE/DEACTIVATE (default)
hub.local_interval_ms = IntervalMode.OFF     # disable timed local data
```

## Relaying upstream signals

By default (`relay_downstream=True`), all upstream signals are relayed to downstream clients.

Set `relay_downstream=False` to decode upstream signals hub-side without exposing them to downstream clients. This is useful when you want to read raw values, compute derived signals, and only expose those:

```python
# Hidden: raw signals decoded hub-side but not visible to Loggbok
arduino = hub.add_tcp("192.168.1.10", 24, name="Arduino", relay_downstream=False)

# Expose a computed signal instead
dew_point = hub.add_signal("DewPoint", "float")
```

The hub can decode upstream frames using older protocol versions (`B2`–`B5` for devices, `B1`/`D1` for legacy/Arduino data) but always sends `B6`/`D2` downstream to clients.

## Schema change detection

When an upstream device changes its signals at runtime, the hub detects the schema hash mismatch and automatically re-discovers the new signal layout. This propagates through chained hubs. For older upstream devices that don't include a schema hash (D1/B1 frames), the hub falls back to signal count comparison.

## Forwarding custom commands

All custom commands from downstream clients are automatically forwarded to upstream devices — no registration needed.

```python
hub.add_tcp("192.168.1.10", 24, name="Arduino")     # accepts all commands (default)
hub.add_tcp("192.168.1.11", 25, name="Sensor",
            forward_custom_commands=False)            # accepts no commands
hub.add_tcp("192.168.1.12", 26, name="LED",
            forward_custom_commands=["SET_LED"])      # accepts only SET_LED
```

Use `forward=False` on `@on_command()` to keep a command local (never forwarded, regardless of upstream settings):

```python
# Handle locally AND forward (default)
@hub.on_command("SET_LED")
def handle_led(state):
    print(f"LED = {state}")

# Local only (never forwarded)
@hub.on_command("MOTOR", forward=False)
def handle_motor(speed):
    print(f"Motor = {speed}")
```

## Auto-reconnect

TCP upstreams can automatically reconnect after connection loss:

```python
hub.add_tcp("192.168.1.10", 24, name="Arduino", interval_ms=300, auto_reconnect=True)
```

When an upstream disconnects (detected either before or during frame reading), the hub:

1. Sends a `STATUS_UPSTREAM_LOST` (`0x80`) D2 data frame to downstream clients (signal values are zeroed in this frame)
2. Retries the TCP connection every 5 seconds (unlimited attempts)

The `StatusPayload` of the `0x80` frame includes an auto-reconnect flag
(byte 0 = `0x01`) so downstream clients can indicate that reconnection
is being attempted before a successful reconnect.

On successful reconnect, the hub re-discovers device info and:

1. Sends a `STATUS_UPSTREAM_RECONNECTED` (`0x81`) D2 data frame
2. Re-sends `BLAECK.ACTIVATE` to hub-managed upstreams
3. If the upstream device restarted, sends a `C0` restart notification frame containing the device name and version info. Downstream clients can use this to re-send `BLAECK.ACTIVATE` for client-controlled upstreams.

> **Note:** The hub always sends D2 frames downstream, even when the upstream
> device uses the older B1/D1 format. StatusByte values `0x80`+ are only
> generated by hubs — upstream devices in the `0x00`–`0x7F` range are relayed
> as-is.
