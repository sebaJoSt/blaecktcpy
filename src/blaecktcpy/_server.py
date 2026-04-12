"""BlaeckTCPy — Unified BlaeckTCP Protocol Implementation."""

import atexit
import logging
import signal
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

from . import _encoder
from ._signal import Signal, SignalList, IntervalMode, TimestampMode
from ._tcp import ClientManager
from .hub import _decoder as decoder
from .hub._manager import HubManager, _UpstreamDevice

__all__ = ["BlaeckTCPy"]

from importlib.metadata import version as _pkg_version

LIB_VERSION = _pkg_version("blaecktcpy")
LIB_NAME = "blaecktcpy"

# ANSI color helpers (only when stdout is a terminal)
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
_BOLD = "\033[1m" if _USE_COLOR else ""
_BLUE_ULINE = "\033[94;4m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""

# Re-export encoding constants for backward compatibility
STATUS_OK = _encoder.STATUS_OK
STATUS_UPSTREAM_LOST = _encoder.STATUS_UPSTREAM_LOST
STATUS_UPSTREAM_RECONNECTED = _encoder.STATUS_UPSTREAM_RECONNECTED

_MSC_MASTER = _encoder.MSC_MASTER
_MSC_SLAVE = _encoder.MSC_SLAVE

# Message IDs for data frames
_MSG_ID_ACTIVATE = 185273099  # 0x0B0B0B0B — client-controlled (BLAECK.ACTIVATE)
_MSG_ID_HUB = 185273100  # 0x0B0B0B0C — hub-overridden interval



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
        if self._interval_ms == 0:
            return True
        now = time.time_ns()
        if self._first_tick:
            self._base_ns = now
            self._setpoint_ms = self._interval_ms
            self._first_tick = False
            return True
        elapsed_ms = (now - self._base_ns) / 1_000_000
        if elapsed_ms < self._setpoint_ms:
            return False
        while self._setpoint_ms <= elapsed_ms:
            self._setpoint_ms += self._interval_ms
        return True


