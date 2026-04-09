"""BlaeckTCPy — Unified BlaeckTCP Protocol Implementation."""

import atexit
import binascii
import logging
import selectors
import signal
import socket
import sys
import time
from dataclasses import dataclass, field

from ._signal import Signal, SignalList, IntervalMode, TimestampMode
from .hub import _decoder as decoder
from .hub._upstream import UpstreamTCP, _UpstreamBase

__all__ = ["BlaeckTCPy"]

from importlib.metadata import version as _pkg_version

LIB_VERSION = _pkg_version("blaecktcpy")
LIB_NAME = "blaecktcpy"

_MAX_RECV_BUFFER = 65536  # 64 KB per-client receive buffer limit

# Status byte values for data frames
STATUS_OK = 0x00
STATUS_UPSTREAM_LOST = 0x80
STATUS_UPSTREAM_RECONNECTED = 0x81

# MasterSlaveConfig byte values
_MSC_MASTER = b"\x01"
_MSC_SLAVE = b"\x02"

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


@dataclass
class _UpstreamDevice:
    """Internal bookkeeping for one upstream connection."""

    device_name: str
    transport: _UpstreamBase
    symbol_table: list[decoder.DecodedSymbol] = field(default_factory=list)
    index_map: dict[int, int] = field(default_factory=dict)
    device_infos: list[decoder.DecodedDeviceInfo] = field(default_factory=list)
    slave_id_map: dict[tuple[int, int], int] = field(default_factory=dict)
    interval_ms: int = IntervalMode.CLIENT
    connected: bool = True
    relay_downstream: bool = True
    forward_custom_commands: bool | list[str] = True
    _signals: list[Signal] = field(default_factory=list)
    _upstream_signals: SignalList | None = field(default=None, repr=False)
    expected_schema_hash: int = 0
    schema_stale: bool = False
    _initial_restart_seen: bool = False
    _restart_c0_sent: bool = False
    auto_reconnect: bool = False
    _reconnect_cooldown: float = 0.0
    _reconnecting: bool = False
    _awaiting_symbols: bool = False
    _awaiting_devices: bool = False

    @property
    def signals(self) -> SignalList:
        if self._upstream_signals is None:
            raise RuntimeError(
                "Signals not available yet — call start() first"
            )
        return self._upstream_signals

    def __getitem__(self, key: int | str) -> Signal:
        return self.signals[key]


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
        self._command_handlers: dict[str, object] = {}
        self._non_forwarded_commands: set[str] = set()
        self._read_callback = None
        self._connect_callback = None
        self._disconnect_callback = None
        self._before_write_callback = None
        self._server_restarted = True
        self._restart_flag_pending = True
        self.data_clients: set[int] = set()
        self._client_meta: dict[int, dict] = {}
        self._client_addrs: dict[int, str] = {}
        self._recv_buffers: dict = {}
        self._closed = False
        self._timestamp_mode = TimestampMode.NONE
        self._start_time: float = 0.0
        self._schema_hash: int = 0

        # Upstream state
        self._upstreams: list[_UpstreamDevice] = []
        self._upstream_disconnect_callback = None
        self._data_received_callbacks: list[tuple[str | None, object]] = []

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
        self._init_socket()

        try:
            self._bind_socket(self._ip, self._port)
        except OSError:
            self._server_socket.close()
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
                self._init_socket()
                self._bind_socket(self._ip, self._port)
            else:
                raise OSError(f"Port {self._port} is already in use")

        self._start_listening()

        # Register upstream signals and build index maps
        if self._upstreams:
            offset = self._local_signal_count
            hub_slave_idx = 0
            for upstream in self._upstreams:
                if upstream.relay_downstream:
                    # Build slave_id_map: (msc, slave_id) → hub slave_id
                    seen: dict[tuple[int, int], int] = {}
                    for sym in upstream.symbol_table:
                        key = (sym.msc, sym.slave_id)
                        if key not in seen:
                            hub_slave_idx += 1
                            seen[key] = hub_slave_idx
                    # Include device-only entries (devices with no symbols)
                    for info in upstream.device_infos:
                        key = (info.msc, info.slave_id)
                        if key not in seen:
                            hub_slave_idx += 1
                            seen[key] = hub_slave_idx
                    upstream.slave_id_map = seen

                    # Relayed: register on self so downstream clients see them
                    for i, sym in enumerate(upstream.symbol_table):
                        sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                            sym.datatype_code, "float"
                        )
                        sig = Signal(sym.name, sig_type)
                        self.signals.append(sig)
                        upstream._signals.append(self.signals[offset])
                        upstream.index_map[i] = offset
                        offset += 1
                else:
                    # Non-relayed: store signals internally for hub-side access
                    for i, sym in enumerate(upstream.symbol_table):
                        sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                            sym.datatype_code, "float"
                        )
                        sig = Signal(sym.name, sig_type)
                        upstream._signals.append(sig)
                        upstream.index_map[i] = i

                # Freeze signal collection now that _signals is fully populated
                upstream._upstream_signals = SignalList(upstream._signals)
                # Cache schema hash for mismatch detection
                upstream.expected_schema_hash = decoder.compute_schema_hash(
                    [(s.name, s.datatype_code) for s in upstream.symbol_table]
                )

        self._started = True
        self._start_time = time.time()
        self._update_schema_hash()
        self._install_signal_handler()

        # Activate/deactivate upstreams based on interval setting
        for upstream in self._upstreams:
            if upstream.interval_ms >= 0:
                b = upstream.interval_ms.to_bytes(4, "little")
                params = ",".join(str(x) for x in b)
                upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}")
            elif upstream.interval_ms == IntervalMode.OFF:
                upstream.transport.send_command("BLAECK.DEACTIVATE")

        total = len(self.signals)
        n_up = len(self._upstreams)
        if n_up:
            self._logger.info(
                f"blaecktcpy v{LIB_VERSION} — Listening on "
                f"{self._ip}:{self._port} "
                f"({total} signals, {n_up} upstream{'s' if n_up != 1 else ''})"
            )
        else:
            self._logger.info(
                f"blaecktcpy v{LIB_VERSION} — Listening on "
                f"{self._ip}:{self._port}"
            )

        # Start HTTP status page
        if self._http_port is not None:
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
                    f"Status page: http://{http_ip}:{self._http_port}"
                )

        atexit.register(self.close)

    def _init_socket(self):
        """Create TCP socket."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            self._server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        else:
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def _bind_socket(self, ip, port):
        """Bind socket to address."""
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
        """Start listening for connections."""
        self._server_socket.setblocking(False)
        self._server_socket.listen()
        self._clients: dict[int, socket.socket] = {}
        self._next_client_id = 0
        self._commanding_client = None
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._server_socket, selectors.EVENT_READ)

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
        for i in range(lc):
            if self.signals[i].signal_name == key:
                return i
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
        """Connect to an upstream TCP device and discover its signals.

        Blocks until the symbol table is fetched or timeout expires.
        Must be called before :meth:`start`.

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
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")
        if not isinstance(relay_downstream, bool):
            raise TypeError("relay_downstream must be True or False")
        if not isinstance(forward_custom_commands, (bool, list)):
            raise TypeError("forward_custom_commands must be True, False, or a list of command names")

        label = name or f"{ip}:{port}"
        transport = UpstreamTCP(label, ip, port, logger=self._logger)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay_downstream, forward_custom_commands, auto_reconnect=auto_reconnect)

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
        """Connect to an upstream serial device and discover its signals.

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
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")
        if not isinstance(relay_downstream, bool):
            raise TypeError("relay_downstream must be True or False")
        if not isinstance(forward_custom_commands, (bool, list)):
            raise TypeError("forward_custom_commands must be True, False, or a list of command names")

        from .hub._upstream import UpstreamSerial

        label = name or port
        transport = UpstreamSerial(label, port, baudrate, dtr, logger=self._logger)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay_downstream, forward_custom_commands)

    def _discover_upstream(
        self,
        name: str,
        transport: _UpstreamBase,
        timeout: float,
        interval_ms: int = 0,
        relay_downstream: bool = True,
        forward_custom_commands: bool | list[str] = True,
        auto_reconnect: bool = False,
    ) -> _UpstreamDevice:
        """Connect and fetch the symbol table from an upstream device."""
        label = transport.name

        if not transport.connect(timeout):
            raise ConnectionError(
                f"Failed to connect to upstream '{label}': {transport.last_error}"
            )

        # Stop any ongoing timed data transmission
        transport.send_command("BLAECK.DEACTIVATE")

        # Poll for symbol list with periodic retries
        transport.send_command("BLAECK.WRITE_SYMBOLS")
        frame = None
        max_polls = int(timeout / 0.1)
        for i in range(max_polls):
            time.sleep(0.1)
            frames = transport.read_frames()
            for f in frames:
                if len(f) > 0 and f[0] == decoder.MSGKEY_SYMBOL_LIST:
                    frame = f
                    break
            if frame is not None:
                break
            if i > 0 and i % 10 == 0:
                transport.send_command("BLAECK.WRITE_SYMBOLS")

        if frame is None:
            transport.close()
            raise TimeoutError(f"Upstream '{label}' did not respond to WRITE_SYMBOLS")

        symbols = decoder.parse_symbol_list(frame)
        if not symbols:
            transport.close()
            raise ValueError(f"Upstream '{label}' reported no signals")

        upstream = _UpstreamDevice(
            device_name=name, transport=transport,
            interval_ms=interval_ms, relay_downstream=relay_downstream,
            forward_custom_commands=forward_custom_commands,
            auto_reconnect=auto_reconnect,
        )
        upstream.symbol_table = symbols

        # Fetch device info with polling retries
        device_msgkeys = decoder.MSGKEY_DEVICES_ALL
        identity = f",0,0,0,0,{self._device_name.decode()},hub"
        transport.send_command(f"BLAECK.GET_DEVICES{identity}")
        frame = None
        for i in range(max_polls):
            time.sleep(0.1)
            frames = transport.read_frames()
            for f in frames:
                if len(f) > 0 and f[0] in device_msgkeys:
                    frame = f
                    break
            if frame is not None:
                break
            if i > 0 and i % 10 == 0:
                transport.send_command(f"BLAECK.GET_DEVICES{identity}")

        if frame is not None:
            try:
                upstream.device_infos = decoder.parse_all_devices(frame)
            except Exception as e:
                self._logger.debug(f"Upstream '{label}' device info parse error: {e}")

        # Use upstream device name if no name was provided
        if not name and upstream.device_infos:
            upstream.device_name = upstream.device_infos[0].device_name

        # Consume initial server_restarted so reconnect can detect real restarts
        for info in (upstream.device_infos or []):
            if info.server_restarted == "1":
                upstream._initial_restart_seen = True
                upstream._restart_c0_sent = True  # suppress first data frame restart_flag too
                break
        self._upstreams.append(upstream)

        interval_info = ""
        if upstream.interval_ms >= 0:
            interval_info = f" (ACTIVATE sent: {upstream.interval_ms} ms — client control locked)"
        elif upstream.interval_ms == IntervalMode.OFF:
            interval_info = " (DEACTIVATE sent — client control locked)"
        elif upstream.interval_ms == IntervalMode.CLIENT:
            interval_info = " (interval: client controlled)"
        self._logger.info(f"Upstream '{upstream.device_name}': {len(symbols)} signals discovered{interval_info}")
        return upstream

    # ========================================================================
    # Connection Management
    # ========================================================================
    @property
    def connected(self) -> bool:
        """True if any downstream client is connected."""
        return bool(getattr(self, "_clients", None))

    @property
    def commanding_client(self):
        """The client socket that sent the most recent command, or None."""
        return getattr(self, "_commanding_client", None)

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
                self._client_meta[client_id] = {"name": "", "type": "unknown"}
                self._client_addrs[client_id] = f"{addr[0]}:{addr[1]}"
                self._logger.info(f"Client #{client_id} connected: {addr[0]}:{addr[1]}")
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
            meta = self._client_meta.pop(client_id, {})
            self._client_addrs.pop(client_id, None)
        else:
            meta = {}
        self._recv_buffers.pop(conn, None)
        if self._commanding_client is conn:
            self._commanding_client = None
        name = meta.get("name", "")
        rtype = meta.get("type", "unknown")
        cid = client_id if client_id >= 0 else '?'
        if name:
            self._logger.info(f"Client #{cid} disconnected ({rtype}: {name})")
        else:
            self._logger.info(f"Client #{cid} disconnected")
        if client_id >= 0 and self._disconnect_callback is not None:
            self._disconnect_callback(client_id)
        if not self._clients and self._fixed_interval_ms == IntervalMode.CLIENT:
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

                    self._logger.debug(f"_tcp_read raw chunk: {chunk!r}")

                    if len(self._recv_buffers[conn]) > _MAX_RECV_BUFFER:
                        self._logger.warning("Receive buffer overflow — dropping client")
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
                    self._logger.debug(f"Read error: {e}")
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
                self._logger.debug(f"Send error: {e}")
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
                self._logger.debug(f"Send error: {e}")
                self._disconnect_client(conn)

        return sent

    # ========================================================================
    # Callbacks
    # ========================================================================
    def on_command(self, command: str | None = None, *, forward: bool = True):
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

    def on_data_received(self, upstream_name: str | None = None):
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

    def on_upstream_disconnected(self):
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

    def _update_client_identity(self, params: list, conn) -> None:
        """Extract optional RequesterDeviceName/Type from GET_DEVICES params."""
        if len(params) <= 4:
            return
        client_id = self._client_id_for(conn)
        if client_id < 0:
            return
        name = params[4].strip() if len(params) > 4 else ""
        rtype = params[5].strip() if len(params) > 5 else "unknown"
        if name:
            self._client_meta[client_id] = {"name": name, "type": rtype}
            addr = self._client_addrs.get(client_id, "")
            self._logger.info(
                f"Client #{client_id} identified ({rtype}: {name})"
            )

    # ========================================================================
    # Message Handlers
    # ========================================================================
    def read(self) -> None:
        """Read and process all pending messages from downstream clients.

        When upstreams exist, uses hub-style command handling (forwarding
        to upstreams).  When no upstreams, uses simpler server-style
        processing.
        """
        messages = self._tcp_read()

        for command, params, conn in messages:
            self._commanding_client = conn

            if self._upstreams:
                # Hub-style command handling
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
                    if params:
                        full_cmd = f"{command},{','.join(str(p) for p in params)}"
                    else:
                        full_cmd = command

                    if command == "BLAECK.WRITE_DATA":
                        # One-shot: forward to relayed upstreams only
                        for upstream in self._upstreams:
                            if upstream.relay_downstream and upstream.transport.connected:
                                upstream.transport.send_command(full_cmd)
                    else:
                        # ACTIVATE/DEACTIVATE: only forward to client-managed relayed upstreams
                        for upstream in self._upstreams:
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

            else:
                # Simple server-style command handling (no upstreams)
                if command == "BLAECK.WRITE_SYMBOLS":
                    self.write_symbols(self._decode_four_byte(params))

                elif command == "BLAECK.WRITE_DATA":
                    self.write_all_data(self._decode_four_byte(params))

                elif command == "BLAECK.GET_DEVICES":
                    self._update_client_identity(params, conn)
                    self.write_devices(self._decode_four_byte(params))

                elif command == "BLAECK.ACTIVATE":
                    if self._fixed_interval_ms == IntervalMode.CLIENT:
                        self._set_timed_data(True, self._decode_four_byte(params))

                elif command == "BLAECK.DEACTIVATE":
                    if self._fixed_interval_ms == IntervalMode.CLIENT:
                        self._set_timed_data(False)

            # Dispatch to specific command handler
            handler = self._command_handlers.get(command)
            if handler is not None:
                handler(*params)

            # Forward custom commands to opted-in upstreams
            if (
                command not in self._non_forwarded_commands
                and not command.startswith("BLAECK.")
            ):
                if params:
                    full_cmd = f"{command},{','.join(str(p) for p in params)}"
                else:
                    full_cmd = command
                for upstream in self._upstreams:
                    fcc = upstream.forward_custom_commands
                    if not fcc or not upstream.transport.connected:
                        continue
                    if isinstance(fcc, list) and command not in fcc:
                        continue
                    upstream.transport.send_command(full_cmd)

            # Fire catch-all callback
            if self._read_callback is not None:
                self._read_callback(command, *params)

    # ========================================================================
    # Message Writers
    # ========================================================================
    def write_symbols(self, msg_id: int = 1) -> None:
        """Send symbol list to connected clients."""
        if not self.connected:
            return

        header = self.MSG_SYMBOL_LIST + b":" + msg_id.to_bytes(4, "little") + b":"

        if self._upstreams:
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
            for upstream in self._upstreams:
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

        if self._upstreams:
            # Hub-style: master + slave devices
            for client_id, conn in list(self._clients.items()):
                payload = (
                    _MSC_MASTER
                    + b"\x00"  # SlaveID 0 for master
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
                    + self._client_meta.get(client_id, {}).get("name", "").encode()
                    + b"\0"
                    + self._client_meta.get(client_id, {}).get("type", "unknown").encode()
                    + b"\0"
                    + (b"1" if self._server_restarted else b"0")
                    + b"\0"
                    + b"hub\0"
                    + b"0\0"  # parent (master references itself)
                )

                # Upstream devices as slaves — only relayed upstreams
                for upstream in self._upstreams:
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
                        payload += (
                            _MSC_SLAVE
                            + bytes([hub_sid])
                            + info.device_name.encode()
                            + b"\0"
                            + info.hw_version.encode()
                            + b"\0"
                            + info.fw_version.encode()
                            + b"\0"
                            + info.lib_version.encode()
                            + b"\0"
                            + info.lib_name.encode()
                            + b"\0"
                            + str(client_id).encode()
                            + b"\0"
                            + (b"1" if client_id in self.data_clients else b"0")
                            + b"\0"
                            + self._client_meta.get(client_id, {}).get("name", "").encode()
                            + b"\0"
                            + self._client_meta.get(client_id, {}).get("type", "unknown").encode()
                            + b"\0"
                            + (info.server_restarted.encode() if info.server_restarted else b"0")
                            + b"\0"
                            + device_type.encode()
                            + b"\0"
                            + str(parent_sid).encode()
                            + b"\0"
                        )

                data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"

                try:
                    conn.sendall(data)
                except OSError as e:
                    self._logger.debug(f"Send error: {e}")
                    self._disconnect_client(conn)
        else:
            # Simple server-style
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
                    + self._client_meta.get(client_id, {}).get("name", "").encode()
                    + b"\0"
                    + self._client_meta.get(client_id, {}).get("type", "unknown").encode()
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
                    self._logger.debug(f"Send error: {e}")
                    self._disconnect_client(conn)

        self._server_restarted = False

        # Clear upstream server_restarted after sending to prevent stale values
        for upstream in self._upstreams:
            for info in upstream.device_infos:
                if info.server_restarted == "1":
                    info.server_restarted = "0"

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
            self._logger.info(
                f"Local signal interval set ({value} ms) — client control locked"
            )
        elif value == IntervalMode.OFF:
            self._fixed_interval_ms = IntervalMode.OFF
            self._timed_activated = False
            self._timer.deactivate()
            self._logger.info("Local signal interval set (OFF) — client control locked")
        elif value == IntervalMode.CLIENT:
            self._fixed_interval_ms = IntervalMode.CLIENT
            self._logger.info("Local signal interval set (CLIENT) — client controlled")

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
    # Upstream Polling
    # ========================================================================
    def _poll_upstreams(self) -> None:
        """Read frames from all upstream devices, update signals, and relay."""
        if not self._upstreams:
            return

        for upstream in self._upstreams:
            if not upstream.transport.connected:
                if upstream.connected:
                    upstream.connected = False
                    upstream._reconnecting = False
                    upstream._awaiting_symbols = False
                    upstream._awaiting_devices = False
                    self._zero_upstream_signals(upstream)
                    self._send_upstream_lost_frame(upstream)
                    if self._upstream_disconnect_callback is not None:
                        self._upstream_disconnect_callback(upstream.device_name)

                # Attempt auto-reconnect on cooldown
                if upstream.auto_reconnect:
                    now = time.time()
                    if now >= upstream._reconnect_cooldown:
                        upstream._reconnect_cooldown = now + 5.0
                        if upstream.transport.connect(timeout=2.0):
                            upstream.connected = True
                            upstream._reconnecting = True
                            # Same pattern as initial discovery: DEACTIVATE, then
                            # send commands and poll for device info with retries
                            upstream.transport.send_command("BLAECK.DEACTIVATE")
                            upstream.transport.send_command("BLAECK.WRITE_SYMBOLS")
                            identity = f",0,0,0,0,{self._device_name.decode()},hub"
                            upstream.transport.send_command(
                                f"BLAECK.GET_DEVICES{identity}"
                            )
                            device_frame = None
                            for i in range(20):  # up to 2 s
                                time.sleep(0.1)
                                frames = upstream.transport.read_frames()
                                for f in frames:
                                    if len(f) > 0 and f[0] in decoder.MSGKEY_DEVICES_ALL:
                                        device_frame = f
                                        break
                                if device_frame is not None:
                                    break
                                if i > 0 and i % 10 == 0:
                                    upstream.transport.send_command(
                                        f"BLAECK.GET_DEVICES{identity}"
                                    )
                            if device_frame is not None:
                                restart_detected = False
                                try:
                                    infos = decoder.parse_all_devices(device_frame)
                                    if infos:
                                        upstream.device_infos = infos
                                        self._rebuild_slave_id_map(upstream)
                                        for info in infos:
                                            if info.server_restarted == "1":
                                                restart_detected = True
                                                self._logger.info(
                                                    f"Upstream '{upstream.device_name}' restart detected via device info"
                                                )
                                except Exception as e:
                                    self._logger.warning(
                                        f"Device info processing for "
                                        f"'{upstream.device_name}': {e}"
                                    )
                            else:
                                restart_detected = False
                            if upstream.transport.connected:
                                self._finalize_reconnect(upstream, restart_detected)
                            else:
                                upstream.connected = False
                                upstream._reconnecting = False
                        else:
                            self._logger.debug(
                                f"Upstream '{upstream.device_name}' reconnect attempt failed"
                            )
                continue

            frames = upstream.transport.read_frames()

            # Detect disconnect that happened during read
            if upstream.connected and not upstream.transport.connected:
                upstream.connected = False
                upstream._reconnecting = False
                upstream._awaiting_symbols = False
                upstream._awaiting_devices = False
                self._zero_upstream_signals(upstream)
                self._send_upstream_lost_frame(upstream)
                if self._upstream_disconnect_callback is not None:
                    self._upstream_disconnect_callback(upstream.device_name)
                continue

            for frame in frames:
                if len(frame) == 0:
                    continue
                msg_key = frame[0]

                # Handle B0 symbol list during re-discovery or reconnect
                if msg_key == decoder.MSGKEY_SYMBOL_LIST and (upstream.schema_stale or upstream._awaiting_symbols):
                    try:
                        new_symbols = decoder.parse_symbol_list(frame)
                        if new_symbols:
                            self._rebuild_upstream_schema(upstream, new_symbols)
                            self._rebuild_slave_id_map(upstream)
                            if upstream.schema_stale:
                                # Request device info to update slave_id_map
                                identity = f",0,0,0,0,{self._device_name.decode()},hub"
                                upstream.transport.send_command(
                                    f"BLAECK.GET_DEVICES{identity}"
                                )
                            upstream.schema_stale = False
                            self._logger.info(
                                f"Schema refreshed for '{upstream.device_name}': "
                                f"{len(new_symbols)} signals"
                            )
                    except Exception as e:
                        self._logger.warning(
                            f"Schema re-discovery failed for "
                            f"'{upstream.device_name}': {e}"
                        )
                    # Always clear flag and finalize, even if parsing failed
                    if upstream._awaiting_symbols:
                        upstream._awaiting_symbols = False
                        if upstream._reconnecting and not upstream._awaiting_devices:
                            self._finalize_reconnect(upstream)
                    continue

                # Handle device info frames (update device_infos + slave_id_map)
                if msg_key in decoder.MSGKEY_DEVICES_ALL:
                    try:
                        infos = decoder.parse_all_devices(frame)
                        if infos:
                            upstream.device_infos = infos
                            self._rebuild_slave_id_map(upstream)
                            # Detect upstream restart from device info
                            for info in infos:
                                if info.server_restarted == "1":
                                    if upstream._initial_restart_seen:
                                        self._send_upstream_restarted_frame(upstream)
                                        self._resend_activate(upstream)
                                        self._logger.info(
                                            f"Upstream '{upstream.device_name}' restart detected via device info"
                                        )
                                    else:
                                        upstream._initial_restart_seen = True
                    except Exception as e:
                        self._logger.warning(
                            f"Device info processing for "
                            f"'{upstream.device_name}': {e}"
                        )
                    # Always clear flag and finalize, even if parsing failed
                    if upstream._awaiting_devices:
                        upstream._awaiting_devices = False
                        if not upstream._awaiting_symbols:
                            self._finalize_reconnect(upstream)
                    continue

                # Forward upstream restart notification (0xC0) to downstream
                if msg_key == 0xC0:
                    if upstream._initial_restart_seen:
                        self._send_upstream_restarted_frame(upstream)
                        self._resend_activate(upstream)
                        self._logger.info(
                            f"Upstream '{upstream.device_name}' restarted (0xC0)"
                        )
                    else:
                        upstream._initial_restart_seen = True
                    continue

                if msg_key in decoder.MSGKEY_DATA_ALL:
                    # Skip data frames while re-discovering schema
                    if upstream.schema_stale:
                        continue

                    try:
                        decoded = decoder.parse_data(frame, upstream.symbol_table)

                        # Schema hash check (D2 frames)
                        if (
                            msg_key == decoder.MSGKEY_DATA_D2
                            and decoded.schema_hash != upstream.expected_schema_hash
                        ):
                            upstream.schema_stale = True
                            self._logger.warning(
                                f"Schema change detected on '{upstream.device_name}' "
                                f"(hash {decoded.schema_hash:#06x} != "
                                f"{upstream.expected_schema_hash:#06x}), "
                                f"requesting re-discovery"
                            )
                            upstream.transport.send_command("BLAECK.WRITE_SYMBOLS")
                            continue

                        # Signal count check (D1/B1 fallback)
                        if (
                            msg_key != decoder.MSGKEY_DATA_D2
                            and len(decoded.signals) != len(upstream.symbol_table)
                        ):
                            upstream.schema_stale = True
                            self._logger.warning(
                                f"Signal count mismatch on '{upstream.device_name}' "
                                f"({len(decoded.signals)} != "
                                f"{len(upstream.symbol_table)}), "
                                f"requesting re-discovery"
                            )
                            upstream.transport.send_command("BLAECK.WRITE_SYMBOLS")
                            continue

                        # Relay upstream restart flag downstream.
                        # Suppress the first restart flag from each upstream —
                        # it's expected after initial connection and the hub's
                        # own restart flag already covers the fresh start.
                        if decoded.restart_flag:
                            if not upstream._initial_restart_seen:
                                upstream._initial_restart_seen = True
                                self._logger.debug(
                                    f"Upstream '{upstream.device_name}' initial restart flag suppressed"
                                )
                            elif upstream._restart_c0_sent:
                                # Already notified via 0xC0 — consume silently
                                upstream._restart_c0_sent = False
                                self._logger.debug(
                                    f"Upstream '{upstream.device_name}' restart flag consumed (0xC0 already sent)"
                                )
                            else:
                                self._send_upstream_restarted_frame(upstream)
                                self._resend_activate(upstream)
                                self._logger.info(
                                    f"Upstream '{upstream.device_name}' restart flag relayed via 0xC0"
                                )

                        if not upstream.relay_downstream:
                            # Non-relayed: update internal signals only
                            for sig_id, value in decoded.signals.items():
                                idx = upstream.index_map.get(sig_id)
                                if idx is not None and idx < len(upstream._signals):
                                    upstream._signals[idx].value = value
                            try:
                                self._fire_data_received(upstream)
                            except Exception as e:
                                self._logger.warning(
                                    f"on_data_received callback error for "
                                    f"'{upstream.device_name}': {e}"
                                )
                            continue

                        for sig_id, value in decoded.signals.items():
                            hub_idx = upstream.index_map.get(sig_id)
                            if hub_idx is not None and hub_idx < len(self.signals):
                                self.signals[hub_idx].value = value
                                self.signals[hub_idx].updated = True

                        # Fire callback before relay so transforms can run
                        try:
                            self._fire_data_received(upstream)
                        except Exception as e:
                            self._logger.warning(
                                f"on_data_received callback error for "
                                f"'{upstream.device_name}': {e}"
                            )

                        # Forward upstream timestamp only with a single relayed device
                        relayed_count = sum(1 for u in self._upstreams if u.relay_downstream)
                        single = relayed_count == 1 and self._local_signal_count == 0
                        # Widen upstream uint32 timestamp to uint64
                        ts = decoded.timestamp if single and decoded.timestamp is not None else None
                        try:
                            ts_mode = TimestampMode(decoded.timestamp_mode) if ts is not None else None
                        except ValueError:
                            ts = None
                            ts_mode = None

                        # Replace msg_id only when hub overrides BLAECK.ACTIVATE
                        relay_msg_id = decoded.msg_id
                        if upstream.interval_ms >= 0 and relay_msg_id == _MSG_ID_ACTIVATE:
                            relay_msg_id = _MSG_ID_HUB

                        # Determine this upstream's signal index range
                        hub_indices = sorted(upstream.index_map.values())
                        if not hub_indices:
                            continue
                        start_idx = hub_indices[0]
                        end_idx = hub_indices[-1]
                        header = (
                            self.MSG_DATA
                            + b":"
                            + relay_msg_id.to_bytes(4, "little")
                            + b":"
                        )
                        relay_data = (
                            b"<BLAECK:"
                            + self._build_data_msg(
                                header,
                                start=start_idx,
                                end=end_idx,
                                only_updated=True,
                                timestamp=ts,
                                timestamp_mode=ts_mode,
                                status=decoded.status_byte,
                                status_payload=decoded.status_payload,
                            )
                            + b"/BLAECK>\r\n"
                        )
                        self._tcp_send_data(relay_data)
                    except Exception as e:
                        self._logger.warning(
                            f"Upstream '{upstream.device_name}' frame dropped: {e}"
                        )

    def _fire_data_received(self, upstream: _UpstreamDevice) -> None:
        """Invoke all matching on_data_received callbacks."""
        for name_filter, func in self._data_received_callbacks:
            if name_filter is None or name_filter == upstream.device_name:
                func(upstream)

    def _zero_upstream_signals(self, upstream: _UpstreamDevice) -> None:
        """Reset all signals from a disconnected upstream to zero."""
        if upstream.relay_downstream:
            for hub_idx in upstream.index_map.values():
                if hub_idx < len(self.signals):
                    self.signals[hub_idx].value = 0
                    self.signals[hub_idx].updated = True
        else:
            for sig in upstream._signals:
                sig.value = 0

    def _rebuild_upstream_schema(
        self, upstream: _UpstreamDevice, new_symbols: list[decoder.DecodedSymbol]
    ) -> None:
        """Rebuild an upstream's signals after schema change.

        Removes all upstream signals from self.signals, then re-adds them
        from fresh symbol tables. Rebuilds index_map for all upstreams.
        """
        upstream.symbol_table = new_symbols
        upstream.expected_schema_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in new_symbols]
        )

        # Remove ALL upstream signals and rebuild from scratch
        del self.signals[self._local_signal_count:]

        offset = self._local_signal_count
        for u in self._upstreams:
            u._signals = []
            u.index_map = {}
            if u.relay_downstream:
                for i, sym in enumerate(u.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    sig = Signal(sym.name, sig_type)
                    self.signals.append(sig)
                    u._signals.append(self.signals[offset])
                    u.index_map[i] = offset
                    offset += 1
            else:
                for i, sym in enumerate(u.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    sig = Signal(sym.name, sig_type)
                    u._signals.append(sig)
                    u.index_map[i] = i
            u._upstream_signals = SignalList(u._signals)

        self._update_schema_hash()

    def _rebuild_slave_id_map(self, upstream: _UpstreamDevice) -> None:
        """Rebuild slave_id_map for one upstream from its current data."""
        if not upstream.relay_downstream:
            return
        hub_slave_idx = 0
        for u in self._upstreams:
            if u is not upstream and u.relay_downstream and u.slave_id_map:
                hub_slave_idx = max(hub_slave_idx, max(u.slave_id_map.values()))
        seen: dict[tuple[int, int], int] = {}
        for sym in upstream.symbol_table:
            key = (sym.msc, sym.slave_id)
            if key not in seen:
                hub_slave_idx += 1
                seen[key] = hub_slave_idx
        for info in upstream.device_infos:
            key = (info.msc, info.slave_id)
            if key not in seen:
                hub_slave_idx += 1
                seen[key] = hub_slave_idx
        upstream.slave_id_map = seen

    def _send_upstream_lost_frame(self, upstream: _UpstreamDevice) -> None:
        """Send one data frame with STATUS_UPSTREAM_LOST for a disconnected upstream."""
        if not upstream.relay_downstream or not self.connected:
            return
        hub_indices = sorted(upstream.index_map.values())
        if not hub_indices:
            return
        start_idx = hub_indices[0]
        end_idx = hub_indices[-1]
        # StatusPayload[0]: 0x01 = auto-reconnect enabled
        auto_reconnect_byte = b"\x01" if upstream.auto_reconnect else b"\x00"
        status_payload = auto_reconnect_byte + b"\x00\x00\x00"
        header = (
            self.MSG_DATA
            + b":"
            + _MSG_ID_HUB.to_bytes(4, "little")
            + b":"
        )
        data = (
            b"<BLAECK:"
            + self._build_data_msg(
                header,
                start=start_idx,
                end=end_idx,
                only_updated=True,
                status=STATUS_UPSTREAM_LOST,
                status_payload=status_payload,
            )
            + b"/BLAECK>\r\n"
        )
        self._tcp_send_data(data)

    def _send_upstream_reconnected_frame(self, upstream: _UpstreamDevice) -> None:
        """Send one data frame with STATUS_UPSTREAM_RECONNECTED for a reconnected upstream."""
        if not upstream.relay_downstream or not self.connected:
            return
        hub_indices = sorted(upstream.index_map.values())
        if not hub_indices:
            return
        # Mark signals as updated so they're included in the frame
        for hub_idx in hub_indices:
            self.signals[hub_idx].updated = True
        start_idx = hub_indices[0]
        end_idx = hub_indices[-1]
        header = (
            self.MSG_DATA
            + b":"
            + _MSG_ID_HUB.to_bytes(4, "little")
            + b":"
        )
        data = (
            b"<BLAECK:"
            + self._build_data_msg(
                header,
                start=start_idx,
                end=end_idx,
                only_updated=True,
                status=STATUS_UPSTREAM_RECONNECTED,
            )
            + b"/BLAECK>\r\n"
        )
        self._tcp_send_data(data)

    def _send_upstream_restarted_frame(self, upstream: _UpstreamDevice) -> None:
        """Build and send a 0xC0 restart frame with the upstream's device name."""
        if not self.connected:
            return
        # Frame format: MSGKEY(C0) : MSGID(4) : MSC + SlaveID + Name\0 + HW\0 + FW\0 + LibVersion\0 + LibName\0
        info = upstream.device_infos[0] if upstream.device_infos else None
        name = (info.device_name if info else upstream.device_name).encode()
        hw = (info.hw_version.encode() if info else b"")
        fw = (info.fw_version.encode() if info else b"")
        lib_ver = (info.lib_version.encode() if info else b"")
        lib_name = (info.lib_name.encode() if info else b"")
        payload = (
            b"\xC0:\x01\x00\x00\x00:"
            + _MSC_SLAVE
            + b"\x01"
            + name + b"\0"
            + hw + b"\0"
            + fw + b"\0"
            + lib_ver + b"\0"
            + lib_name + b"\0"
        )
        data = b"<BLAECK:" + payload + b"/BLAECK>\r\n"
        self._tcp_send_data(data)
        upstream._restart_c0_sent = True

    def _resend_activate(self, upstream: _UpstreamDevice) -> None:
        """Re-send ACTIVATE/DEACTIVATE after upstream restart or reconnect."""
        if upstream.interval_ms >= 0:
            b = upstream.interval_ms.to_bytes(4, "little")
            params = ",".join(str(x) for x in b)
            upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}")
            self._logger.info(
                f"Upstream '{upstream.device_name}' re-activated ({upstream.interval_ms} ms)"
            )
        elif upstream.interval_ms == IntervalMode.OFF:
            upstream.transport.send_command("BLAECK.DEACTIVATE")
        elif upstream.interval_ms == IntervalMode.CLIENT and self._last_client_activate_cmd:
            upstream.transport.send_command(self._last_client_activate_cmd)
            self._logger.info(
                f"Upstream '{upstream.device_name}' client interval restored"
            )

    def _finalize_reconnect(self, upstream: _UpstreamDevice, restart_detected: bool = False) -> None:
        """Complete reconnect: notify downstream, re-send ACTIVATE, then report restart."""
        upstream._reconnecting = False
        # 512: notify downstream that upstream is back
        self._send_upstream_reconnected_frame(upstream)
        self._resend_activate(upstream)
        # 510: report restart after reconnect is confirmed
        if restart_detected:
            self._send_upstream_restarted_frame(upstream)
        self._logger.info(
            f"Upstream '{upstream.device_name}' reconnected"
        )

    def _rebuild_upstream_indices(self) -> None:
        """Rebuild relayed upstream index_map from current signals list."""
        offset = self._local_signal_count
        for upstream in self._upstreams:
            if upstream.relay_downstream:
                for k in range(len(upstream._signals)):
                    upstream.index_map[k] = offset + k
                offset += len(upstream._signals)

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

        Args:
            header: Pre-built header bytes (msg_key:msg_id:)
            start: First signal index (inclusive)
            end: Last signal index (inclusive), -1 = last signal
            only_updated: If True, include only signals with updated=True
            timestamp: Timestamp in microseconds (uint64), or None
            timestamp_mode: Timestamp mode byte. If None, uses the
                instance's :attr:`timestamp_mode`.
            status: Status byte (STATUS_OK, STATUS_UPSTREAM_LOST, etc.)
            status_payload: 4-byte status payload forwarded from upstream.
        """
        if end == -1:
            end = len(self.signals) - 1
        if len(status_payload) != 4:
            raise ValueError(
                f"status_payload must be 4 bytes, got {len(status_payload)}"
            )

        # Restart flag
        restart_flag = b"\x01" if self._restart_flag_pending else b"\x00"
        self._restart_flag_pending = False

        # Schema hash
        schema_hash = self._schema_hash.to_bytes(2, "little")

        # Timestamp
        mode = timestamp_mode if timestamp_mode is not None else self._timestamp_mode
        if timestamp is not None and mode != TimestampMode.NONE:
            mode_byte = int(mode).to_bytes(1, "little")
            meta = (
                restart_flag
                + b":"
                + schema_hash
                + b":"
                + mode_byte
                + timestamp.to_bytes(8, "little")
                + b":"
            )
        else:
            meta = restart_flag + b":" + schema_hash + b":" + b"\x00" + b":"

        payload = b""
        for idx in range(start, end + 1):
            sig = self.signals[idx]
            if only_updated and not sig.updated:
                continue
            payload += idx.to_bytes(2, "little") + sig.to_bytes()
            if only_updated:
                sig.updated = False

        frame_no_crc = header + meta + payload + status.to_bytes(1, "little") + status_payload
        crc = binascii.crc32(frame_no_crc).to_bytes(4, "little")

        return frame_no_crc + crc

    def _get_symbols(self) -> bytes:
        """Build symbol list message (simple server mode)."""
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
            for u in self._upstreams:
                if u.device_name == name:
                    return _status(u)
            raise KeyError(f"No upstream named '{name}'")

        return {u.device_name: _status(u) for u in self._upstreams}

    def __getitem__(self, name: str) -> _UpstreamDevice:
        """Access an upstream device by name.

        Example::

            bltcp["Arduino"]["temperature"].value
            bltcp["Arduino"].signals[0].value
        """
        for upstream in self._upstreams:
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
        for upstream in self._upstreams:
            if upstream.transport.connected:
                upstream.transport.send_command("BLAECK.DEACTIVATE")
            upstream.transport.close()

        # Close downstream connections
        if hasattr(self, "_clients"):
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
        if hasattr(self, "_sel"):
            try:
                self._sel.unregister(self._server_socket)
            except Exception:
                pass
            self._sel.close()
        if hasattr(self, "_server_socket"):
            self._server_socket.close()

        self._logger.info("Server closed")

    def __enter__(self):
        """Enable 'with' statement usage."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up on exit."""
        self.close()

    def __repr__(self):
        if hasattr(self, "_clients"):
            n = len(self._clients)
        else:
            n = 0
        clients = f"{n} client{'s' if n != 1 else ''}"
        active = "active" if self._timed_activated else "inactive"
        n_up = len(self._upstreams)
        if n_up:
            return (
                f"blaecktcpy [{clients}] [{active}] "
                f"({len(self.signals)} signals, {n_up} upstreams)"
            )
        return f"blaecktcpy [{clients}] [{active}] ({len(self.signals)} signals)"
