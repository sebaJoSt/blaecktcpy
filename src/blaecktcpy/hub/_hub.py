"""BlaeckHub — multi-device signal aggregator.

Connects to multiple upstream BlaeckTCP(y)/BlaeckSerial devices,
discovers their signals, decodes incoming data frames, and serves
all signals as a single merged device to Loggbok via a BlaeckServer
downstream server.

Example::

    from blaecktcpy import BlaeckHub

    hub = BlaeckHub("0.0.0.0", 23, "My Hub", "Python", "1.0")
    hub.add_tcp("ESP32", "192.168.1.10", 23)
    hub.add_tcp("Python", "127.0.0.1", 24)
    hub.start()

    while True:
        hub.tick()
"""

import logging
import time
from dataclasses import dataclass, field

from .._signal import Signal
from .._server import BlaeckServer, LIB_VERSION, LIB_NAME, STATUS_UPSTREAM_LOST
from . import _decoder as decoder
from ._upstream import UpstreamTCP, UpstreamSerial, _UpstreamBase

# MasterSlaveConfig byte values
_MSC_MASTER = b"\x01"
_MSC_SLAVE = b"\x02"

# Message IDs for data frames
_MSG_ID_ACTIVATE = 185273099  # 0x0B0B0B0B — client-controlled (BLAECK.ACTIVATE)
_MSG_ID_HUB = 185273100  # 0x0B0B0B0C — hub-overridden interval

logger = logging.getLogger("blaecktcpy")


class UpstreamSignals:
    """Signal collection for an upstream device.

    Supports access by index (int) or signal name (str)::

        upstream.signals[0].value
        upstream.signals["temperature"].value
    """

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals
        self._name_map: dict[str, int] = {}
        for i, sig in enumerate(signals):
            self._name_map[sig.signal_name] = i

    def __getitem__(self, key: int | str) -> Signal:
        if isinstance(key, int):
            return self._signals[key]
        if isinstance(key, str):
            idx = self._name_map.get(key)
            if idx is None:
                raise KeyError(f"No signal named {key!r}")
            return self._signals[idx]
        raise TypeError(f"Expected int or str, got {type(key).__name__}")

    def __len__(self) -> int:
        return len(self._signals)

    def __iter__(self):
        return iter(self._signals)

    def __repr__(self) -> str:
        return f"UpstreamSignals({self._signals!r})"

    def _rebuild_name_map(self) -> None:
        """Rebuild after signals list is populated (e.g. during start)."""
        self._name_map.clear()
        for i, sig in enumerate(self._signals):
            self._name_map[sig.signal_name] = i


@dataclass
class _UpstreamDevice:
    """Internal bookkeeping for one upstream connection."""

    name: str
    transport: _UpstreamBase
    symbol_table: list[decoder.DecodedSymbol] = field(default_factory=list)
    index_map: dict[int, int] = field(default_factory=dict)
    device_info: decoder.DecodedDeviceInfo | None = None
    interval_ms: int = 0
    was_connected: bool = True
    relay: bool = True
    _signals: list[Signal] = field(default_factory=list)
    _upstream_signals: UpstreamSignals | None = field(default=None, repr=False)

    @property
    def signals(self) -> UpstreamSignals:
        if self._upstream_signals is None:
            raise RuntimeError(
                "Signals not available yet — call hub.start() first"
            )
        return self._upstream_signals

    def __getitem__(self, key: int | str) -> Signal:
        return self.signals[key]


