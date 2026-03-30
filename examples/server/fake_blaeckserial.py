"""
Fake BlaeckSerial — emulates an Arduino with I2C master + 2 slaves.

Speaks B3 (devices) and B1 (data) protocol like real BlaeckSerial,
so the hub treats it as an older device with slave structure.

Device tree when connected through a hub:

  Hub (hub, master)
  └─ ArduinoMega (server, master of BlaeckSerial)
     ├─ TempSensor (server, I2C slave 8)
     └─ PressureSensor (server, I2C slave 42)

Signals:
  Master:   MasterVoltage (float)
  Slave 8:  Temperature (float), Humidity (float)
  Slave 42: Pressure (float)

Usage:
  Terminal 1: python examples/server/fake_blaeckserial.py  (port 10026)
  Terminal 2: python examples/hub/treeview_test.py         (port 10023, add this upstream)
  Then connect Loggbok to 127.0.0.1:10023
"""

import binascii
import math
import selectors
import socket
import struct
import time

HOST = "127.0.0.1"
PORT = 10026

# --- Signal definitions: (name, dtype_code, msc, slave_id) ---
# dtype 8 = float (4 bytes)
SIGNALS = [
    ("MasterVoltage", 8, 1, 0),      # Master (MSC=1, SlaveID=0)
    ("Temperature", 8, 2, 8),         # I2C Slave 8
    ("Humidity", 8, 2, 8),            # I2C Slave 8
    ("Pressure", 8, 2, 42),           # I2C Slave 42
]

# --- Device definitions: (name, hw, fw, lib_ver, lib_name, msc, slave_id) ---
DEVICES = [
    ("ArduinoMega", "Mega2560", "1.0", "3.1.0", "blaeckserial", 1, 0),
    ("TempSensor", "BME280", "1.0", "3.1.0", "blaeckserial", 2, 8),
    ("PressureSensor", "BMP390", "1.0", "3.1.0", "blaeckserial", 2, 42),
]


def _wrap(frame: bytes) -> bytes:
    """Wrap a frame in <BLAECK:.../BLAECK>\\r\\n markers."""
    return b"<BLAECK:" + frame + b"/BLAECK>\r\n"


def build_symbol_list(msg_id: int) -> bytes:
    """Build B0 symbol list frame."""
    key = b"\xb0"
    mid = msg_id.to_bytes(4, "little")
    payload = b""
    for name, dtype, msc, sid in SIGNALS:
        payload += bytes([msc, sid]) + name.encode() + b"\x00" + bytes([dtype])
    return _wrap(key + b":" + mid + b":" + payload)


def build_devices(msg_id: int) -> bytes:
    """Build B3 device info frame (BlaeckSerial format)."""
    key = b"\xb3"
    mid = msg_id.to_bytes(4, "little")
    payload = b""
    for name, hw, fw, lib_ver, lib_name, msc, sid in DEVICES:
        payload += (
            bytes([msc, sid])
            + name.encode() + b"\x00"
            + hw.encode() + b"\x00"
            + fw.encode() + b"\x00"
            + lib_ver.encode() + b"\x00"
            + lib_name.encode() + b"\x00"
        )
    return _wrap(key + b":" + mid + b":" + payload)


def build_data(msg_id: int, values: list[float]) -> bytes:
    """Build B1 data frame (legacy sequential format)."""
    key = b"\xb1"
    mid = msg_id.to_bytes(4, "little")
    payload = b""
    for val in values:
        payload += struct.pack("<f", val)
    status = b"\x00"
    crc_input = key + b":" + mid + b":" + payload
    crc = binascii.crc32(crc_input).to_bytes(4, "little")
    return _wrap(key + b":" + mid + b":" + payload + status + crc)


def parse_command(text: str) -> tuple[str, int]:
    """Parse '<COMMAND,param>' → (command, msg_id)."""
    text = text.strip("<>")
    parts = text.split(",")
    command = parts[0].strip()
    msg_id = 1
    if len(parts) > 1:
        try:
            raw = parts[1].strip()
            msg_id = int.from_bytes(bytes.fromhex(raw), "little") if len(raw) == 8 else int(raw)
        except (ValueError, IndexError):
            pass
    return command, msg_id


# --- TCP Server ---
server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_sock.bind((HOST, PORT))
server_sock.listen(2)
server_sock.setblocking(False)

sel = selectors.DefaultSelector()
sel.register(server_sock, selectors.EVENT_READ)

clients: dict[socket.socket, str] = {}
timed_active: dict[socket.socket, tuple[bool, int, float]] = {}

print(f"Fake BlaeckSerial listening on {HOST}:{PORT}")
print(f"  Master: ArduinoMega (MSC=1, SlaveID=0)")
print(f"  Slave:  TempSensor (MSC=2, SlaveID=8)")
print(f"  Slave:  PressureSensor (MSC=2, SlaveID=42)")

start_time = time.time()

while True:
    events = sel.select(timeout=0.01)
    now = time.time()
    t = (now - start_time) * 1000

    # Generate signal values
    values = [
        3.3 + math.sin(t * 0.0005) * 0.1,    # MasterVoltage
        22.0 + math.sin(t * 0.001) * 5.0,     # Temperature
        45.0 + math.cos(t * 0.0008) * 10.0,   # Humidity
        1013.25 + math.sin(t * 0.0003) * 5.0,  # Pressure
    ]

    for key, mask in events:
        if key.fileobj is server_sock:
            conn, addr = server_sock.accept()
            conn.setblocking(False)
            sel.register(conn, selectors.EVENT_READ)
            clients[conn] = ""
            print(f"Client connected: {addr}")
        else:
            conn = key.fileobj
            try:
                data = conn.recv(4096)
                if not data:
                    raise ConnectionResetError
                clients[conn] += data.decode("utf-8", errors="replace")

                # Process complete commands
                buf = clients[conn]
                while True:
                    s = buf.find("<")
                    if s == -1:
                        buf = ""
                        break
                    e = buf.find(">", s)
                    if e == -1:
                        buf = buf[s:]
                        break
                    msg = buf[s:e + 1]
                    buf = buf[e + 1:]
                    command, msg_id = parse_command(msg)

                    if command == "BLAECK.WRITE_SYMBOLS":
                        conn.sendall(build_symbol_list(msg_id))
                    elif command == "BLAECK.GET_DEVICES":
                        conn.sendall(build_devices(msg_id))
                    elif command == "BLAECK.WRITE_DATA":
                        conn.sendall(build_data(msg_id, values))
                    elif command == "BLAECK.ACTIVATE":
                        timed_active[conn] = (True, msg_id, now)
                        print(f"  ACTIVATE (msg_id={msg_id})")
                    elif command == "BLAECK.DEACTIVATE":
                        timed_active.pop(conn, None)
                        print(f"  DEACTIVATE")

                clients[conn] = buf

            except (ConnectionResetError, OSError):
                print(f"Client disconnected")
                sel.unregister(conn)
                conn.close()
                clients.pop(conn, None)
                timed_active.pop(conn, None)

    # Send timed data to activated clients
    for conn, (active, msg_id, last_sent) in list(timed_active.items()):
        if active and now - last_sent >= 0.3:
            try:
                conn.sendall(build_data(msg_id, values))
                timed_active[conn] = (True, msg_id, now)
            except OSError:
                timed_active.pop(conn, None)
