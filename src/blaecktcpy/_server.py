"""BlaeckServer — BlaeckTCP Protocol Implementation (TCP Server Mode)."""

import atexit
import binascii
import logging
import selectors
import signal
import socket
import sys
import time
from typing import Union

from ._signal import Signal

__all__ = ["BlaeckServer"]

from importlib.metadata import version as _pkg_version

LIB_VERSION = _pkg_version("blaecktcpy")
LIB_NAME = "blaecktcpy"

_MAX_RECV_BUFFER = 65536  # 64 KB per-client receive buffer limit

# Status byte values for data frames
# 0x00: normal, 0x01: I2C CRC error (BlaeckSerial)
STATUS_OK = 0x00
STATUS_UPSTREAM_LOST = 0x02

logger = logging.getLogger("blaecktcpy")


class _IntervalTimer:
    """Reusable interval timer with first-tick initialization."""

    __slots__ = ("_interval_ms", "_base_ns", "_setpoint_ms", "_first_tick")

    def __init__(self):
        self._interval_ms: int = 0
        self._base_ns: int = 0
        self._setpoint_ms: float = 0
        self._first_tick: bool = False

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def activate(self, interval_ms: int) -> None:
        """Start the timer with the given interval."""
        self._interval_ms = interval_ms
        self._first_tick = True

    def deactivate(self) -> None:
        """Stop the timer."""
        self._interval_ms = 0
        self._first_tick = False

    def elapsed(self) -> bool:
        """Return True if the interval has elapsed. Advances setpoint on True."""
        now = time.time_ns()
        if self._first_tick:
            self._base_ns = now
            self._setpoint_ms = self._interval_ms
            self._first_tick = False
            return True
        elapsed_ms = (now - self._base_ns) / 1_000_000
        if elapsed_ms < self._setpoint_ms:
            return False
        self._setpoint_ms += self._interval_ms
        return True


