"""BlaeckHub — multi-device signal aggregator.

Connects to multiple upstream BlaeckTCP(y)/BlaeckSerial devices,
discovers their signals, decodes incoming data frames, and serves
all signals as a single merged device to Loggbok via a BlaeckServer
downstream server.

Example::

    from blaecktcpy import BlaeckHub

    hub = BlaeckHub("0.0.0.0", 23, "My Hub", "Python", "1.0")
    hub.add_tcp("192.168.1.10", 23, "ESP32")
    hub.add_tcp("127.0.0.1", 24, "Python")
    hub.start()

    while True:
        hub.tick()
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Union

from .._signal import Signal, SignalList, IntervalMode
from .._server import BlaeckServer, LIB_VERSION, LIB_NAME, STATUS_UPSTREAM_LOST, _IntervalTimer
from . import _decoder as decoder
from ._upstream import UpstreamTCP, UpstreamSerial, _UpstreamBase

# MasterSlaveConfig byte values
_MSC_MASTER = b"\x01"
_MSC_SLAVE = b"\x02"

# Message IDs for data frames
_MSG_ID_ACTIVATE = 185273099  # 0x0B0B0B0B — client-controlled (BLAECK.ACTIVATE)
_MSG_ID_HUB = 185273100  # 0x0B0B0B0C — hub-overridden interval

logger = logging.getLogger("blaecktcpy")


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
    _signals: list[Signal] = field(default_factory=list)
    _upstream_signals: SignalList | None = field(default=None, repr=False)

    @property
    def signals(self) -> SignalList:
        if self._upstream_signals is None:
            raise RuntimeError(
                "Signals not available yet — call hub.start() first"
            )
        return self._upstream_signals

    def __getitem__(self, key: int | str) -> Signal:
        return self.signals[key]


class HubLocalSignals:
    """Manages local signals on a BlaeckHub.

    Accessed via ``hub.local``. Provides the same signal write/update API
    as :class:`BlaeckServer`, scoped to hub-local signals only.

    Example::

        sig = hub.local.add_signal("temperature", "float")
        hub.local.set_interval(300)
        hub.start()

        while True:
            hub.tick()
            hub.local.tick()
    """

    def __init__(self, hub: "BlaeckHub"):
        self._hub = hub
        self._signals: SignalList = SignalList()
        self._before_write_callback = None
        self._fixed_interval_ms: int = IntervalMode.CLIENT
        self._timed_activated: bool = False
        self._timer = _IntervalTimer()

    # ---- Setup (before start) ----

    def add_signal(
        self,
        signal_or_name: Union[Signal, str],
        datatype: str = "",
        value: Union[int, float] = 0,
    ) -> Signal:
        """Add a local signal to the hub.

        Can be called with a Signal object or with individual arguments::

            hub.local.add_signal(Signal('temp', 'float', 0.0))
            hub.local.add_signal('temp', 'float', 0.0)         # shorthand

        Local signals appear before upstream signals in the signal list.
        Can be called before or after :meth:`BlaeckHub.start`.

        .. warning::
           Adding signals after start changes the signal list.
           Ensure no client is actively logging when you do this.

        Returns:
            The Signal object. Update its ``.value`` and ``.updated``
            attributes in your main loop.
        """
        if isinstance(signal_or_name, Signal):
            sig = signal_or_name
        elif isinstance(signal_or_name, str):
            sig = Signal(signal_or_name, datatype, value)
        else:
            raise TypeError(f"Expected Signal or str, got {type(signal_or_name)}")
        self._signals.append(sig)
        if self._hub._started and self._hub._server is not None:
            insert_pos = len(self._signals) - 1
            self._hub._server.signals.insert(insert_pos, sig)
            self._rebuild_upstream_indices()
        return sig

    def add_signals(self, signals) -> None:
        """Add multiple local signals at once.

        Accepts any iterable of Signal objects::

            hub.local.add_signals([
                Signal('temp', 'float', 0.0),
                Signal('led',  'bool',  False),
            ])

        .. warning::
           Adding signals after start changes the signal list.
           Ensure no client is actively logging when you do this.
        """
        for sig in signals:
            if isinstance(sig, Signal):
                self.add_signal(sig.signal_name, sig.datatype, sig.value)
            else:
                self.add_signal(*sig)

    def delete_signals(self) -> None:
        """Remove all local signals.

        .. warning::
           Ensure no client is actively logging when you call this.
        """
        if self._hub._started and self._hub._server is not None:
            n = len(self._signals)
            if n > 0:
                del self._hub._server.signals[:n]
                self._rebuild_upstream_indices()
        self._signals.clear()

    def _rebuild_upstream_indices(self) -> None:
        """Rebuild relayed upstream index_map from current server.signals."""
        server = self._hub._server
        for upstream in self._hub._upstreams:
            if upstream.relay_downstream:
                for k, sig in enumerate(upstream._signals):
                    upstream.index_map[k] = server.signals.index(sig)

    def set_interval(self, interval_ms: int) -> None:
        """Set the timed data interval mode for local signals.

        Controls how timed data transmission is managed:

        * **interval_ms >= 0** — Lock at the given rate.  Client
          ``ACTIVATE`` / ``DEACTIVATE`` commands are ignored.
          ``0`` means "as fast as possible."
        * **IntervalMode.OFF** — Timed data is off.  Client
          ``ACTIVATE`` is ignored.
        * **IntervalMode.CLIENT** — Client controlled (default).
          The client's ``ACTIVATE`` / ``DEACTIVATE`` commands
          determine the rate.

        Args:
            interval_ms: Interval in milliseconds, or an
                :class:`IntervalMode` member.
        """
        if interval_ms >= 0:
            self._fixed_interval_ms = interval_ms
            self._timed_activated = True
            self._timer.activate(interval_ms)
            logger.info(
                f"Local fixed interval set ({interval_ms} ms) — client control locked"
            )
        elif interval_ms == IntervalMode.OFF:
            self._fixed_interval_ms = IntervalMode.OFF
            self._timed_activated = False
            self._timer.deactivate()
            logger.info("Local timed data locked off — client control locked")
        elif interval_ms == IntervalMode.CLIENT:
            self._fixed_interval_ms = IntervalMode.CLIENT
            logger.info("Local client control restored")

    def on_before_write(self):
        """Decorator to register a callback that fires before local data is sent.

        Use this to update local signal values right before they are
        transmitted — especially useful for client-triggered ``WRITE_DATA``
        one-shot requests.

        Example::

            @hub.local.on_before_write()
            def refresh():
                hub.local.signals["temperature"].value = read_sensor()
        """

        def decorator(func):
            self._before_write_callback = func
            return func

        return decorator

    def _fire_before_write(self) -> None:
        """Invoke the before-write callback if registered."""
        if self._before_write_callback is not None:
            self._before_write_callback()

    # ---- Signal resolution ----

    def _resolve(self, key: Union[str, int]) -> int:
        """Resolve a signal name or index to a valid local signal index.

        Raises:
            IndexError: If index is out of range for local signals.
            KeyError: If signal name is not found among local signals.
        """
        if isinstance(key, int):
            if not self._signals:
                raise IndexError("No local signals configured")
            if 0 <= key < len(self._signals):
                return key
            raise IndexError(
                f"Local signal index {key} out of range "
                f"(0..{len(self._signals) - 1})"
            )
        for i, sig in enumerate(self._signals):
            if sig.signal_name == key:
                return i
        raise KeyError(f"Local signal '{key}' not found")

    def _require_started(self) -> BlaeckServer:
        """Return the hub's server, raising if not started."""
        if not self._hub._started or self._hub._server is None:
            raise RuntimeError("Must call start() before using hub.local methods")
        return self._hub._server

    # ---- Single-signal methods ----

    def write(
        self, key: Union[str, int], value: Union[int, float], *, msg_id: int = 1
    ) -> None:
        """Update a local signal's value and immediately send it.

        Args:
            key: Signal name (str) or local index (int)
            value: New value to set
            msg_id: Message ID for the protocol frame
        """
        server = self._require_started()
        idx = self._resolve(key)
        server.write(idx, value, msg_id=msg_id)

    def update(self, key: Union[str, int], value: Union[int, float]) -> None:
        """Update a local signal's value and mark it as updated (no send).

        Args:
            key: Signal name (str) or local index (int)
            value: New value to set
        """
        server = self._require_started()
        idx = self._resolve(key)
        server.update(idx, value)

    def mark_signal_updated(self, key: Union[str, int]) -> None:
        """Mark a local signal as updated without changing its value."""
        self._require_started()
        idx = self._resolve(key)
        self._signals[idx].updated = True

    def mark_all_signals_updated(self) -> None:
        """Mark all local signals as updated."""
        self._require_started()
        for sig in self._signals:
            sig.updated = True

    def clear_all_update_flags(self) -> None:
        """Clear the updated flag on all local signals."""
        self._require_started()
        for sig in self._signals:
            sig.updated = False

    @property
    def has_updated_signals(self) -> bool:
        """True if any local signal is marked as updated."""
        return any(sig.updated for sig in self._signals)

    @property
    def signals(self) -> SignalList:
        """Local signals, accessible by index or name.

        Example::

            hub.local.signals[0].value
            hub.local.signals["temperature"].value
        """
        return self._signals

    # ---- Immediate bulk methods ----

    def write_all_data(self, msg_id: int = 1) -> None:
        """Send all local signal data to data-enabled clients immediately."""
        server = self._require_started()
        if not self._signals or not server.connected:
            return
        self._fire_before_write()
        local_count = len(self._signals)
        header = server.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + server._build_data_msg(header, start=0, end=local_count - 1)
            + b"/BLAECK>\r\n"
        )
        server._tcp_send_data(data)

    def write_updated_data(self, msg_id: int = 1) -> None:
        """Send only updated local signals to data-enabled clients immediately.

        Args:
            msg_id: Message ID for the protocol frame
        """
        server = self._require_started()
        if not self._signals or not server.connected:
            return
        if not self.has_updated_signals:
            return
        self._fire_before_write()
        local_count = len(self._signals)
        header = server.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + server._build_data_msg(
                header, start=0, end=local_count - 1, only_updated=True,
            )
            + b"/BLAECK>\r\n"
        )
        server._tcp_send_data(data)

    # ---- Timed methods ----

    def timed_write_all_data(self, msg_id: int = 185273099) -> bool:
        """Send all local signals if the timer interval has elapsed."""
        server = self._require_started()
        if not self._timed_activated or not self._signals:
            return False
        if not server.connected:
            return False
        if not self._timer.elapsed():
            return False

        self._fire_before_write()
        local_count = len(self._signals)
        header = server.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + server._build_data_msg(header, start=0, end=local_count - 1)
            + b"/BLAECK>\r\n"
        )
        return server._tcp_send_data(data)

    def timed_write_updated_data(self, msg_id: int = 185273099) -> bool:
        """Send only updated local signals if the timer interval has elapsed."""
        server = self._require_started()
        if not self._timed_activated or not self._signals:
            return False
        if not server.connected:
            return False
        if not self._timer.elapsed():
            return False
        if not self.has_updated_signals:
            return False

        self._fire_before_write()
        local_count = len(self._signals)
        header = server.MSG_DATA + b":" + msg_id.to_bytes(4, "little") + b":"
        data = (
            b"<BLAECK:"
            + server._build_data_msg(
                header, start=0, end=local_count - 1, only_updated=True
            )
            + b"/BLAECK>\r\n"
        )
        return server._tcp_send_data(data)

    def tick(self, msg_id: int = 185273099) -> bool:
        """Send all local signal data if the timer interval has elapsed.

        Convenience alias for :meth:`timed_write_all_data`.
        Call this in your main loop alongside :meth:`BlaeckHub.tick`.
        Returns True if timed data was sent.
        """
        return self.timed_write_all_data(msg_id)

    def tick_updated(self, msg_id: int = 185273099) -> bool:
        """Send only updated local signals if the timer interval has elapsed.

        Convenience alias for :meth:`timed_write_updated_data`.
        Call this in your main loop alongside :meth:`BlaeckHub.tick`.
        Returns True if timed data was sent.
        """
        return self.timed_write_updated_data(msg_id)


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
        self._server: BlaeckServer | None = None
        self._started = False

        self.local = HubLocalSignals(self)

        self._disconnect_callback = None
        self._client_connect_callback = None
        self._client_disconnect_callback = None
        self._data_received_callbacks: list[tuple[str | None, object]] = []
        self._command_handlers: dict[str, object] = {}
        self._command_catchall = None

    # ====================================================================
    # Setup — call before start()
    # ====================================================================

    def add_tcp(
        self,
        ip: str,
        port: int,
        name: str = "",
        timeout: float = 5.0,
        interval_ms: int = IntervalMode.CLIENT,
        relay_downstream: bool = True,
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
                :class:`IntervalMode` member.  Default is
                ``IntervalMode.CLIENT`` (client controlled).
            relay_downstream: If False, signals are decoded hub-side but not exposed
                to downstream clients (no symbols, devices, or data).

        Returns:
            Upstream handle for accessing signal values.
        """
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")

        label = name or f"{ip}:{port}"
        transport = UpstreamTCP(label, ip, port)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay_downstream)

    def add_serial(
        self,
        port: str,
        baudrate: int = 115200,
        name: str = "",
        timeout: float = 5.0,
        dtr: bool = True,
        interval_ms: int = IntervalMode.CLIENT,
        relay_downstream: bool = True,
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
                :class:`IntervalMode` member.  Default is
                ``IntervalMode.CLIENT`` (client controlled).
            relay_downstream: If False, signals are decoded hub-side but not exposed
                to downstream clients (no symbols, devices, or data).

        Returns:
            Upstream handle for accessing signal values.
        """
        if self._started:
            raise RuntimeError("Cannot add upstreams after start()")

        label = name or port
        transport = UpstreamSerial(label, port, baudrate, dtr)
        return self._discover_upstream(name, transport, timeout, interval_ms, relay_downstream)

    def _discover_upstream(
        self,
        name: str,
        transport: _UpstreamBase,
        timeout: float,
        interval_ms: int = 0,
        relay_downstream: bool = True,
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
            device_name=name, transport=transport, interval_ms=interval_ms, relay_downstream=relay_downstream
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
                upstream.device_infos = decoder.parse_all_devices(frame)
            except Exception as e:
                logger.debug(f"Upstream '{label}' device info parse error: {e}")

        # Use upstream device name if no name was provided
        if not name and upstream.device_infos:
            upstream.device_name = upstream.device_infos[0].device_name

        self._upstreams.append(upstream)

        logger.info(f"Upstream '{upstream.device_name}': {len(symbols)} signals discovered")
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
        for sig in self.local._signals:
            self._server.add_signal(sig)

        # Register upstream signals and build index maps
        offset = len(self.local._signals)
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
            upstream._upstream_signals = SignalList(upstream._signals)

        self._started = True

        # Wire up server client callbacks
        if self._client_connect_callback is not None:
            self._server._connect_callback = self._client_connect_callback
        if self._client_disconnect_callback is not None:
            self._server._disconnect_callback = self._client_disconnect_callback

        # Activate upstreams with a fixed interval
        for upstream in self._upstreams:
            if upstream.interval_ms >= 0:
                b = upstream.interval_ms.to_bytes(4, "little")
                params = ",".join(str(x) for x in b)
                upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}")

        total = len(self._server.signals)
        logger.info(f"BlaeckHub started with {total} signals")

    def close(self) -> None:
        """Shut down all upstream connections and the downstream server."""
        for upstream in self._upstreams:
            if upstream.transport.connected:
                upstream.transport.send_command("BLAECK.DEACTIVATE")
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

    def read(self) -> None:
        """Read and process commands from downstream clients.

        Call this repeatedly in your main loop when you only need to handle
        client commands (e.g. WRITE_SYMBOLS, WRITE_DATA) without polling
        upstreams. Useful when the hub has only local signals and no upstreams.
        """
        if not self._started or self._server is None:
            raise RuntimeError("Must call start() before read()")
        self._read()

    def tick(self) -> None:
        """Main loop tick — read commands and poll upstreams.

        Call this repeatedly in your main loop.
        Reads commands from downstream clients and forwards them to upstreams.
        Reads data from upstreams and immediately relays updated signals downstream.

        Does **not** send local signal data — use :meth:`HubLocalSignals.tick`
        (``hub.local.tick()``) for that.
        """
        if not self._started or self._server is None:
            raise RuntimeError("Must call start() before tick()")
        self._read()
        self._poll_upstreams()

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
            self._server._commanding_client = conn

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

                # Local signals: respond to client when in client-controlled mode
                if (
                    self.local._signals
                    and self.local._fixed_interval_ms == IntervalMode.CLIENT
                ):
                    if command == "BLAECK.ACTIVATE":
                        self.local._timed_activated = True
                        self.local._timer.activate(
                            self._server._decode_four_byte(params)
                        )
                    elif command == "BLAECK.DEACTIVATE":
                        self.local._timed_activated = False
                        self.local._timer.deactivate()

                # WRITE_DATA: one-shot send of local signals
                if self.local._signals and command == "BLAECK.WRITE_DATA":
                    self.local._fire_before_write()
                    msg_id = self._server._decode_four_byte(params)
                    local_count = len(self.local._signals)
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

            # Dispatch to specific command handler
            handler = self._command_handlers.get(command)
            if handler is not None:
                handler(*params)

            # Fire catch-all callback
            if self._command_catchall is not None:
                self._command_catchall(command, *params)

    def _poll_upstreams(self) -> None:
        """Read frames from all upstream devices, update signals, and relay immediately."""
        for upstream in self._upstreams:
            if not upstream.transport.connected:
                if upstream.connected:
                    upstream.connected = False
                    self._zero_upstream_signals(upstream)
                    self._send_upstream_lost_frame(upstream)
                    if self._disconnect_callback is not None:
                        self._disconnect_callback(upstream.device_name)
                continue

            frames = upstream.transport.read_frames()

            # Detect disconnect that happened during read
            if upstream.connected and not upstream.transport.connected:
                upstream.connected = False
                self._zero_upstream_signals(upstream)
                self._send_upstream_lost_frame(upstream)
                if self._disconnect_callback is not None:
                    self._disconnect_callback(upstream.device_name)
                continue

            for frame in frames:
                if len(frame) == 0:
                    continue
                msg_key = frame[0]
                if msg_key in decoder.MSGKEY_DATA_ALL:
                    try:
                        decoded = decoder.parse_data(frame, upstream.symbol_table)

                        # relay upstream restart flag downstream
                        if decoded.restart_flag:
                            self._server._restart_flag_pending = True

                        if not upstream.relay_downstream:
                            # Non-relayed: update internal signals only
                            for sig_id, value in decoded.signals.items():
                                idx = upstream.index_map.get(sig_id)
                                if idx is not None and idx < len(upstream._signals):
                                    upstream._signals[idx].value = value
                            try:
                                self._fire_data_received(upstream)
                            except Exception as e:
                                logger.warning(
                                    f"on_data_received callback error for "
                                    f"'{upstream.device_name}': {e}"
                                )
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
                                f"'{upstream.device_name}': {e}"
                            )
                        # Forward upstream timestamp only with a single relayed device
                        relayed_count = sum(1 for u in self._upstreams if u.relay_downstream)
                        single = relayed_count == 1 and not self.local._signals
                        ts = decoded.timestamp if single else None
                        # Replace msg_id only when hub overrides BLAECK.ACTIVATE
                        msg_id = decoded.msg_id
                        if upstream.interval_ms >= 0 and msg_id == _MSG_ID_ACTIVATE:
                            msg_id = _MSG_ID_HUB
                        # Determine this upstream's signal index range
                        hub_indices = sorted(upstream.index_map.values())
                        if not hub_indices:
                            continue
                        start_idx = hub_indices[0]
                        end_idx = hub_indices[-1]
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
                                start=start_idx,
                                end=end_idx,
                                only_updated=True,
                                timestamp=ts,
                                status=decoded.status_byte,
                            )
                            + b"/BLAECK>\r\n"
                        )
                        self._server._tcp_send_data(data)
                    except Exception as e:
                        logger.warning(
                            f"Upstream '{upstream.device_name}' frame dropped: {e}"
                        )

    def _zero_upstream_signals(self, upstream: _UpstreamDevice) -> None:
        """Reset all signals from a disconnected upstream to zero."""
        if upstream.relay_downstream:
            for hub_idx in upstream.index_map.values():
                if hub_idx < len(self._server.signals):
                    self._server.signals[hub_idx].value = 0
                    self._server.signals[hub_idx].updated = True
        else:
            for sig in upstream._signals:
                sig.value = 0

    def _send_upstream_lost_frame(self, upstream: _UpstreamDevice) -> None:
        """Send one data frame with STATUS_UPSTREAM_LOST for a disconnected upstream."""
        if not upstream.relay_downstream or not self._server.connected:
            return
        hub_indices = sorted(upstream.index_map.values())
        if not hub_indices:
            return
        start_idx = hub_indices[0]
        end_idx = hub_indices[-1]
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
                start=start_idx,
                end=end_idx,
                only_updated=True,
                status=STATUS_UPSTREAM_LOST,
            )
            + b"/BLAECK>\r\n"
        )
        self._server._tcp_send_data(data)

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
        for sig in self.local._signals:
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
                hub_sid = upstream.slave_id_map[key]
                dtype_code = sym.datatype_code.to_bytes(1, "little")
                payload += (
                    _MSC_SLAVE
                    + bytes([hub_sid])
                    + sym.name.encode()
                    + b"\0"
                    + dtype_code
                )

        data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
        logger.debug(f"WRITE_SYMBOLS frame ({len(data)} bytes): {data.hex(' ')}")
        # Human-readable symbol dump
        for sig in self.local._signals:
            logger.debug(
                f"  SlaveID=0 (local) name={sig.signal_name!r} dtype={sig.datatype}"
            )
        for upstream in self._upstreams:
            if not upstream.relay_downstream:
                continue
            for sym in upstream.symbol_table:
                key = (sym.msc, sym.slave_id)
                hub_sid = upstream.slave_id_map[key]
                logger.debug(
                    f"  SlaveID={hub_sid} name={sym.name!r} dtype={sym.datatype_code}"
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
                + (b"1" if self._server._server_restarted else b"0")
                + b"\0"  # server_restarted
                + b"hub\0"
                + b"0\0"  # parent (master references itself)
            )

            # Upstream devices as slaves — only relayed upstreams
            for upstream in self._upstreams:
                if not upstream.relay_downstream:
                    continue
                # Build old SlaveID → new hub SlaveID map for parent remapping
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
                        # First entry of each upstream belongs to hub master
                        parent_sid = 0
                        first_entry = False
                    else:
                        # Remap parent through old→new map
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
                        + (b"1" if client_id in self._server.data_clients else b"0")
                        + b"\0"
                        + (info.server_restarted.encode() if info.server_restarted else b"0")
                        + b"\0"  # server_restarted
                        + device_type.encode()
                        + b"\0"
                        + str(parent_sid).encode()
                        + b"\0"
                    )

            data = b"<BLAECK:" + header + payload + b"/BLAECK>\r\n"
            logger.debug(f"WRITE_DEVICES frame ({len(data)} bytes): {data.hex(' ')}")
            # Human-readable device dump
            logger.debug(f"  Master: SlaveID=0 name={self._device_name!r}")
            for upstream in self._upstreams:
                if not upstream.relay_downstream:
                    continue
                for info in upstream.device_infos:
                    key = (info.msc, info.slave_id)
                    hub_sid = upstream.slave_id_map.get(key)
                    if hub_sid is not None:
                        logger.debug(
                            f"  Slave:  SlaveID={hub_sid} name={info.device_name!r} hw={info.hw_version!r} fw={info.fw_version!r} lib={info.lib_version!r} lib_name={info.lib_name!r}"
                        )
            try:
                conn.sendall(data)
            except OSError as e:
                logger.debug(f"Send error: {e}")
                self._server._disconnect_client(conn)

        self._server._server_restarted = False

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
                print(f"Data from {upstream.device_name}")
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
            else:
                self._command_handlers[command] = func
            return func

        return decorator

    def _fire_data_received(self, upstream: _UpstreamDevice) -> None:
        """Invoke all matching on_data_received callbacks."""
        for name_filter, func in self._data_received_callbacks:
            if name_filter is None or name_filter == upstream.device_name:
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
            if upstream.device_name == name:
                return upstream
        raise KeyError(f"No upstream named {name!r}")

    @property
    def connected(self) -> bool:
        """True if any downstream client (e.g. Loggbok) is connected."""
        if self._server:
            return self._server.connected
        return False

    @property
    def commanding_client(self):
        """The client socket that sent the most recent command, or None."""
        if self._server:
            return self._server.commanding_client
        return None

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
                if u.device_name == name:
                    return _status(u)
            raise KeyError(f"No upstream named '{name}'")

        return {u.device_name: _status(u) for u in self._upstreams}

    def __repr__(self):
        n_up = len(self._upstreams)
        n_sig = len(self.signals)
        started = "started" if self._started else "not started"
        return f"BlaeckHub [{n_up} upstreams] [{n_sig} signals] [{started}]"