class BlaeckHub:
    """Aggregates signals from multiple upstream BlaeckTCP(y) devices.

    Connects to upstream devices during setup, discovers their signals,
    then serves a merged signal list to downstream clients (e.g. Loggbok)
    via an embedded BlaeckServer server.
    """

    def __init__(
        self,
        ip: str,
        port: int,
        device_name: str,
        device_hw_version: str,
        device_fw_version: str,
    ):
        self._device_name = device_name
        self._device_hw_version = device_hw_version
        self._device_fw_version = device_fw_version
        self._ip = ip
        self._port = port

        self._upstreams: list[_UpstreamDevice] = []
        self._local_signals: list[Signal] = []
        self._server: BlaeckServer | None = None
        self._started = False

        self._local_interval_ms: int = 0
        self._local_fixed_interval: bool = False
        self._local_timer_base: int = 0
        self._local_timer_setpoint: float = 0
        self._local_first_time: bool = True

        self._disconnect_callback = None
        self._client_connect_callback = None
        self._client_disconnect_callback = None
        self._data_received_callbacks: list[tuple[str | None, object]] = []
        self._command_handlers: dict[str, object] = {}
        self._command_catchall = None

    # ====================================================================
    # Setup — call before start()
    # ====================================================================

    def set_local_interval(self, interval_ms: int) -> None:
        """Set a fixed sending rate for local signals.

        When set, local signals are sent at this hub-managed interval
        and downstream clients cannot override the rate via ACTIVATE.
        Without this call, local signals follow the downstream client's
        ACTIVATE/DEACTIVATE commands — just like upstreams without
        ``interval_ms``.

        Must be called before :meth:`start`.

        Args:
            interval_ms: Interval in milliseconds.
        """
        if self._started:
            raise RuntimeError("Cannot set local interval after start()")
        self._local_interval_ms = interval_ms
        self._local_fixed_interval = True

    def add_signal(
        self,
        name: str,
        datatype: str,
        value: int | float = 0,
    ) -> Signal:
        """Add a local signal to the hub.

        Local signals appear before upstream signals in the signal list.
        Must be called before :meth:`start`.

        Args:
            name: Signal name
            datatype: Signal datatype (e.g. 'float', 'int', 'bool')
            value: Initial value

        Returns:
            The Signal object. Update its ``.value`` and ``.updated``
            attributes in your main loop.
        """
        if self._started:
            raise RuntimeError("Cannot add signals after start()")
        sig = Signal(name, datatype, value)
        self._local_signals.append(sig)
        return sig

    def add_tcp(
        self,
        ip: str,
        port: int,
        name: str = "",
        timeout: float = 5.0,
        interval_ms: int = 0,
        relay: bool = True,
    ) -> _UpstreamDevice:
        """Connect to an upstream TCP device and discover its signals.

        Blocks until the symbol table is fetched or timeout expires.
        Must be called before :meth:`start`.

        Args:
            ip: IP address of the upstream device
            port: TCP port of the upstream device
            name: Optional friendly name; defaults to upstream device name
            timeout: Connection and discovery timeout in seconds
            interval_ms: If > 0, activate timed data at this interval on
                start.  The downstream client cannot override this rate.
            relay: If False, signals are decoded hub-side but not exposed
                to downstream clients (no symbols, devices, or data).

        Returns:
            Upstream handle for accessing signal values.
        """
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")

        label = name or f"{ip}:{port}"
        transport = UpstreamTCP(label, ip, port)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay)

    def add_serial(
        self,
        port: str,
        baudrate: int = 115200,
        name: str = "",
        timeout: float = 5.0,
        dtr: bool = True,
        interval_ms: int = 0,
        relay: bool = True,
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
            interval_ms: If > 0, activate timed data at this interval on
                start.  The downstream client cannot override this rate.
            relay: If False, signals are decoded hub-side but not exposed
                to downstream clients (no symbols, devices, or data).

        Returns:
            Upstream handle for accessing signal values.
        """
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")

        label = name or port
        transport = UpstreamSerial(label, port, baudrate, dtr)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay)

    def _discover_upstream(
        self,
        name: str,
        transport: _UpstreamBase,
        timeout: float,
        interval_ms: int = 0,
        relay: bool = True,
    ) -> _UpstreamDevice:
        """Connect and fetch the symbol table from an upstream device."""
        label = transport.name  # temporary label for error messages

        if not transport.connect(timeout):
            raise ConnectionError(
                f"Failed to connect to upstream '{label}': {transport.last_error}"
            )

        # Stop any ongoing timed data transmission
        transport.send_command("BLAECK.DEACTIVATE")

        # Poll for symbol list with periodic retries (like Loggbok)
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
            # Retry every 1 second
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
            name=name, transport=transport, interval_ms=interval_ms, relay=relay
        )
        upstream.symbol_table = symbols

        # Fetch device info with polling retries
        device_msgkeys = decoder.MSGKEY_DEVICES_ALL
        transport.send_command("BLAECK.GET_DEVICES")
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
                transport.send_command("BLAECK.GET_DEVICES")

        if frame is not None:
            try:
                upstream.device_info = decoder.parse_devices(frame)
            except Exception as e:
                logger.debug(f"Upstream '{label}' device info parse error: {e}")

        # Use upstream device name if no name was provided
        if not name and upstream.device_info:
            upstream.name = upstream.device_info.device_name

        self._upstreams.append(upstream)

        logger.info(f"Upstream '{upstream.name}': {len(symbols)} signals discovered")
        return upstream

    # ====================================================================
    # Lifecycle
    # ====================================================================

    def start(self) -> None:
        """Create the downstream server and freeze the signal list.

        Call after all :meth:`add_tcp` / :meth:`add_serial`
        calls.  The signal list cannot change after this point.

        Upstreams with a fixed ``interval_ms`` are activated
        automatically.
        """
        if self._started:
            raise RuntimeError("Already started")

        self._server = BlaeckServer(
            self._ip,
            self._port,
            self._device_name,
            self._device_hw_version,
            self._device_fw_version,
        )

        # Register local signals first
        for sig in self._local_signals:
            self._server.add_signal(sig)

        # Register upstream signals and build index maps
        offset = len(self._local_signals)
        for upstream in self._upstreams:
            if upstream.relay:
                # Relayed: register on server so Loggbok sees them
                for i, sym in enumerate(upstream.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    self._server.add_signal(sym.name, sig_type)
                    upstream._signals.append(self._server.signals[offset])
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
            upstream._upstream_signals = UpstreamSignals(upstream._signals)

        self._started = True

        # Wire up server client callbacks
        if self._client_connect_callback is not None:
            self._server._connect_callback = self._client_connect_callback
        if self._client_disconnect_callback is not None:
            self._server._disconnect_callback = self._client_disconnect_callback

        # Wire up command handlers
        for cmd, handler in self._command_handlers.items():
            self._server._command_handlers[cmd] = handler
        if self._command_catchall is not None:
            self._server._read_callback = self._command_catchall

        # Activate upstreams with a fixed interval
        for upstream in self._upstreams:
            if upstream.interval_ms > 0:
                b = upstream.interval_ms.to_bytes(4, "little")
                params = ",".join(str(x) for x in b)
                upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}")

        total = len(self._server.signals)
        logger.info(f"BlaeckHub started with {total} signals")

    def close(self) -> None:
        """Shut down all upstream connections and the downstream server."""
        for upstream in self._upstreams:
            upstream.transport.close()
        if self._server:
            self._server.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ====================================================================
    # Main loop
    # ====================================================================

    def tick(self) -> None:
        """Main loop tick — poll upstreams, serve downstream.

        Call this repeatedly in your main loop.
        Reads commands from downstream clients and forwards them to upstreams.
        Reads data from upstreams and immediately relays updated signals downstream.
        """
        if not self._started or self._server is None:
            raise RuntimeError("Must call start() before tick()")
        self._read()
        self._poll_upstreams()
        self._tick_local()

    def _read(self) -> None:
        """Simplified read — no built-in data/timer handling.

        Handles WRITE_SYMBOLS and GET_DEVICES locally.
        Forwards ACTIVATE, DEACTIVATE, and WRITE_DATA to upstreams.
        """
        messages = self._server._tcp_read()

        if messages:
            logger.debug(f"_read() received {len(messages)} message(s)")
        for command, params, conn in messages:
            logger.debug(f"  command={command!r} params={params!r}")
            self._server._active_client = conn

            if command == "BLAECK.WRITE_SYMBOLS":
                self._write_symbols(self._server._decode_four_byte(params))

            elif command == "BLAECK.GET_DEVICES":
                self._write_devices(self._server._decode_four_byte(params))

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
                        if upstream.relay and upstream.transport.connected:
                            upstream.transport.send_command(full_cmd)
                else:
                    # ACTIVATE/DEACTIVATE: only forward to client-managed relayed upstreams
                    for upstream in self._upstreams:
                        if (
                            upstream.relay
                            and upstream.interval_ms == 0
                            and upstream.transport.connected
                        ):
                            upstream.transport.send_command(full_cmd)

                # Local signals: respond to client when not hub-managed
                if self._local_signals and not self._local_fixed_interval:
                    if command == "BLAECK.ACTIVATE":
                        self._local_interval_ms = self._server._decode_four_byte(params)
                        self._local_first_time = True
                    elif command == "BLAECK.DEACTIVATE":
                        self._local_interval_ms = 0

                # WRITE_DATA: one-shot send of local signals
                if self._local_signals and command == "BLAECK.WRITE_DATA":
                    msg_id = self._server._decode_four_byte(params)
                    local_count = len(self._local_signals)
                    header = (
                        self._server.MSG_DATA
                        + b":"
                        + msg_id.to_bytes(4, "little")
                        + b":"
                    )
                    data = (
                        b"<BLAECK:"
                        + self._server._build_data_msg(
                            header, start=0, end=local_count - 1
                        )
                        + b"/BLAECK>\r\n"
                    )
                    self._server._tcp_send_data(data)

    def _poll_upstreams(self) -> None:
        """Read frames from all upstream devices, update signals, and relay immediately."""
        for upstream in self._upstreams:
            was_connected = upstream.was_connected

            if not upstream.transport.connected:
                if was_connected:
                    upstream.was_connected = False
                    self._zero_upstream_signals(upstream)
                    self._send_upstream_lost_frame(upstream)
                    if self._disconnect_callback is not None:
                        self._disconnect_callback(upstream.name)
                continue

            frames = upstream.transport.read_frames()

            # Detect disconnect that happened during read
            if was_connected and not upstream.transport.connected:
                upstream.was_connected = False
                self._zero_upstream_signals(upstream)
                self._send_upstream_lost_frame(upstream)
                if self._disconnect_callback is not None:
                    self._disconnect_callback(upstream.name)
                continue

            for frame in frames:
                if len(frame) == 0:
                    continue
                msg_key = frame[0]
                if msg_key in decoder.MSGKEY_DATA_ALL:
                    try:
                        decoded = decoder.parse_data(frame, upstream.symbol_table)

                        if not upstream.relay:
                            # Non-relayed: update internal signals only
                            for sig_id, value in decoded.signals.items():
                                idx = upstream.index_map.get(sig_id)
                                if idx is not None and idx < len(upstream._signals):
                                    upstream._signals[idx].value = value
                            self._fire_data_received(upstream)
                            continue

                        for sig_id, value in decoded.signals.items():
                            hub_idx = upstream.index_map.get(sig_id)
                            if hub_idx is not None and hub_idx < len(
                                self._server.signals
                            ):
                                self._server.signals[hub_idx].value = value
                                self._server.signals[hub_idx].updated = True
                        # Fire callback before relay so transforms can
                        # modify signal values before they go downstream
                        try:
                            self._fire_data_received(upstream)
                        except Exception as e:
                            logger.warning(
                                f"on_data_received callback error for "
                                f"'{upstream.name}': {e}"
                            )
                        # Forward upstream timestamp only with a single relayed device
                        relayed_count = sum(1 for u in self._upstreams if u.relay)
                        single = relayed_count == 1 and not self._local_signals
                        ts = decoded.timestamp if single else None
                        # Replace msg_id only when hub overrides BLAECK.ACTIVATE
                        msg_id = decoded.msg_id
                        if upstream.interval_ms > 0 and msg_id == _MSG_ID_ACTIVATE:
                            msg_id = _MSG_ID_HUB
                        # Send only upstream signals (skip local range)
                        local_count = len(self._local_signals)
                        header = (
                            self._server.MSG_DATA
                            + b":"
                            + msg_id.to_bytes(4, "little")
                            + b":"
                        )
                        data = (
                            b"<BLAECK:"
                            + self._server._build_data_msg(
                                header,
                                start=local_count,
                                only_updated=True,
                                timestamp=ts,
                            )
                            + b"/BLAECK>\r\n"
                        )
                        self._server._tcp_send_data(data)
                    except Exception as e:
                        logger.warning(
                            f"Upstream '{upstream.name}' frame dropped: {e}"
                        )

    def _zero_upstream_signals(self, upstream: _UpstreamDevice) -> None:
        """Reset all signals from a disconnected upstream to zero."""
        if upstream.relay:
            for hub_idx in upstream.index_map.values():
                if hub_idx < len(self._server.signals):
                    self._server.signals[hub_idx].value = 0
                    self._server.signals[hub_idx].updated = True
        else:
            for sig in upstream._signals:
                sig.value = 0

    def _send_upstream_lost_frame(self, upstream: _UpstreamDevice) -> None:
        """Send one data frame with STATUS_UPSTREAM_LOST for a disconnected upstream."""
        if not upstream.relay or not self._server.connected:
            return
        local_count = len(self._local_signals)
        header = (
            self._server.MSG_DATA
            + b":"
            + _MSG_ID_HUB.to_bytes(4, "little")
            + b":"
        )
        data = (
            b"<BLAECK:"
            + self._server._build_data_msg(
                header,
                start=local_count,
                only_updated=True,
                status=STATUS_UPSTREAM_LOST,
            )
            + b"/BLAECK>\r\n"
        )
        self._server._tcp_send_data(data)

    # ====================================================================
    # Local signal timing
    # ====================================================================

    def _tick_local(self) -> None:
        """Send local signals at the configured interval."""
        if self._local_interval_ms <= 0 or not self._local_signals:
            return
        if not self._server or not self._server.connected:
            return
        if not self._local_timer_elapsed():
            return

        msg_id = _MSG_ID_HUB if self._local_fixed_interval else _MSG_ID_ACTIVATE
        local_count = len(self._local_signals)
        header = self._server.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + self._server._build_data_msg(header, start=0, end=local_count - 1)
            + b"/BLAECK>\r\n"
        )
        self._server._tcp_send_data(data)

    def _local_timer_elapsed(self) -> bool:
        """Check if the local signal interval has elapsed."""
        now = time.time_ns()
        if self._local_first_time:
            self._local_timer_base = now
            self._local_timer_setpoint = self._local_interval_ms
            self._local_first_time = False
            return True
        elapsed_ms = (now - self._local_timer_base) / 1_000_000
        if elapsed_ms < self._local_timer_setpoint:
            return False
        self._local_timer_setpoint += self._local_interval_ms
        return True

    # ====================================================================
    # Upstream command forwarding
    # ====================================================================

    def _forward_to_upstreams(self, command: str) -> None:
        """Forward a command to all connected upstream devices."""
        for upstream in self._upstreams:
            if upstream.transport.connected:
                upstream.transport.send_command(command)

    def _write_symbols(self, msg_id: int) -> None:
        """Send symbol list with hub as master, then upstream slaves."""
        if not self._server.connected:
            return

        header = (
            self._server.MSG_SYMBOL_LIST + b":" + msg_id.to_bytes(4, "little") + b":"
        )

        payload = b""
        # Local (master) signals first
        for sig in self._local_signals:
            payload += (
                _MSC_MASTER
                + b"\x00"
                + sig.signal_name.encode()
                + b"\0"
                + sig.get_dtype_byte()
            )

        # Upstream (slave) signals — only relayed upstreams
        slave_idx = 0
        for upstream in self._upstreams:
            if not upstream.relay:
                continue
            slave_idx += 1
            slave_id = bytes([slave_idx])
            for sym in upstream.symbol_table:
                dtype_code = sym.datatype_code.to_bytes(1, "little")
                payload += (
                    _MSC_SLAVE + slave_id + sym.name.encode() + b"\0" + dtype_code
                )

        data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
        logger.debug(f"WRITE_SYMBOLS frame ({len(data)} bytes): {data.hex(' ')}")
        # Human-readable symbol dump
        for sig in self._local_signals:
            logger.debug(
                f"  SlaveID=0 (local) name={sig.signal_name!r} dtype={sig.datatype}"
            )
        slave_idx = 0
        for upstream in self._upstreams:
            if not upstream.relay:
                continue
            slave_idx += 1
            for sym in upstream.symbol_table:
                logger.debug(
                    f"  SlaveID={slave_idx} name={sym.name!r} dtype={sym.datatype_code}"
                )
        self._server._tcp_send(data)

    def _write_devices(self, msg_id: int) -> None:
        """Send hub as master device, then upstream slaves."""
        if not self._server.connected:
            return

        header = self._server.MSG_DEVICES + b":" + msg_id.to_bytes(4, "little") + b":"

        for client_id, conn in list(self._server._clients.items()):
            # Hub itself as master device
            payload = (
                _MSC_MASTER
                + b"\x00"  # SlaveID 0 for master
                + self._device_name.encode()
                + b"\0"
                + self._device_hw_version.encode()
                + b"\0"
                + self._device_fw_version.encode()
                + b"\0"
                + LIB_VERSION.encode()
                + b"\0"
                + LIB_NAME.encode()
                + b"\0"
                + str(client_id).encode()
                + b"\0"
                + (b"1" if client_id in self._server.data_clients else b"0")
                + b"\0"
                + b"0\0"  # server_restarted
                + b"hub\0"
            )

            # Upstream devices as slaves — only relayed upstreams
            slave_idx = 0
            for upstream in self._upstreams:
                if not upstream.relay:
                    continue
                info = upstream.device_info
                if info is None:
                    slave_idx += 1
                    continue
                slave_idx += 1
                slave_id = bytes([slave_idx])
                payload += (
                    _MSC_SLAVE
                    + slave_id
                    + upstream.name.encode()
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
                    + (b"1" if client_id in self._server.data_clients else b"0")
                    + b"\0"
                    + b"0\0"  # server_restarted
                    + b"server\0"
                )

            data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
            logger.debug(f"WRITE_DEVICES frame ({len(data)} bytes): {data.hex(' ')}")
            # Human-readable device dump
            logger.debug(f"  Master: SlaveID=0 name={self._device_name!r}")
            slave_idx = 0
            for upstream in self._upstreams:
                if not upstream.relay:
                    continue
                slave_idx += 1
                info = upstream.device_info
                if info is not None:
                    logger.debug(
                        f"  Slave:  SlaveID={slave_idx} name={upstream.name!r} hw={info.hw_version!r} fw={info.fw_version!r} lib={info.lib_version!r} lib_name={info.lib_name!r}"
                    )
            try:
                conn.sendall(data)
            except OSError as e:
                logger.debug(f"Send error: {e}")
                self._server._disconnect_client(conn)

    # ====================================================================
    # Callbacks
    # ====================================================================

    def on_upstream_disconnected(self):
        """Decorator to register a callback when an upstream device disconnects.

        Example::

            @hub.on_upstream_disconnected()
            def handle(name):
                print(f"Lost connection to {name}")
        """

        def decorator(func):
            self._disconnect_callback = func
            return func

        return decorator

    def on_client_connected(self):
        """Decorator to register a callback when a downstream client connects.

        Example::

            @hub.on_client_connected()
            def handle(client_id):
                print(f"Client #{client_id} connected")
        """

        def decorator(func):
            self._client_connect_callback = func
            if self._server is not None:
                self._server._connect_callback = func
            return func

        return decorator

    def on_client_disconnected(self):
        """Decorator to register a callback when a downstream client disconnects.

        Example::

            @hub.on_client_disconnected()
            def handle(client_id):
                print(f"Client #{client_id} disconnected")
        """

        def decorator(func):
            self._client_disconnect_callback = func
            if self._server is not None:
                self._server._disconnect_callback = func
            return func

        return decorator

    def on_data_received(self, upstream_name: str | None = None):
        """Decorator to register a callback when upstream data arrives.

        Args:
            upstream_name: If provided, only fires for that upstream.
                If None, fires for any upstream.

        Example::

            @hub.on_data_received("Arduino")
            def handle(upstream):
                temp = upstream.signals["temperature"].value

            @hub.on_data_received()
            def handle_all(upstream):
                print(f"Data from {upstream.name}")
        """

        def decorator(func):
            self._data_received_callbacks.append((upstream_name, func))
            return func

        return decorator

    def on_command(self, command: str | None = None):
        """Decorator to register a handler for commands from downstream clients.

        With a command name, registers a handler for that specific command.
        Without a command name, registers a catch-all for every message.

        Example::

            @hub.on_command("SET_MODE")
            def handle_mode(mode):
                print(f"Mode set to {mode}")

            @hub.on_command()
            def log_all(command, *params):
                print(f"{command}: {params}")
        """

        def decorator(func):
            if command is None:
                self._command_catchall = func
                if self._server is not None:
                    self._server._read_callback = func
            else:
                self._command_handlers[command] = func
                if self._server is not None:
                    self._server._command_handlers[command] = func
            return func

        return decorator

    def _fire_data_received(self, upstream: _UpstreamDevice) -> None:
        """Invoke all matching on_data_received callbacks."""
        for name_filter, func in self._data_received_callbacks:
            if name_filter is None or name_filter == upstream.name:
                func(upstream)

    # ====================================================================
    # Status & properties
    # ====================================================================

    @property
    def signals(self) -> list:
        """All server signals (local + relayed upstream)."""
        if self._server:
            return self._server.signals
        return []

    def __getitem__(self, name: str) -> _UpstreamDevice:
        """Access an upstream device by name.

        Example::

            hub["Arduino"]["temperature"].value
            hub["Arduino"].signals[0].value
        """
        for upstream in self._upstreams:
            if upstream.name == name:
                return upstream
        raise KeyError(f"No upstream named {name!r}")

    @property
    def connected(self) -> bool:
        """True if any downstream client (e.g. Loggbok) is connected."""
        if self._server:
            return self._server.connected
        return False

    @property
    def server(self) -> BlaeckServer | None:
        """The underlying BlaeckServer server instance (available after start)."""
        return self._server

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
                if u.name == name:
                    return _status(u)
            raise KeyError(f"No upstream named '{name}'")

        return {u.name: _status(u) for u in self._upstreams}

    def __repr__(self):
        n_up = len(self._upstreams)
        n_sig = len(self.signals)
        started = "started" if self._started else "not started"
        return f"BlaeckHub [{n_up} upstreams] [{n_sig} signals] [{started}]"