class BlaeckServer:
    """blaecktcpy — BlaeckTCP Protocol Implementation (TCP Server Mode)"""

    # Message type keys (pre-computed bytes for wire encoding)
    MSG_SYMBOL_LIST = b"\xb0"
    MSG_DATA = b"\xd1"
    MSG_DEVICES = b"\xb6"

    def __init__(
        self,
        ip: str,
        port: int,
        device_name: str,
        device_hw_version: str,
        device_fw_version: str,
    ):
        """
        Initialize BlaeckServer.

        Args:
            ip: IP address to bind to (e.g. '127.0.0.1' = localhost)
            port: TCP port to listen on
            device_name: Name of the device
            device_hw_version: Hardware version string
            device_fw_version: Firmware version string
        """
        self._ip = ip
        self._port = port

        self._init_device_info(device_name, device_hw_version, device_fw_version)
        self._init_socket()

        try:
            self._bind_socket(ip, port)
        except OSError:
            self._server_socket.close()
            if not self._stdin_is_interactive():
                raise OSError(f"Port {port} is already in use")
            alt_port = self._find_free_port(ip, port)
            print(
                f"\033[33m[WARNING]\033[0m Something is already running on port {port}."
            )
            answer = input(
                f"Would you like to run blaecktcpy on port {alt_port} instead? \033[1m(Y/n)\033[0m "
            ).strip()
            if answer.lower() in ("", "y", "yes"):
                port = alt_port
                self._init_socket()
                self._bind_socket(ip, port)
            else:
                raise OSError(f"Port {port} is already in use")

        self._port = port

        self._start_listening()
        self._init_protocol()
        self._install_signal_handler()

        print(
            f"\033[32mblaecktcpy v{LIB_VERSION}\033[0m — Listening on \033[36m{ip}:{port}\033[0m"
        )
        atexit.register(self.close)

    def _init_device_info(self, name, hw, fw):
        """Step 1: Initialize device information"""
        self.signals = []
        self._device_name = name.encode()
        self._device_hw_version = hw.encode()
        self._device_fw_version = fw.encode()

    def _init_socket(self):
        """Step 2: Create TCP socket"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            self._server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        else:
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def _bind_socket(self, ip, port):
        """Step 3: Bind socket to address"""
        self._server_socket.bind((ip, port))

    @staticmethod
    def _find_free_port(ip: str, starting_port: int) -> int:
        """Find the next available port starting from starting_port + 1."""
        for port in range(starting_port + 1, 65536):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((ip, port))
                    return port
                except OSError:
                    continue
        raise OSError("No free port found")

    @staticmethod
    def _stdin_is_interactive() -> bool:
        """Return True when stdin is attached to an interactive terminal."""
        stdin = sys.stdin
        return bool(stdin and hasattr(stdin, "isatty") and stdin.isatty())

    def _start_listening(self):
        """Step 4: Start listening for connections"""
        self._server_socket.setblocking(False)
        self._server_socket.listen()
        self._clients = {}  # client_id → socket
        self._next_client_id = 0
        self._commanding_client = None
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._server_socket, selectors.EVENT_READ)

    def _init_protocol(self):
        """Step 5: Initialize protocol state"""
        self._timed_activated = False
        self._timer = _IntervalTimer()
        self._master_slave_config = bytes.fromhex("00")
        self._slave_id = bytes.fromhex("00")
        self._command_handlers = {}
        self._read_callback = None
        self._connect_callback = None
        self._disconnect_callback = None
        self._before_write_callback = None
        self._server_restarted = True
        self._restart_flag_pending = True
        self.data_clients = set()
        self._recv_buffers = {}
        self._closed = False

    def _install_signal_handler(self):
        """Install SIGINT handler for clean shutdown."""
        self._original_sigint = signal.getsignal(signal.SIGINT)

        def _handler(signum, frame):
            self.close()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _handler)

    # ========================================================================
    # Signal Management
    # ========================================================================
    def add_signal(
        self,
        signal_or_name: Union[Signal, str],
        datatype: str = "",
        value: Union[int, float] = 0,
    ) -> Signal:
        """Add a signal to the signal list.

        Can be called with a Signal object or with individual arguments::

            bltcp.add_signal(Signal('temp', 'float', 0.0))
            bltcp.add_signal('temp', 'float', 0.0)         # shorthand

        Returns the added Signal.
        """
        if isinstance(signal_or_name, Signal):
            sig = signal_or_name
        elif isinstance(signal_or_name, str):
            sig = Signal(signal_or_name, datatype, value)
        else:
            raise TypeError(f"Expected Signal or str, got {type(signal_or_name)}")
        self.signals.append(sig)
        return sig

    def add_signals(self, signals) -> None:
        """Add multiple signals at once.

        Accepts any iterable of Signal objects::

            bltcp.add_signals([
                Signal('temp', 'float', 0.0),
                Signal('led',  'bool',  False),
            ])
        """
        for sig in signals:
            self.add_signal(sig)

    def delete_signals(self) -> None:
        """Clear all signals."""
        self.signals = []

    def _resolve_signal(self, key: Union[str, int]) -> int:
        """Resolve a signal name or index to a valid index."""
        if isinstance(key, int):
            if 0 <= key < len(self.signals):
                return key
            raise IndexError(f"Signal index {key} out of range")
        for i, sig in enumerate(self.signals):
            if sig.signal_name == key:
                return i
        raise KeyError(f"Signal '{key}' not found")

    def write(
        self, key: Union[str, int], value: Union[int, float], *, msg_id: int = 1
    ) -> None:
        """Update a single signal's value and immediately send it.

        Args:
            key: Signal name (str) or index (int)
            value: New value to set
            msg_id: Message ID for the protocol frame
        """
        idx = self._resolve_signal(key)
        self.signals[idx].value = value
        if not self.connected:
            return
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = b"<BLAECK:" + self._build_data_msg(header, idx, idx) + b"/BLAECK>\r\n"
        self._tcp_send_data(data)

    def update(self, key: Union[str, int], value: Union[int, float]) -> None:
        """Update a signal's value and mark it as updated (no send).

        Args:
            key: Signal name (str) or index (int)
            value: New value to set
        """
        idx = self._resolve_signal(key)
        self.signals[idx].value = value
        self.signals[idx].updated = True

    def mark_signal_updated(self, key: Union[str, int]) -> None:
        """Mark a signal as updated without changing its value."""
        idx = self._resolve_signal(key)
        self.signals[idx].updated = True

    def mark_all_signals_updated(self) -> None:
        """Mark all signals as updated."""
        for sig in self.signals:
            sig.updated = True

    def clear_all_update_flags(self) -> None:
        """Clear the updated flag on all signals."""
        for sig in self.signals:
            sig.updated = False

    @property
    def has_updated_signals(self) -> bool:
        """True if any signal is marked as updated."""
        return any(sig.updated for sig in self.signals)

    # ========================================================================
    # Connection Management
    # ========================================================================
    @property
    def connected(self) -> bool:
        """Check if any client is connected"""
        return len(self._clients) > 0

    @property
    def timed_activated(self) -> bool:
        """Check if timed data transmission is active"""
        return self._timed_activated

    @property
    def commanding_client(self):
        """The client socket that sent the most recent command, or None."""
        return self._commanding_client

    def _accept_new_clients(self):
        """Accept all pending new connections."""
        while True:
            try:
                conn, addr = self._server_socket.accept()
                conn.setblocking(False)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sel.register(conn, selectors.EVENT_READ)
                client_id = self._next_client_id
                self._next_client_id += 1
                self._clients[client_id] = conn
                self._recv_buffers[conn] = ""
                self.data_clients.add(client_id)
                logger.info(f"Client #{client_id} connected: {addr[0]}:{addr[1]}")
                if self._connect_callback is not None:
                    self._connect_callback(client_id)
            except (BlockingIOError, OSError):
                break

    def _client_id_for(self, conn) -> int:
        """Find the client ID for a given socket, or -1 if not found."""
        for cid, c in self._clients.items():
            if c is conn:
                return cid
        return -1

    def _disconnect_client(self, conn):
        """Remove and close a client connection."""
        client_id = self._client_id_for(conn)
        try:
            self._sel.unregister(conn)
        except Exception:
            pass
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        if client_id >= 0:
            self._clients.pop(client_id, None)
            self.data_clients.discard(client_id)
        self._recv_buffers.pop(conn, None)
        if self._commanding_client is conn:
            self._commanding_client = None
        logger.info(f"Client #{client_id if client_id >= 0 else '?'} disconnected")
        if client_id >= 0 and self._disconnect_callback is not None:
            self._disconnect_callback(client_id)
        if not self._clients:
            self._timed_activated = False

    def _tcp_read(self) -> list:
        """Non-blocking TCP read; returns list of (command, params, conn) tuples."""
        messages = []

        events = self._sel.select(timeout=0)
        for key, _ in events:
            if key.fileobj is self._server_socket:
                self._accept_new_clients()
            else:
                conn = key.fileobj
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        self._disconnect_client(conn)
                        continue

                    self._recv_buffers[conn] = self._recv_buffers.get(
                        conn, ""
                    ) + chunk.decode("utf-8", errors="ignore")

                    logger.debug(f"_tcp_read raw chunk: {chunk!r}")

                    if len(self._recv_buffers[conn]) > _MAX_RECV_BUFFER:
                        logger.warning("Receive buffer overflow — dropping client")
                        self._disconnect_client(conn)
                        continue

                    # Extract all complete <...> messages
                    buf = self._recv_buffers[conn]
                    while True:
                        start = buf.find("<")
                        if start == -1:
                            buf = ""
                            break
                        end = buf.find(">", start)
                        if end == -1:
                            buf = buf[start:]  # Keep from '<' onward
                            break
                        content = buf[start + 1 : end]
                        buf = buf[end + 1 :]

                        parts = content.split(",")
                        command = parts[0].strip()
                        params = (
                            [p.strip() for p in parts[1:]] if len(parts) > 1 else []
                        )
                        messages.append((command, params, conn))

                    self._recv_buffers[conn] = buf

                except BlockingIOError:
                    pass
                except OSError as e:
                    logger.debug(f"Read error: {e}")
                    self._disconnect_client(conn)

        return messages

    def _tcp_send(self, data: bytes) -> bool:
        """Broadcast data to all connected clients."""
        if not self._clients:
            return False

        sent = False
        for conn in list(self._clients.values()):
            try:
                conn.sendall(data)
                sent = True
            except OSError as e:
                logger.debug(f"Send error: {e}")
                self._disconnect_client(conn)

        return sent

    def _tcp_send_data(self, data: bytes) -> bool:
        """Send data only to clients in data_clients set."""
        if not self._clients:
            return False

        sent = False
        for client_id, conn in list(self._clients.items()):
            if client_id not in self.data_clients:
                continue
            try:
                conn.sendall(data)
                sent = True
            except OSError as e:
                logger.debug(f"Send error: {e}")
                self._disconnect_client(conn)

        return sent

    # ========================================================================
    # Command Parser
    # ========================================================================
    def on_command(self, command: str | None = None):
        """Decorator to register a command handler.

        With a command name, registers a handler for that specific command.
        Parameters are unpacked as positional string arguments.

        Without a command name, registers a catch-all that fires for every
        message after built-in and specific handlers.  Receives the command
        name as the first argument followed by parameters.

        Example::

            @bltcp.on_command("SET_LED")
            def handle_led(state):
                print(f"LED = {state}")

            @bltcp.on_command()
            def log_all(command, *params):
                print(f"{command}: {params}")
        """

        def decorator(func):
            if command is None:
                self._read_callback = func
            else:
                self._command_handlers[command] = func
            return func

        return decorator

    def on_client_connected(self):
        """Decorator to register a callback when a new client connects.

        Receives the client ID. By default all clients receive data.
        Remove a client_id from data_clients to exclude it.

        Example::

            @bltcp.on_client_connected()
            def on_connect(client_id):
                if client_id > 0:
                    bltcp.data_clients.discard(client_id)
        """

        def decorator(func):
            self._connect_callback = func
            return func

        return decorator

    def on_client_disconnected(self):
        """Decorator to register a callback when a client disconnects.

        Receives the client ID that was disconnected.

        Example::

            @bltcp.on_client_disconnected()
            def on_disconnect(client_id):
                print(f"Client #{client_id} left")
        """

        def decorator(func):
            self._disconnect_callback = func
            return func

        return decorator

    def on_before_write(self):
        """Decorator to register a callback that fires before data is written.

        Use this to update signal values right before they are transmitted.

        Example::

            @bltcp.on_before_write()
            def refresh_signals():
                bltcp.signals[0].value = read_sensor()
        """

        def decorator(func):
            self._before_write_callback = func
            return func

        return decorator

    # ========================================================================
    # Protocol Message Parsing
    # ========================================================================

    @staticmethod
    def _decode_four_byte(params: list) -> int:
        """Decode up to 4 parameter bytes into a little-endian integer."""
        result = 0
        for i, part in enumerate(params[:4]):
            try:
                result += int(part) << (i * 8)
            except ValueError:
                pass
        return result

    # ========================================================================
    # Message Handlers
    # ========================================================================
    def read(self) -> None:
        """Read and process all pending messages."""
        messages = self._tcp_read()

        for command, params, conn in messages:
            self._commanding_client = conn

            # Handle built-in protocol commands
            if command == "BLAECK.WRITE_SYMBOLS":
                self.write_symbols(self._decode_four_byte(params))

            elif command == "BLAECK.WRITE_DATA":
                self.write_all_data(self._decode_four_byte(params))

            elif command == "BLAECK.GET_DEVICES":
                self.write_devices(self._decode_four_byte(params))

            elif command == "BLAECK.ACTIVATE":
                self.set_timed_data(True, self._decode_four_byte(params))

            elif command == "BLAECK.DEACTIVATE":
                self.set_timed_data(False)

            # Dispatch to specific command handler
            handler = self._command_handlers.get(command)
            if handler is not None:
                handler(*params)

            # Fire catch-all callback
            if self._read_callback is not None:
                self._read_callback(command, *params)

    # ========================================================================
    # Message Writers
    # ========================================================================
    def write_symbols(self, msg_id: int = 1) -> None:
        """Send symbol list to client."""
        if not self.connected:
            return

        header = self.MSG_SYMBOL_LIST + b":" + msg_id.to_bytes(4, "little") + b":"
        data = b"<BLAECK:" + header + self._get_symbols() + b"/BLAECK>\r\n"

        self._tcp_send(data)

    def write_all_data(self, msg_id: int = 1) -> None:
        """Send all signal data to data-enabled clients."""
        if not self.connected or not self.signals:
            return
        if self._before_write_callback is not None:
            self._before_write_callback()
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = b"<BLAECK:" + self._build_data_msg(header) + b"/BLAECK>\r\n"
        self._tcp_send_data(data)

    def write_updated_data(self, msg_id: int = 1, timestamp: int | None = None) -> None:
        """Send only signals marked as updated to data-enabled clients."""
        if not self.connected or not self.has_updated_signals:
            return
        if self._before_write_callback is not None:
            self._before_write_callback()
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, only_updated=True, timestamp=timestamp)
            + b"/BLAECK>\r\n"
        )
        self._tcp_send_data(data)

    def _timer_elapsed(self) -> bool:
        """Check if the timed interval has elapsed. Advances setpoint on True."""
        return self._timer.elapsed()

    def timed_write_all_data(self, msg_id: int = 185273099) -> bool:
        """Send all data if timer interval has elapsed."""
        if not (self.connected and self._timed_activated):
            return False
        if not self._timer_elapsed():
            return False
        if self._before_write_callback is not None:
            self._before_write_callback()
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = b"<BLAECK:" + self._build_data_msg(header) + b"/BLAECK>\r\n"
        return self._tcp_send_data(data)

    def timed_write_updated_data(self, msg_id: int = 185273099) -> bool:
        """Send only updated signals if timer interval has elapsed."""
        if not (self.connected and self._timed_activated):
            return False
        if not self._timer_elapsed():
            return False
        if not self.has_updated_signals:
            return False
        if self._before_write_callback is not None:
            self._before_write_callback()
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, only_updated=True)
            + b"/BLAECK>\r\n"
        )
        return self._tcp_send_data(data)

    def set_timed_data(self, activated: bool, interval_ms: int = 0) -> None:
        """Programmatically activate or deactivate timed data transmission.

        Args:
            activated: True to start, False to stop
            interval_ms: Interval in milliseconds (only used when activating)
        """
        self._timed_activated = activated
        if activated:
            self._timer.activate(interval_ms)
            logger.info(f"Timed data activated (interval: {interval_ms} ms)")
        else:
            self._timer.deactivate()
            logger.info("Timed data deactivated")

    def write_devices(self, msg_id: int = 1) -> None:
        """Send device information to each connected client."""
        if not self.connected:
            return

        header = self.MSG_DEVICES + b":" + msg_id.to_bytes(4, "little") + b":"

        for client_id, conn in list(self._clients.items()):
            device_info = (
                self._master_slave_config
                + self._slave_id
                + self._device_name
                + b"\0"
                + self._device_hw_version
                + b"\0"
                + self._device_fw_version
                + b"\0"
                + LIB_VERSION.encode()
                + b"\0"
                + LIB_NAME.encode()
                + b"\0"
                + str(client_id).encode()
                + b"\0"
                + (b"1" if client_id in self.data_clients else b"0")
                + b"\0"
                + (b"1" if self._server_restarted else b"0")
                + b"\0"
                + b"server\0"
                + b"0\0"  # parent (SlaveID 0 = self)
            )

            data = b"<BLAECK:" + header + device_info + b"/BLAECK>\r\n"

            try:
                conn.sendall(data)
            except OSError as e:
                logger.debug(f"Send error: {e}")
                self._disconnect_client(conn)

        self._server_restarted = False

    # ========================================================================
    # Main Loop
    # ========================================================================
    def tick(self, msg_id: int = 185273099) -> bool:
        """Main loop tick — read commands, send all data on timer.

        Call this repeatedly in your main loop.
        Returns True if timed data was sent.
        """
        self.read()
        return self.timed_write_all_data(msg_id) if self._timed_activated else False

    def tick_updated(self, msg_id: int = 185273099) -> bool:
        """Main loop tick — read commands, send only updated data on timer.

        Like tick() but only transmits signals marked as updated.
        Returns True if timed data was sent.
        """
        self.read()
        return self.timed_write_updated_data(msg_id) if self._timed_activated else False

    # ========================================================================
    # Internal Protocol Methods
    # ========================================================================
    def _build_data_msg(
        self,
        header: bytes,
        start: int = 0,
        end: int = -1,
        only_updated: bool = False,
        timestamp: int | None = None,
        status: int = STATUS_OK,
    ) -> bytes:
        """Build data message with CRC32 checksum (v5 format).

        Args:
            header: Pre-built header bytes (msg_key:msg_id:)
            start: First signal index (inclusive)
            end: Last signal index (inclusive), -1 = last signal
            only_updated: If True, include only signals with updated=True
            timestamp: Optional upstream timestamp (millis) to forward
            status: Status byte (STATUS_OK or STATUS_UPSTREAM_LOST)
        """
        if end == -1:
            end = len(self.signals) - 1

        # Restart flag
        restart_flag = b"\x01" if self._restart_flag_pending else b"\x00"
        self._restart_flag_pending = False

        # Timestamp
        if timestamp is not None:
            timestamp_mode = b"\x01"
            meta = (
                restart_flag
                + b":"
                + timestamp_mode
                + timestamp.to_bytes(4, "little")
                + b":"
            )
        else:
            timestamp_mode = b"\x00"
            meta = restart_flag + b":" + timestamp_mode + b":"

        payload = b""
        for idx in range(start, end + 1):
            sig = self.signals[idx]
            if only_updated and not sig.updated:
                continue
            payload += idx.to_bytes(2, "little") + sig.to_bytes()
            if only_updated:
                sig.updated = False

        crc_input = header + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")

        return crc_input + status.to_bytes(1, "little") + crc

    def _get_symbols(self) -> bytes:
        """Build symbol list message"""
        result = b""

        for sig in self.signals:
            result += (
                self._master_slave_config
                + self._slave_id
                + sig.signal_name.encode()
                + b"\0"
                + sig.get_dtype_byte()
            )

        return result

    # ========================================================================
    # Context Manager Support
    # ========================================================================
    def __enter__(self):
        """Enable 'with' statement usage"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up on exit"""
        self.close()

    def close(self):
        """Gracefully close all connections."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        signal.signal(signal.SIGINT, self._original_sigint)
        for conn in list(self._clients.values()):
            try:
                self._sel.unregister(conn)
            except Exception:
                pass
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._clients.clear()
        try:
            self._sel.unregister(self._server_socket)
        except Exception:
            pass
        self._sel.close()
        self._server_socket.close()
        logger.info("Server closed")

    def __repr__(self):
        n = len(self._clients)
        clients = f"{n} client{'s' if n != 1 else ''}"
        active = "active" if self._timed_activated else "inactive"
        return f"blaecktcpy [{clients}] [{active}] ({len(self.signals)} signals)"