class BlaeckTCPy:
    """blaecktcpy — Unified BlaeckTCP Protocol Implementation.

    Works as a standalone server (no upstreams) or as a hub that
    aggregates signals from multiple upstream devices.

    Constructor stores parameters only.  Call :meth:`start` to create
    the TCP socket and begin listening.
    """

    # Message type keys (pre-computed bytes for wire encoding)
    MSG_SYMBOL_LIST = b"\xb0"
    MSG_DATA = b"\xd2"
    MSG_DEVICES = b"\xb6"

    def __init__(
        self,
        *,
        ip: str,
        port: int,
        device_name: str,
        device_hw_version: str,
        device_fw_version: str,
        log_level: int | None = logging.INFO,
        http_port: int | None = 8080,
    ):
        """
        Initialize BlaeckTCPy.

        Args:
            ip: IP address to bind to (e.g. '127.0.0.1' = localhost)
            port: TCP port to listen on
            device_name: Name of the device
            device_hw_version: Hardware version string
            device_fw_version: Firmware version string
            log_level: Logging level for this instance (e.g.
                ``logging.DEBUG``, ``logging.WARNING``).  Defaults to
                ``logging.INFO``.  Pass ``None`` to silence all output.
            http_port: Port for the HTTP status page.  A lightweight web
                server starts alongside the TCP server showing device
                info, signals, and connected clients.  If the port is
                occupied, a free port is chosen automatically.
                Defaults to ``8080``.  Pass ``None`` to disable the
                status page.
        """
        self._ip = ip
        self._port = port

        # Per-instance logger
        logger_name = device_name.replace(" ", "_") if device_name else f"{ip}_{port}"
        self._logger = logging.getLogger(f"blaecktcpy.{logger_name}")
        if log_level is None:
            self._logger.disabled = True
        else:
            self._logger.setLevel(log_level)

        # Device info
        self.signals = SignalList()
        self._device_name = device_name.encode()
        self._device_hw_version = device_hw_version.encode()
        self._device_fw_version = device_fw_version.encode()

        # Protocol state
        self._timed_activated = False
        self._fixed_interval_ms = IntervalMode.CLIENT
        self._last_client_activate_cmd: str | None = None
        self._timer = _IntervalTimer()
        self._master_slave_config = b"\x00"
        self._slave_id = b"\x00"
        self._command_handlers: dict[str, Callable[..., Any]] = {}
        self._non_forwarded_commands: set[str] = set()
        self._read_callback: Callable[..., Any] | None = None
        self._connect_callback: Callable[[int], Any] | None = None
        self._disconnect_callback: Callable[[int], Any] | None = None
        self._before_write_callback: Callable[[], Any] | None = None
        self._server_restarted = True
        self._restart_flag_pending = True
        self._tcp = ClientManager(self, self._logger)
        self._closed = False
        self._timestamp_mode = TimestampMode.NONE
        self._start_time: float = 0.0
        self._schema_hash: int = 0

        # Upstream state
        self._hub = HubManager(self, self._logger)
        self._upstream_disconnect_callback: Callable[..., Any] | None = None
        self._data_received_callbacks: list[tuple[str | None, Callable[..., Any]]] = []

        # Local signal boundary (frozen at start())
        self._local_signal_count = 0
        self._started = False

        # HTTP status page
        self._http_port = http_port
        self._httpd = None

    # ========================================================================
    # Setup — Socket and Listening
    # ========================================================================

    def start(self) -> None:
        """Create socket, bind, listen, register upstream signals, activate.

        Must be called after all :meth:`add_signal`, :meth:`add_tcp`, and
        :meth:`add_serial` calls (though :meth:`add_signal` also works after
        start).
        """
        if self._started:
            raise RuntimeError("Already started")

        # Freeze local signal count
        self._local_signal_count = len(self.signals)

        # Create and bind socket
        self._tcp.init_socket()

        try:
            self._tcp.bind(self._ip, self._port)
        except OSError:
            assert self._tcp._server_socket is not None
            self._tcp._server_socket.close()
            if not self._stdin_is_interactive():
                raise OSError(f"Port {self._port} is already in use")
            alt_port = self._find_free_port(self._ip, self._port)
            self._logger.warning(
                f"Something is already running on port {self._port}."
            )
            answer = input(
                f"Would you like to run blaecktcpy on port {alt_port} instead? \033[1m(Y/n)\033[0m "
            ).strip()
            if answer.lower() in ("", "y", "yes"):
                self._port = alt_port
                self._tcp.init_socket()
                self._tcp.bind(self._ip, self._port)
            else:
                raise OSError(f"Port {self._port} is already in use")

        self._tcp.start_listening()

        # Connect and discover all upstreams sequentially
        self._hub.discover_all()
        self._hub.register_signals()

        self._started = True
        self._start_time = time.time()
        self._update_schema_hash()
        self._install_signal_handler()
        self._hub.activate()
        self._log_startup_banner()
        self._start_http_status_page()
        self._log_local_signals()
        atexit.register(self.close)

    def _log_local_signals(self) -> None:
        """Log local signal count and interval mode."""
        if self._local_signal_count == 0:
            return
        n = self._local_signal_count
        interval_info = ""
        if self._fixed_interval_ms >= 0:
            interval_info = (
                f" (interval: {self._fixed_interval_ms} ms"
                f" — client control locked)"
            )
        elif self._fixed_interval_ms == IntervalMode.OFF:
            interval_info = " (DEACTIVATE — client control locked)"
        elif self._fixed_interval_ms == IntervalMode.CLIENT:
            interval_info = " (interval: client controlled)"
        self._logger.info(
            f"Local: {n} signal{'s' if n != 1 else ''}"
            f"{interval_info}"
        )

    def _register_upstream_signals(self) -> None:
        """Build slave_id_maps, register upstream signals, and build index maps."""
        self._hub.register_signals()

    def _activate_upstreams(self) -> None:
        """Send ACTIVATE or DEACTIVATE to upstreams based on interval setting."""
        self._hub.activate()

    def _log_startup_banner(self) -> None:
        """Log the startup banner with address and signal count."""
        total = len(self.signals)
        n_up = len(self._hub._upstreams)
        if n_up:
            self._logger.info(
                f"{_RESET}{_BOLD}blaecktcpy v{LIB_VERSION}{_RESET} — Listening on "
                f"{_BOLD}{self._ip}:{self._port}{_RESET} "
                f"({total} signals, {n_up} upstream{'s' if n_up != 1 else ''})"
            )
        else:
            self._logger.info(
                f"{_RESET}{_BOLD}blaecktcpy v{LIB_VERSION}{_RESET} — Listening on "
                f"{_BOLD}{self._ip}:{self._port}{_RESET}"
            )

    def _start_http_status_page(self) -> None:
        """Start the HTTP status page server if configured."""
        if self._http_port is None:
            return

        from ._http import start_http_server
        http_ip = "127.0.0.1" if self._ip in ("0.0.0.0", "") else self._ip
        try:
            self._httpd = start_http_server(self, self._http_port)
        except OSError:
            orig_port = self._http_port
            alt_port = self._find_free_port(http_ip, self._http_port)
            try:
                self._httpd = start_http_server(self, alt_port)
                self._http_port = alt_port
                self._logger.warning(
                    f"HTTP port {orig_port} was in use, "
                    f"using port {alt_port} instead"
                )
            except OSError as e:
                self._logger.warning(
                    f"HTTP status page could not start: {e}"
                )
        if self._httpd is not None:
            self._logger.info(
                f"{_RESET}Status page: "
                f"{_BLUE_ULINE}http://{http_ip}:{self._http_port}{_RESET}"
            )

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

    def _install_signal_handler(self) -> None:
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
        signal_or_name: Signal | str,
        datatype: str = "",
        value: int | float = 0,
    ) -> Signal:
        """Add a local signal.

        Can be called with a Signal object or with individual arguments::

            bltcp.add_signal(Signal('temp', 'float', 0.0))
            bltcp.add_signal('temp', 'float', 0.0)         # shorthand

        Can be called before or after :meth:`start`.

        Returns the added Signal.
        """
        if isinstance(signal_or_name, Signal):
            sig = signal_or_name
        elif isinstance(signal_or_name, str):
            sig = Signal(signal_or_name, datatype, value)
        else:
            raise TypeError(f"Expected Signal or str, got {type(signal_or_name)}")

        if self._started:
            # Insert at the local boundary, before upstream signals
            self.signals.insert(self._local_signal_count, sig)
            self._local_signal_count += 1
            self._rebuild_upstream_indices()
            self._update_schema_hash()
        else:
            self.signals.append(sig)

        return sig

    def add_signals(self, signals) -> None:
        """Add multiple local signals at once.

        Accepts any iterable of Signal objects::

            bltcp.add_signals([
                Signal('temp', 'float', 0.0),
                Signal('led',  'bool',  False),
            ])
        """
        for sig in signals:
            self.add_signal(sig)

    def delete_signals(self) -> None:
        """Remove all local signals.

        After start, upstream signals are preserved and their indices
        are rebuilt.
        """
        if self._started:
            n = self._local_signal_count
            if n > 0:
                del self.signals[:n]
                self._local_signal_count = 0
                self._rebuild_upstream_indices()
                self._update_schema_hash()
        else:
            self.signals = SignalList()

    def _resolve_signal(self, key: str | int) -> int:
        """Resolve a signal name or index to a valid local signal index."""
        lc = self._local_signal_count if self._started else len(self.signals)
        if isinstance(key, int):
            if 0 <= key < lc:
                return key
            raise IndexError(f"Signal index {key} out of range")
        idx = self.signals.index_of(key)
        if idx is not None and idx < lc:
            return idx
        raise KeyError(f"Signal '{key}' not found")

    def write(
        self,
        key: str | int,
        value: int | float,
        *,
        msg_id: int = 1,
        unix_timestamp: float | int | None = None,
    ) -> None:
        """Update a single local signal's value and immediately send it.

        Args:
            key: Signal name (str) or index (int)
            value: New value to set
            msg_id: Message ID for the protocol frame
            unix_timestamp: Override timestamp for UNIX mode.
                float = seconds since epoch (converted internally),
                int = microseconds since epoch (used directly).
        """
        idx = self._resolve_signal(key)
        self.signals[idx].value = value
        if not self.connected:
            return
        ts = self._resolve_timestamp(unix_timestamp)
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = b"<BLAECK:" + self._build_data_msg(header, idx, idx, timestamp=ts) + b"/BLAECK>\r\n"
        self._tcp_send_data(data)

    def update(self, key: str | int, value: int | float) -> None:
        """Update a local signal's value and mark it as updated (no send).

        Args:
            key: Signal name (str) or index (int)
            value: New value to set
        """
        idx = self._resolve_signal(key)
        self.signals[idx].value = value
        self.signals[idx].updated = True

    def mark_signal_updated(self, key: str | int) -> None:
        """Mark a local signal as updated without changing its value."""
        idx = self._resolve_signal(key)
        self.signals[idx].updated = True

    def mark_all_signals_updated(self) -> None:
        """Mark all local signals as updated."""
        lc = self._local_signal_count if self._started else len(self.signals)
        for i in range(lc):
            self.signals[i].updated = True

    def clear_all_update_flags(self) -> None:
        """Clear the updated flag on all local signals."""
        lc = self._local_signal_count if self._started else len(self.signals)
        for i in range(lc):
            self.signals[i].updated = False

    @property
    def has_updated_signals(self) -> bool:
        """True if any local signal is marked as updated."""
        lc = self._local_signal_count if self._started else len(self.signals)
        return any(self.signals[i].updated for i in range(lc))

    # ========================================================================
    # Upstream Setup
    # ========================================================================
    def add_tcp(
        self,
        ip: str,
        port: int,
        name: str = "",
        timeout: float = 5.0,
        interval_ms: int = IntervalMode.CLIENT,
        relay_downstream: bool = True,
        forward_custom_commands: bool | list[str] = True,
        auto_reconnect: bool = False,
    ) -> _UpstreamDevice:
        """Register an upstream TCP device for later discovery.

        Does not connect or block; connection and discovery happen
        in :meth:`start`.  Must be called before :meth:`start`.

        Args:
            ip: IP address of the upstream device
            port: TCP port of the upstream device
            name: Optional friendly name; defaults to upstream device name
            timeout: Connection and discovery timeout in seconds
            interval_ms: Interval in milliseconds, or an
                :class:`IntervalMode` member.
            relay_downstream: If False, signals are decoded but not exposed
                to downstream clients.
            forward_custom_commands: Controls which custom commands are
                forwarded to this upstream.  ``True`` (default) forwards
                all, ``False`` forwards none, or a list of command names
                to forward only those.
            auto_reconnect: If True, automatically reconnect when the
                upstream TCP connection is lost.

        Returns:
            Upstream handle for accessing signal values.
        """
        return self._hub.add_tcp(
            ip, port, name, timeout, interval_ms,
            relay_downstream, forward_custom_commands, auto_reconnect,
        )

    def add_serial(
        self,
        port: str,
        baudrate: int = 115200,
        name: str = "",
        timeout: float = 5.0,
        dtr: bool = True,
        interval_ms: int = IntervalMode.CLIENT,
        relay_downstream: bool = True,
        forward_custom_commands: bool | list[str] = True,
    ) -> _UpstreamDevice:
        """Register an upstream serial device for later discovery.

        Does not connect or block; connection and discovery happen
        in :meth:`start`.
        Requires pyserial: ``pip install blaecktcpy[serial]``
        Must be called before :meth:`start`.

        Args:
            port: Serial port (e.g. 'COM3', '/dev/ttyUSB0')
            baudrate: Serial baud rate
            name: Optional friendly name; defaults to upstream device name
            timeout: Connection and discovery timeout in seconds
            dtr: Enable DTR (set False for Arduino Mega to prevent reset)
            interval_ms: Interval in milliseconds, or an
                :class:`IntervalMode` member.
            relay_downstream: If False, signals are decoded but not exposed
                to downstream clients.
            forward_custom_commands: Controls which custom commands are
                forwarded to this upstream.  ``True`` (default) forwards
                all, ``False`` forwards none, or a list of command names
                to forward only those.

        Returns:
            Upstream handle for accessing signal values.
        """
        return self._hub.add_serial(
            port, baudrate, name, timeout, dtr, interval_ms,
            relay_downstream, forward_custom_commands,
        )

    def _discover_all_upstreams(self) -> None:
        """Connect and discover each upstream sequentially (blocking)."""
        self._hub.discover_all()

    def _poll_upstreams(self) -> None:
        """Read frames from all upstream devices, update signals, and relay."""
        self._hub.poll()

    def _fire_data_received(self, upstream: _UpstreamDevice) -> None:
        """Invoke all matching on_data_received callbacks."""
        for name_filter, func in self._data_received_callbacks:
            if name_filter is None or name_filter == upstream.device_name:
                func(upstream)

    def _send_upstream_lost_frame(self, upstream: _UpstreamDevice) -> None:
        """Send STATUS_UPSTREAM_LOST frame for a disconnected upstream."""
        self._hub._send_upstream_lost_frame(upstream)

    def _send_upstream_reconnected_frame(self, upstream: _UpstreamDevice) -> None:
        """Send STATUS_UPSTREAM_RECONNECTED frame for a reconnected upstream."""
        self._hub._send_upstream_reconnected_frame(upstream)

    def _resend_activate(self, upstream: _UpstreamDevice) -> None:
        """Re-send ACTIVATE/DEACTIVATE after upstream restart or reconnect."""
        self._hub._resend_activate(upstream)

    # ========================================================================
    # Connection Management
    # ========================================================================
    @property
    def connected(self) -> bool:
        """True if any downstream client is connected."""
        return bool(self._tcp._clients)

    @property
    def commanding_client(self) -> socket.socket | None:
        """The client socket that sent the most recent command, or None."""
        return self._tcp._commanding_client

    @property
    def data_clients(self) -> set[int]:
        """Set of client IDs that receive data frames.

        Mutate this set (e.g. ``discard``, ``add``) to control which
        clients receive data.
        """
        return self._tcp.data_clients

    @data_clients.setter
    def data_clients(self, value: set[int]) -> None:
        self._tcp.data_clients = value

    def _accept_new_clients(self) -> None:
        """Accept all pending new connections."""
        self._tcp.accept()

    def _client_id_for(self, conn: socket.socket) -> int:
        """Find the client ID for a given socket, or -1 if not found."""
        return self._tcp.client_id_for(conn)

    def _disconnect_client(self, conn: socket.socket) -> None:
        """Remove and close a client connection."""
        self._tcp.disconnect(conn)

    def _tcp_read(self) -> list[tuple[str, list[str], socket.socket]]:
        """Non-blocking TCP read; returns list of (command, params, conn) tuples."""
        return self._tcp.read_commands()

    def _tcp_send(self, data: bytes) -> bool:
        """Broadcast data to all connected clients."""
        return self._tcp.send_all(data)

    def _tcp_send_data(self, data: bytes) -> bool:
        """Send data only to clients in data_clients set."""
        return self._tcp.send_data(data)

    # ========================================================================
    # Callbacks
    # ========================================================================
    def on_command(self, command: str | None = None, *, forward: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a command handler.

        With a command name, registers a handler for that specific command.
        Parameters are unpacked as positional string arguments.

        Without a command name, registers a catch-all that fires for every
        message after built-in and specific handlers.  Receives the command
        name as the first argument followed by parameters.

        Args:
            command: Command name to handle, or None for a catch-all.
            forward: Whether the command is also forwarded to upstreams
                that have ``forward_custom_commands=True``.  Defaults to
                True.  Set to False for local-only handling.

        Example::

            @bltcp.on_command("SET_LED")
            def handle_led(state):
                print(f"LED = {state}")

            @bltcp.on_command()
            def log_all(command, *params):
                print(f"{command}: {params}")
        """

        if not isinstance(forward, bool):
            raise TypeError("forward must be True or False")

        def decorator(func):
            if command is None:
                self._read_callback = func
            else:
                self._command_handlers[command] = func
                if not forward:
                    self._non_forwarded_commands.add(command)
            return func

        return decorator

    def on_client_connected(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

    def on_client_disconnected(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

    def on_before_write(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

    def on_data_received(self, upstream_name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a callback when upstream data arrives.

        Args:
            upstream_name: If provided, only fires for that upstream.
                If None, fires for any upstream.

        Example::

            @bltcp.on_data_received("Arduino")
            def handle(upstream):
                temp = upstream.signals["temperature"].value
        """

        def decorator(func):
            self._data_received_callbacks.append((upstream_name, func))
            return func

        return decorator

    def on_upstream_disconnected(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a callback when an upstream device disconnects.

        Example::

            @bltcp.on_upstream_disconnected()
            def handle(name):
                print(f"Lost connection to {name}")
        """

        def decorator(func):
            self._upstream_disconnect_callback = func
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

    def _update_client_identity(self, params: list[str], conn: socket.socket) -> None:
        """Extract optional RequesterDeviceName/Type from GET_DEVICES params."""
        if len(params) <= 4:
            return
        client_id = self._client_id_for(conn)
        if client_id < 0:
            return
        name = params[4].strip() if len(params) > 4 else ""
        rtype = params[5].strip() if len(params) > 5 else "unknown"
        if name:
            self._tcp._client_meta[client_id] = {"name": name, "type": rtype}
            addr = self._tcp._client_addrs.get(client_id, "")
            self._logger.info(
                f"Client #{client_id} identified ({rtype}: {name})"
            )

    # ========================================================================
    # Message Handlers
    # ========================================================================
    def read(self) -> None:
        """Read and process all pending messages from downstream clients."""
        messages = self._tcp_read()

        for command, params, conn in messages:
            self._tcp._commanding_client = conn
            self._dispatch_protocol_command(command, params, conn)

            # Dispatch to specific command handler
            handler = self._command_handlers.get(command)
            if handler is not None:
                handler(*params)

            self._forward_custom_command(command, params)

            # Fire catch-all callback
            if self._read_callback is not None:
                self._read_callback(command, *params)

    def _dispatch_protocol_command(
        self, command: str, params: list[str], conn: socket.socket
    ) -> None:
        """Handle BLAECK.* protocol commands from downstream clients."""
        if command == "BLAECK.WRITE_SYMBOLS":
            self.write_symbols(self._decode_four_byte(params))
        elif command == "BLAECK.GET_DEVICES":
            self._update_client_identity(params, conn)
            self.write_devices(self._decode_four_byte(params))
        elif command in (
            "BLAECK.ACTIVATE",
            "BLAECK.DEACTIVATE",
            "BLAECK.WRITE_DATA",
        ):
            if self._hub._upstreams:
                self._handle_hub_data_command(command, params)
            else:
                self._handle_simple_data_command(command, params)

    def _handle_hub_data_command(self, command: str, params: list) -> None:
        """Handle ACTIVATE/DEACTIVATE/WRITE_DATA in hub mode."""
        if params:
            full_cmd = f"{command},{','.join(str(p) for p in params)}"
        else:
            full_cmd = command

        if command == "BLAECK.WRITE_DATA":
            # One-shot: forward to relayed upstreams only
            for upstream in self._hub._upstreams:
                if upstream.relay_downstream and upstream.transport.connected:
                    upstream.transport.send_command(full_cmd)
        else:
            # ACTIVATE/DEACTIVATE: only forward to client-managed relayed upstreams
            for upstream in self._hub._upstreams:
                if (
                    upstream.relay_downstream
                    and upstream.interval_ms == IntervalMode.CLIENT
                    and upstream.transport.connected
                ):
                    upstream.transport.send_command(full_cmd)
                    if command == "BLAECK.ACTIVATE":
                        interval = self._decode_four_byte(params)
                        self._logger.info(
                            f"Client ACTIVATE forwarded to upstream '{upstream.device_name}' ({interval} ms)"
                        )
                    else:
                        self._logger.info(
                            f"Client DEACTIVATE forwarded to upstream '{upstream.device_name}' (OFF)"
                        )
            # Store for replay on upstream reconnect
            self._last_client_activate_cmd = full_cmd

        # Local signals: respond to client when in client-controlled mode
        if (
            self._local_signal_count > 0
            and self._fixed_interval_ms == IntervalMode.CLIENT
        ):
            if command == "BLAECK.ACTIVATE":
                self._set_timed_data(True, self._decode_four_byte(params))
            elif command == "BLAECK.DEACTIVATE":
                self._set_timed_data(False)

        # WRITE_DATA: one-shot send of local signals
        if self._local_signal_count > 0 and command == "BLAECK.WRITE_DATA":
            if self._before_write_callback is not None:
                self._before_write_callback()
            msg_id = self._decode_four_byte(params)
            ts = self._auto_timestamp()
            header = (
                self.MSG_DATA
                + b":"
                + msg_id.to_bytes(4, "little")
                + b":"
            )
            data = (
                b"<BLAECK:"
                + self._build_data_msg(
                    header, start=0, end=self._local_signal_count - 1,
                    timestamp=ts,
                )
                + b"/BLAECK>\r\n"
            )
            self._tcp_send_data(data)

    def _handle_simple_data_command(self, command: str, params: list) -> None:
        """Handle ACTIVATE/DEACTIVATE/WRITE_DATA in simple server mode."""
        if command == "BLAECK.WRITE_DATA":
            self.write_all_data(self._decode_four_byte(params))
        elif command == "BLAECK.ACTIVATE":
            if self._fixed_interval_ms == IntervalMode.CLIENT:
                self._set_timed_data(True, self._decode_four_byte(params))
        elif command == "BLAECK.DEACTIVATE":
            if self._fixed_interval_ms == IntervalMode.CLIENT:
                self._set_timed_data(False)

    def _forward_custom_command(self, command: str, params: list) -> None:
        """Forward non-BLAECK commands to opted-in upstreams."""
        if command in self._non_forwarded_commands or command.startswith("BLAECK."):
            return
        if params:
            full_cmd = f"{command},{','.join(str(p) for p in params)}"
        else:
            full_cmd = command
        for upstream in self._hub._upstreams:
            fcc = upstream.forward_custom_commands
            if not fcc or not upstream.transport.connected:
                continue
            if isinstance(fcc, list) and command not in fcc:
                continue
            upstream.transport.send_command(full_cmd)

    # ========================================================================
    # Message Writers
    # ========================================================================
    def write_symbols(self, msg_id: int = 1) -> None:
        """Send symbol list to connected clients."""
        if not self.connected:
            return

        header = self.MSG_SYMBOL_LIST + b":" + msg_id.to_bytes(4, "little") + b":"

        if self._hub._upstreams:
            # Hub-style: MSC_MASTER for local, MSC_SLAVE for upstream
            payload = b""
            # Local (master) signals first
            for i in range(self._local_signal_count):
                sig = self.signals[i]
                payload += (
                    _MSC_MASTER
                    + b"\x00"
                    + sig.signal_name.encode()
                    + b"\0"
                    + sig.get_dtype_byte()
                )
            # Upstream (slave) signals — only relayed upstreams
            for upstream in self._hub._upstreams:
                if not upstream.relay_downstream:
                    continue
                for sym in upstream.symbol_table:
                    key = (sym.msc, sym.slave_id)
                    hub_sid = upstream.slave_id_map.get(key)
                    if hub_sid is None:
                        continue
                    dtype_code = sym.datatype_code.to_bytes(1, "little")
                    payload += (
                        _MSC_SLAVE
                        + bytes([hub_sid])
                        + sym.name.encode()
                        + b"\0"
                        + dtype_code
                    )
            data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
        else:
            # Simple server-style
            data = b"<BLAECK:" + header + self._get_symbols() + b"/BLAECK>\r\n"

        self._tcp_send(data)

    def write_devices(self, msg_id: int = 1) -> None:
        """Send device information to each connected client."""
        if not self.connected:
            return

        header = self.MSG_DEVICES + b":" + msg_id.to_bytes(4, "little") + b":"

        for client_id, conn in list(self._tcp._clients.items()):
            if self._hub._upstreams:
                payload = self._build_hub_devices_payload(client_id)
            else:
                payload = self._build_simple_device_payload(client_id)

            data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
            try:
                conn.sendall(data)
            except OSError as e:
                self._logger.debug(f"Send error: {e}")
                self._disconnect_client(conn)

        self._server_restarted = False

        # Clear upstream server_restarted after sending to prevent stale values
        for upstream in self._hub._upstreams:
            for info in upstream.device_infos:
                if info.server_restarted == "1":
                    info.server_restarted = "0"

    def _build_hub_devices_payload(self, client_id: int) -> bytes:
        """Build B6 payload for hub mode: DeviceCount + devices + client trailer."""
        # Count devices: 1 master + relayed upstream devices
        device_count = 1
        for upstream in self._hub._upstreams:
            if not upstream.relay_downstream:
                continue
            for info in upstream.device_infos:
                if upstream.slave_id_map.get((info.msc, info.slave_id)) is not None:
                    device_count += 1

        payload = bytes([device_count])

        # Master device
        payload += self._encode_device_entry(
            _MSC_MASTER, b"\x00",
            self._device_name, self._device_hw_version, self._device_fw_version,
            LIB_VERSION.encode(), LIB_NAME.encode(),
            b"1" if self._server_restarted else b"0",
            b"hub", b"0",
        )

        # Upstream devices as slaves
        for upstream in self._hub._upstreams:
            if not upstream.relay_downstream:
                continue
            old_sid_to_new: dict[int, int] = {}
            for (msc, sid), hub_sid in upstream.slave_id_map.items():
                old_sid_to_new[sid] = hub_sid
            first_entry = True
            for info in upstream.device_infos:
                key = (info.msc, info.slave_id)
                hub_sid = upstream.slave_id_map.get(key)
                if hub_sid is None:
                    continue
                device_type = info.device_type or "server"
                if first_entry:
                    parent_sid = 0
                    first_entry = False
                else:
                    orig_parent = int(info.parent) if info.parent else 0
                    parent_sid = old_sid_to_new.get(orig_parent, 0)
                payload += self._encode_device_entry(
                    _MSC_SLAVE, bytes([hub_sid]),
                    info.device_name.encode(), info.hw_version.encode(),
                    info.fw_version.encode(), info.lib_version.encode(),
                    info.lib_name.encode(),
                    info.server_restarted.encode() if info.server_restarted else b"0",
                    device_type.encode(), str(parent_sid).encode(),
                )

        return payload + self._build_client_trailer(client_id)

    def _build_simple_device_payload(self, client_id: int) -> bytes:
        """Build B6 payload for simple server: DeviceCount=1 + device + client trailer."""
        payload = b"\x01" + self._encode_device_entry(
            self._master_slave_config, self._slave_id,
            self._device_name, self._device_hw_version, self._device_fw_version,
            LIB_VERSION.encode(), LIB_NAME.encode(),
            b"1" if self._server_restarted else b"0",
            b"server", b"0",
        )
        return payload + self._build_client_trailer(client_id)

    @staticmethod
    def _encode_device_entry(
        msc: bytes, slave_id: bytes,
        name: bytes, hw: bytes, fw: bytes,
        lib_ver: bytes, lib_name: bytes,
        restarted: bytes, device_type: bytes, parent: bytes,
    ) -> bytes:
        """Encode a single B6 device entry (MSC through Parent)."""
        return _encoder.encode_device_entry(
            msc, slave_id, name, hw, fw,
            lib_ver, lib_name, restarted, device_type, parent,
        )

    def _build_client_trailer(self, client_id: int) -> bytes:
        """Build B6 client trailer: ClientNo, DataEnabled, ClientName, ClientType."""
        return _encoder.build_client_trailer(
            client_id, self._tcp.data_clients, self._tcp._client_meta,
        )

    def write_all_data(self, msg_id: int = 1, *, unix_timestamp: float | int | None = None) -> None:
        """Send all local signal data to data-enabled clients.

        Args:
            msg_id: Message ID for the protocol frame.
            unix_timestamp: Override timestamp for UNIX mode.
                float = seconds since epoch (converted internally),
                int = microseconds since epoch (used directly).
        """
        if not self.connected:
            return
        lc = self._local_signal_count if self._started else len(self.signals)
        if lc == 0:
            return
        if self._before_write_callback is not None:
            self._before_write_callback()
        ts = self._resolve_timestamp(unix_timestamp)
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, start=0, end=lc - 1, timestamp=ts)
            + b"/BLAECK>\r\n"
        )
        self._tcp_send_data(data)

    def write_updated_data(self, msg_id: int = 1, *, unix_timestamp: float | int | None = None) -> None:
        """Send only updated local signals to data-enabled clients.

        Args:
            msg_id: Message ID for the protocol frame.
            unix_timestamp: Override timestamp for UNIX mode.
                float = seconds since epoch (converted internally),
                int = microseconds since epoch (used directly).
        """
        if not self.connected or not self.has_updated_signals:
            return
        lc = self._local_signal_count if self._started else len(self.signals)
        if lc == 0:
            return
        if self._before_write_callback is not None:
            self._before_write_callback()
        ts = self._resolve_timestamp(unix_timestamp)
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, start=0, end=lc - 1, only_updated=True, timestamp=ts)
            + b"/BLAECK>\r\n"
        )
        self._tcp_send_data(data)

    def _timer_elapsed(self) -> bool:
        """Check if the timed interval has elapsed."""
        return self._timer.elapsed()

    def timed_write_all_data(self, msg_id: int | None = None, *, unix_timestamp: float | int | None = None) -> bool:
        """Send all local data if timer interval has elapsed.

        Args:
            msg_id: Message ID for the protocol frame.  *None* (default)
                selects ``_MSG_ID_HUB`` when the local interval is
                locked, ``_MSG_ID_ACTIVATE`` otherwise.
            unix_timestamp: Override timestamp for UNIX mode.
                float = seconds since epoch (converted internally),
                int = microseconds since epoch (used directly).
        """
        if msg_id is None:
            msg_id = _MSG_ID_HUB if self._fixed_interval_ms >= 0 else _MSG_ID_ACTIVATE
        if not self.connected:
            return False
        if not self._timed_activated:
            return False
        lc = self._local_signal_count if self._started else len(self.signals)
        if lc == 0:
            return False
        if not self._timer_elapsed():
            return False
        if self._before_write_callback is not None:
            self._before_write_callback()
        ts = self._resolve_timestamp(unix_timestamp)
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, start=0, end=lc - 1, timestamp=ts)
            + b"/BLAECK>\r\n"
        )
        return self._tcp_send_data(data)

    def timed_write_updated_data(self, msg_id: int | None = None, *, unix_timestamp: float | int | None = None) -> bool:
        """Send only updated local signals if timer interval has elapsed.

        Args:
            msg_id: Message ID for the protocol frame.  *None* (default)
                selects ``_MSG_ID_HUB`` when the local interval is
                locked, ``_MSG_ID_ACTIVATE`` otherwise.
            unix_timestamp: Override timestamp for UNIX mode.
                float = seconds since epoch (converted internally),
                int = microseconds since epoch (used directly).
        """
        if msg_id is None:
            msg_id = _MSG_ID_HUB if self._fixed_interval_ms >= 0 else _MSG_ID_ACTIVATE
        if not self.connected:
            return False
        if not self._timed_activated:
            return False
        lc = self._local_signal_count if self._started else len(self.signals)
        if lc == 0:
            return False
        if not self._timer_elapsed():
            return False
        if not self.has_updated_signals:
            return False
        if self._before_write_callback is not None:
            self._before_write_callback()
        ts = self._resolve_timestamp(unix_timestamp)
        header = self.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._build_data_msg(header, start=0, end=lc - 1, only_updated=True, timestamp=ts)
            + b"/BLAECK>\r\n"
        )
        return self._tcp_send_data(data)

    def _set_timed_data(self, activated: bool, interval_ms: int = 0) -> None:
        """Programmatically activate or deactivate timed data transmission."""
        self._timed_activated = activated
        if activated:
            self._timer.activate(interval_ms)
            self._logger.info(f"Client ACTIVATE received — local signal interval ({interval_ms} ms)")
        else:
            self._timer.deactivate()
            self._logger.info("Client DEACTIVATE received — local signal interval (OFF)")

    @property
    def local_interval_ms(self) -> int:
        """Local signal timed data interval mode.

        Controls the output rate of local signals only.  In hub mode,
        upstream signals are relayed independently at their own rate
        (configured via the ``interval_ms`` parameter of
        :meth:`add_tcp` / :meth:`add_serial`).

        * **value >= 0** — Lock at the given rate (ms).  Client
          ``ACTIVATE`` / ``DEACTIVATE`` commands are ignored.
          ``0`` means "as fast as possible."
        * **IntervalMode.OFF** — Timed data is off.  Client
          ``ACTIVATE`` is ignored.
        * **IntervalMode.CLIENT** — Client controlled (default).
          The client's ``ACTIVATE`` / ``DEACTIVATE`` commands
          determine the rate.
        """
        return self._fixed_interval_ms

    @local_interval_ms.setter
    def local_interval_ms(self, value: int) -> None:
        if value >= 0:
            self._fixed_interval_ms = value
            self._timed_activated = True
            self._timer.activate(value)
            if self._started:
                self._logger.info(
                    f"Local signal interval changed ({value} ms)"
                )
        elif value == IntervalMode.OFF:
            self._fixed_interval_ms = IntervalMode.OFF
            self._timed_activated = False
            self._timer.deactivate()
            if self._started:
                self._logger.info("Local signal interval changed (OFF)")
        elif value == IntervalMode.CLIENT:
            self._fixed_interval_ms = IntervalMode.CLIENT
            if self._started:
                self._logger.info(
                    "Local signal interval changed (client controlled)"
                )

    @property
    def start_time(self) -> float:
        """Wall-clock time when :meth:`start` was called (``time.time()``).

        Useful as a reference point for elapsed-time calculations.
        """
        return self._start_time

    @property
    def timestamp_mode(self) -> TimestampMode:
        """Timestamp mode for outgoing data frames.

        * **TimestampMode.NONE** — No timestamp (default).
        * **TimestampMode.UNIX** — Microseconds since Unix epoch.
        """
        return self._timestamp_mode

    @timestamp_mode.setter
    def timestamp_mode(self, value: TimestampMode) -> None:
        try:
            mode = TimestampMode(value)
        except ValueError:
            valid = ", ".join(f"{m.name} ({m.value})" for m in TimestampMode)
            raise ValueError(
                f"Invalid timestamp_mode {value!r}. Valid modes: {valid}"
            ) from None
        if mode == TimestampMode.MICROS:
            raise ValueError(
                "TimestampMode.MICROS is not supported for blaecktcpy servers. "
                "Use TimestampMode.UNIX instead."
            )
        self._timestamp_mode = mode

    def _resolve_timestamp(
        self,
        unix_timestamp: float | int | None,
    ) -> int | None:
        """Resolve an explicit timestamp override to microseconds.

        Returns the resolved value in microseconds, or falls back to the
        auto-generated timestamp for the current mode.

        Raises:
            ValueError: If the override doesn't match the current mode
                or if used with NONE mode.
            TypeError: If the value has the wrong type.
        """
        if unix_timestamp is not None:
            if self._timestamp_mode != TimestampMode.UNIX:
                raise ValueError(
                    "unix_timestamp can only be used with TimestampMode.UNIX"
                )
            if isinstance(unix_timestamp, float):
                return int(unix_timestamp * 1_000_000)
            if isinstance(unix_timestamp, int) and not isinstance(unix_timestamp, bool):
                return unix_timestamp
            raise TypeError(
                "unix_timestamp must be float (seconds) or int (µs)"
            )

        return self._auto_timestamp()

    def _auto_timestamp(self) -> int | None:
        """Return the auto-generated timestamp for the current mode, or None."""
        if self._timestamp_mode == TimestampMode.UNIX:
            return int(time.time() * 1_000_000)
        return None

    # ========================================================================
    # Main Loop
    # ========================================================================
    def tick(self, msg_id: int | None = None) -> bool:
        """Main loop tick — read commands, poll upstreams, send all data on timer.

        Call this repeatedly in your main loop.
        Returns True if timed local data was sent.
        """
        self.read()
        self._poll_upstreams()
        return self.timed_write_all_data(msg_id)

    def tick_updated(self, msg_id: int | None = None) -> bool:
        """Main loop tick — read commands, poll upstreams, send only updated data.

        Like tick() but only transmits local signals marked as updated.
        Returns True if timed local data was sent.
        """
        self.read()
        self._poll_upstreams()
        return self.timed_write_updated_data(msg_id)

    # ========================================================================
    # Upstream Schema
    # ========================================================================

    def _rebuild_upstream_indices(self) -> None:
        """Rebuild relayed upstream index_map from current signals list."""
        self._hub._rebuild_upstream_indices()

    def _update_schema_hash(self) -> None:
        """Recompute the server's schema hash from all signals."""
        pairs = []
        for sig in self.signals:
            code = Signal.DATATYPE_TO_CODE.get(sig.datatype, 0)
            pairs.append((sig.signal_name, code))
        self._schema_hash = decoder.compute_schema_hash(pairs)

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
        timestamp_mode: TimestampMode | None = None,
        status: int = STATUS_OK,
        status_payload: bytes = b"\x00\x00\x00\x00",
    ) -> bytes:
        """Build data message with CRC32 checksum (v5 format).

        Delegates to :func:`_encoder.build_data_frame`.
        """
        restart = self._restart_flag_pending
        self._restart_flag_pending = False
        mode = timestamp_mode if timestamp_mode is not None else self._timestamp_mode
        return _encoder.build_data_frame(
            header, self.signals, start, end,
            schema_hash=self._schema_hash,
            restart_flag=restart,
            timestamp_mode=int(mode),
            timestamp=timestamp,
            only_updated=only_updated,
            status=status,
            status_payload=status_payload,
        )

    def _get_symbols(self) -> bytes:
        """Build symbol list message (simple server mode)."""
        return _encoder.build_symbol_payload(
            self.signals, self._master_slave_config, self._slave_id,
        )

    # ========================================================================
    # Status & Properties
    # ========================================================================
    def upstream_status(self, name: str | None = None) -> dict:
        """Get connection status for upstream devices.

        Args:
            name: Specific upstream name, or None for all.

        Returns:
            Dict with 'connected', 'last_seen', 'signals' keys.
            If name is None, returns {name: status_dict, ...}.
        """

        def _status(u: _UpstreamDevice) -> dict:
            return {
                "connected": u.transport.connected,
                "last_seen": u.transport.last_seen,
                "signals": len(u.symbol_table),
            }

        if name is not None:
            for u in self._hub._upstreams:
                if u.device_name == name:
                    return _status(u)
            raise KeyError(f"No upstream named '{name}'")

        return {u.device_name: _status(u) for u in self._hub._upstreams}

    def __getitem__(self, name: str) -> _UpstreamDevice:
        """Access an upstream device by name.

        Example::

            bltcp["Arduino"]["temperature"].value
            bltcp["Arduino"].signals[0].value
        """
        for upstream in self._hub._upstreams:
            if upstream.device_name == name:
                return upstream
        raise KeyError(f"No upstream named {name!r}")

    # ========================================================================
    # Lifecycle
    # ========================================================================
    def close(self):
        """Gracefully close all upstream and downstream connections."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        if hasattr(self, "_original_sigint"):
            signal.signal(signal.SIGINT, self._original_sigint)

        # Stop HTTP status page
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

        # Deactivate and close upstreams
        for upstream in self._hub._upstreams:
            if upstream.transport.connected:
                upstream.transport.send_command("BLAECK.DEACTIVATE")
            upstream.transport.close()

        # Close downstream connections
        self._tcp.close()

        self._logger.info("Server closed")

    def __enter__(self) -> "BlaeckTCPy":
        """Enable 'with' statement usage."""
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> None:
        """Clean up on exit."""
        self.close()

    def __repr__(self) -> str:
        n = len(self._tcp._clients)
        clients = f"{n} client{'s' if n != 1 else ''}"
        active = "active" if self._timed_activated else "inactive"
        n_up = len(self._hub._upstreams)
        if n_up:
            return (
                f"blaecktcpy [{clients}] [{active}] "
                f"({len(self.signals)} signals, {n_up} upstreams)"
            )
        return f"blaecktcpy [{clients}] [{active}] ({len(self.signals)} signals)"
