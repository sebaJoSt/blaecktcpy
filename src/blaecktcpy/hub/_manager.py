"""Hub upstream device management for BlaeckTCPy.

Manages upstream device registration, discovery, polling, schema
validation, data relay, and reconnection.  Used internally by
:class:`~blaecktcpy.BlaeckTCPy`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._signal import Signal, SignalList, IntervalMode
from .._encoder import MSC_SLAVE, STATUS_UPSTREAM_LOST, STATUS_UPSTREAM_RECONNECTED
from . import _decoder as decoder
from ._upstream import UpstreamTCP, _UpstreamBase

if TYPE_CHECKING:
    from .._protocols import HubHost

# Message IDs for data frames
_MSG_ID_ACTIVATE = 185273099  # 0x0B0B0B0B
_MSG_ID_HUB = 185273100  # 0x0B0B0B0C


@dataclass
class UpstreamDevice:
    """Represents one upstream device connected via hub mode."""

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
    replay_commands: list[str] = field(default_factory=list)
    _signals: list[Signal] = field(default_factory=list)
    _upstream_signals: SignalList | None = field(default=None, repr=False)
    expected_schema_hash: int = 0
    schema_stale: bool = False
    _restart_c0_sent: bool = False
    auto_reconnect: bool = False
    _reconnect_cooldown: float = 0.0
    _reconnect_delay: float = 1.0
    _reconnecting: bool = False
    _awaiting_symbols: bool = False
    _awaiting_devices: bool = False
    _restart_detected: bool = False
    _discovery_retry_at: float = 0.0
    _discovery_timeout: float = 0.0
    _ts_relay_warned: bool = False

    @property
    def signals(self) -> SignalList:
        if self._upstream_signals is None:
            raise RuntimeError(
                "Signals not available yet — call start() first"
            )
        return self._upstream_signals

    def __getitem__(self, key: int | str) -> Signal:
        return self.signals[key]


class HubManager:
    """Manages upstream device connections and data relay."""

    def __init__(self, server: HubHost, logger: logging.Logger) -> None:
        self._server: HubHost = server
        self._logger: logging.Logger = logger
        self._upstreams: list[UpstreamDevice] = []

    # ── Registration ─────────────────────────────────────────────────

    @staticmethod
    def _validate_interval_ms(value: int) -> None:
        if value >= 0 or value == IntervalMode.CLIENT or value == IntervalMode.OFF:
            return
        raise ValueError(
            f"Invalid interval_ms: {value}. "
            f"Use a positive integer, IntervalMode.CLIENT, or IntervalMode.OFF."
        )

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
        replay_commands: list[str] | None = None,
    ) -> UpstreamDevice:
        """Register an upstream TCP device for later discovery."""
        if self._server._started:
            raise RuntimeError("Cannot add upstreams after start()")
        self._validate_interval_ms(interval_ms)
        if not isinstance(relay_downstream, bool):
            raise TypeError("relay_downstream must be True or False")
        if not isinstance(forward_custom_commands, (bool, list)):
            raise TypeError(
                "forward_custom_commands must be True, False, "
                "or a list of command names"
            )
        if replay_commands is not None and not isinstance(replay_commands, list):
            raise TypeError("replay_commands must be a list of command names or None")

        label = name or f"{ip}:{port}"
        transport = UpstreamTCP(label, ip, port, logger=self._logger)
        upstream = UpstreamDevice(
            device_name=name,
            transport=transport,
            interval_ms=interval_ms,
            connected=False,
            relay_downstream=relay_downstream,
            forward_custom_commands=forward_custom_commands,
            replay_commands=replay_commands or [],
            auto_reconnect=auto_reconnect,
            _discovery_timeout=timeout,
        )
        self._upstreams.append(upstream)
        return upstream

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
        replay_commands: list[str] | None = None,
    ) -> UpstreamDevice:
        """Register an upstream serial device for later discovery."""
        from ._upstream import UpstreamSerial

        if self._server._started:
            raise RuntimeError("Cannot add upstreams after start()")
        self._validate_interval_ms(interval_ms)
        if not isinstance(relay_downstream, bool):
            raise TypeError("relay_downstream must be True or False")
        if not isinstance(forward_custom_commands, (bool, list)):
            raise TypeError(
                "forward_custom_commands must be True, False, "
                "or a list of command names"
            )
        if replay_commands is not None and not isinstance(replay_commands, list):
            raise TypeError("replay_commands must be a list of command names or None")

        label = name or port
        transport = UpstreamSerial(
            label, port, baudrate, dtr, logger=self._logger
        )
        upstream = UpstreamDevice(
            device_name=name,
            transport=transport,
            interval_ms=interval_ms,
            connected=False,
            relay_downstream=relay_downstream,
            forward_custom_commands=forward_custom_commands,
            replay_commands=replay_commands or [],
            _discovery_timeout=timeout,
        )
        self._upstreams.append(upstream)
        return upstream

    # ── Discovery ────────────────────────────────────────────────────

    def discover_all(self) -> None:
        """Connect and discover each upstream sequentially (blocking)."""
        for upstream in self._upstreams:
            if upstream._discovery_timeout <= 0:
                continue
            self._discover_upstream(upstream)

    def _poll_for_frame(
        self,
        upstream: UpstreamDevice,
        msg_key_match: int | set[int],
        retry_command: str,
        max_polls: int,
    ) -> bytes | None:
        """Send a command and poll for a matching response frame."""
        upstream.transport.send_command(retry_command)
        match = (
            msg_key_match
            if isinstance(msg_key_match, (set, frozenset))
            else {msg_key_match}
        )
        for i in range(max_polls):
            time.sleep(0.1)
            for f in upstream.transport.read_frames():
                if len(f) > 0 and f[0] in match:
                    return f
            if i > 0 and i % 10 == 0:
                upstream.transport.send_command(retry_command)
        return None

    def _discover_upstream(self, upstream: UpstreamDevice) -> None:
        """Connect, fetch symbols and device info from one upstream."""
        timeout = upstream._discovery_timeout
        label = upstream.device_name or upstream.transport.name

        if not upstream.transport.connect(timeout):
            raise ConnectionError(
                f"Failed to connect to upstream '{label}': "
                f"{upstream.transport.last_error}"
            )
        upstream.connected = True
        max_polls = int(timeout / 0.1)

        # Phase 1: request symbol list
        upstream.transport.send_command("BLAECK.DEACTIVATE")
        frame = self._poll_for_frame(
            upstream,
            decoder.MSGKEY_SYMBOL_LIST,
            "BLAECK.WRITE_SYMBOLS",
            max_polls,
        )

        if frame is None:
            upstream.transport.close()
            raise TimeoutError(
                f"Upstream '{label}' did not respond to WRITE_SYMBOLS"
            )

        symbols = decoder.parse_symbol_list(frame)
        upstream.symbol_table = symbols
        upstream.expected_schema_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in symbols]
        )

        # Phase 2: request device info
        get_devices_cmd = f"BLAECK.GET_DEVICES{self._hub_identity}"
        frame = self._poll_for_frame(
            upstream,
            decoder.MSGKEY_DEVICES_ALL,
            get_devices_cmd,
            max_polls,
        )

        if frame is not None:
            try:
                upstream.device_infos = decoder.parse_all_devices(frame)
            except (ValueError, IndexError, UnicodeDecodeError) as e:
                self._logger.debug(
                    f"Upstream '{label}' device info parse error: {e}"
                )

        # Post-discovery finalization
        if not upstream.device_name and upstream.device_infos:
            upstream.device_name = upstream.device_infos[0].device_name

        # Discovery consumed the initial boot state — the 0xC0 frame was
        # discarded by _poll_for_frame and the first data frame's
        # restart_flag is part of the same boot, not a real restart.
        # Setting _restart_c0_sent ensures that flag is consumed.
        upstream._restart_c0_sent = True

        interval_info = ""
        if upstream.interval_ms >= 0:
            interval_info = (
                f" (interval: {upstream.interval_ms} ms — locked)"
            )
        elif upstream.interval_ms == IntervalMode.OFF:
            interval_info = " (interval: OFF — locked)"
        elif upstream.interval_ms == IntervalMode.CLIENT:
            interval_info = " (interval: client controlled)"
        self._logger.info(
            f"Upstream '{upstream.device_name}': "
            f"{len(upstream.symbol_table)} signals discovered"
            f"{interval_info}"
        )

    def register_signals(self) -> None:
        """Build slave_id_maps, register upstream signals, and build index maps."""
        if not self._upstreams:
            return

        signals = self._server.signals
        offset = self._server._local_signal_count
        hub_slave_idx = 0
        for upstream in self._upstreams:
            if upstream.relay_downstream:
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

                for i, sym in enumerate(upstream.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    sig = Signal(sym.name, sig_type)
                    signals.append(sig)
                    upstream._signals.append(signals[offset])
                    upstream.index_map[i] = offset
                    offset += 1
            else:
                for i, sym in enumerate(upstream.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    sig = Signal(sym.name, sig_type)
                    upstream._signals.append(sig)
                    upstream.index_map[i] = i

            upstream._upstream_signals = SignalList(upstream._signals)
            upstream.expected_schema_hash = decoder.compute_schema_hash(
                [(s.name, s.datatype_code) for s in upstream.symbol_table]
            )

    def activate(self) -> None:
        """Send ACTIVATE or DEACTIVATE to upstreams based on interval setting."""
        for upstream in self._upstreams:
            if upstream.interval_ms >= 0:
                b = upstream.interval_ms.to_bytes(4, "little")
                params = ",".join(str(x) for x in b)
                upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}")
            elif upstream.interval_ms == IntervalMode.OFF:
                upstream.transport.send_command("BLAECK.DEACTIVATE")

    # ── Polling ──────────────────────────────────────────────────────

    def poll(self) -> None:
        """Read frames from all upstream devices, update signals, and relay."""
        if not self._upstreams:
            return

        for upstream in self._upstreams:
            if self._poll_upstream_connection(upstream):
                continue

            frames = upstream.transport.read_frames()

            if upstream.connected and not upstream.transport.connected:
                self._handle_upstream_disconnect(upstream)
                continue

            for frame in frames:
                if len(frame) == 0:
                    continue
                msg_key = frame[0]

                if msg_key == decoder.MSGKEY_SYMBOL_LIST and (
                    upstream.schema_stale or upstream._awaiting_symbols
                ):
                    self._handle_symbol_list(upstream, frame)
                    continue

                if msg_key in decoder.MSGKEY_DEVICES_ALL:
                    self._handle_device_info(upstream, frame)
                    continue

                if msg_key == decoder.MSGKEY_RESTART:
                    self._logger.info(
                        f"Upstream '{upstream.device_name}' restarted (0xC0)"
                    )
                    self._send_upstream_restarted_frame(upstream)
                    self._replay_custom_commands(upstream)
                    self._resend_activate(upstream)
                    continue

                if msg_key in decoder.MSGKEY_DATA_ALL:
                    self._process_upstream_data(upstream, frame, msg_key)

            if upstream._reconnecting:
                self._retry_discovery(upstream)

    def _poll_upstream_connection(
        self, upstream: UpstreamDevice
    ) -> bool:
        """Handle connection state. Returns True to skip frame processing."""
        if upstream.transport.connect_pending:
            result = upstream.transport.check_connect()
            if result is True:
                upstream.connected = True
                upstream._reconnecting = True
                upstream._restart_detected = False
                upstream._reconnect_delay = 1.0
                upstream._reconnect_cooldown = 0.0
                self._start_discovery(upstream)
                self._logger.info(
                    f"Upstream '{upstream.device_name}' TCP connected, "
                    f"awaiting discovery"
                )
            elif result is False:
                upstream._reconnect_delay = min(
                    upstream._reconnect_delay * 2, 30.0
                )
                upstream._reconnect_cooldown = (
                    time.time() + upstream._reconnect_delay
                )
                self._logger.debug(
                    f"Upstream '{upstream.device_name}' reconnect "
                    f"attempt failed, next in "
                    f"{upstream._reconnect_delay:.0f}s"
                )
            return True

        if not upstream.transport.connected:
            if upstream.connected:
                self._handle_upstream_disconnect(upstream)

            if upstream.auto_reconnect:
                now = time.time()
                if now >= upstream._reconnect_cooldown:
                    upstream.transport.start_connect(timeout=5.0)
                    if (
                        not upstream.transport.connect_pending
                        and not upstream.transport.connected
                    ):
                        upstream._reconnect_delay = min(
                            upstream._reconnect_delay * 2, 30.0
                        )
                    upstream._reconnect_cooldown = (
                        now + upstream._reconnect_delay
                    )
            return True

        return False

    # ── Data processing ──────────────────────────────────────────────

    def _process_upstream_data(
        self,
        upstream: UpstreamDevice,
        frame: bytes,
        msg_key: int,
    ) -> None:
        """Parse and process a single data frame from an upstream device."""
        if upstream.schema_stale:
            return

        try:
            decoded = decoder.parse_data(frame, upstream.symbol_table)

            if not self._validate_upstream_schema(
                upstream, decoded, msg_key
            ):
                return

            if decoded.restart_flag:
                if upstream._restart_c0_sent:
                    upstream._restart_c0_sent = False
                    self._logger.debug(
                        f"Upstream '{upstream.device_name}' restart "
                        f"flag consumed (0xC0 already sent)"
                    )
                else:
                    self._logger.info(
                        f"Upstream '{upstream.device_name}' restart "
                        f"flag relayed via 0xC0"
                    )
                    self._send_upstream_restarted_frame(upstream)
                    self._replay_custom_commands(upstream)
                    self._resend_activate(upstream)

            if not upstream.relay_downstream:
                for sig_id, value in decoded.signals.items():
                    idx = upstream.index_map.get(sig_id)
                    if idx is not None and idx < len(upstream._signals):
                        upstream._signals[idx].value = value
                try:
                    self._server._fire_data_received(upstream)
                except Exception as e:
                    self._logger.warning(
                        f"on_data_received callback error for "
                        f"'{upstream.device_name}': {e}"
                    )
                return

            self._relay_upstream_data(upstream, decoded)

        except (ValueError, IndexError, UnicodeDecodeError) as e:
            self._logger.warning(
                f"Upstream '{upstream.device_name}' frame dropped: {e}"
            )

    def _validate_upstream_schema(
        self,
        upstream: UpstreamDevice,
        decoded: decoder.DecodedData,
        msg_key: int,
    ) -> bool:
        """Check schema hash or signal count. Returns False if stale."""
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
            return False

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
            return False

        return True

    def _relay_upstream_data(
        self, upstream: UpstreamDevice, decoded: decoder.DecodedData
    ) -> None:
        """Update hub signals from decoded data and relay to downstream."""
        signals = self._server.signals
        for sig_id, value in decoded.signals.items():
            hub_idx = upstream.index_map.get(sig_id)
            if hub_idx is not None and hub_idx < len(signals):
                signals[hub_idx].value = value
                signals[hub_idx].updated = True

        try:
            self._server._fire_data_received(upstream)
        except Exception as e:
            self._logger.warning(
                f"on_data_received callback error for "
                f"'{upstream.device_name}': {e}"
            )

        relayed_count = sum(
            1 for u in self._upstreams if u.relay_downstream
        )
        single = (
            relayed_count == 1
            and self._server._local_signal_count == 0
        )
        ts = (
            decoded.timestamp
            if single and decoded.timestamp is not None
            else None
        )
        if not single and decoded.timestamp is not None:
            if not upstream._ts_relay_warned:
                upstream._ts_relay_warned = True
                self._logger.info(
                    f"Upstream '{upstream.device_name}' sends timestamps, "
                    f"but they are not forwarded to clients — multiple "
                    f"sources may use different clocks. Data is still "
                    f"forwarded without timestamps."
                )
        ts_mode = decoded.timestamp_mode if ts is not None else None

        relay_msg_id = decoded.msg_id
        if upstream.interval_ms >= 0 and relay_msg_id == _MSG_ID_ACTIVATE:
            relay_msg_id = _MSG_ID_HUB

        hub_indices = sorted(upstream.index_map.values())
        if not hub_indices:
            return
        start_idx = hub_indices[0]
        end_idx = hub_indices[-1]
        header = (
            self._server.MSG_DATA
            + b":"
            + relay_msg_id.to_bytes(4, "little")
            + b":"
        )
        relay_data = (
            b"<BLAECK:"
            + self._server._build_data_msg(
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
        self._server._tcp_send_data(relay_data)

    # ── Signal management ────────────────────────────────────────────

    def _zero_upstream_signals(self, upstream: UpstreamDevice) -> None:
        """Reset all signals from a disconnected upstream to zero."""
        if upstream.relay_downstream:
            signals = self._server.signals
            for hub_idx in upstream.index_map.values():
                if hub_idx < len(signals):
                    signals[hub_idx].value = 0
                    signals[hub_idx].updated = True
        else:
            for sig in upstream._signals:
                sig.value = 0

    def _rebuild_upstream_schema(
        self,
        upstream: UpstreamDevice,
        new_symbols: list[decoder.DecodedSymbol],
    ) -> None:
        """Rebuild an upstream's signals after schema change."""
        upstream.symbol_table = new_symbols
        upstream.expected_schema_hash = decoder.compute_schema_hash(
            [(s.name, s.datatype_code) for s in new_symbols]
        )

        signals = self._server.signals
        del signals[self._server._local_signal_count :]

        offset = self._server._local_signal_count
        for u in self._upstreams:
            u._signals = []
            u.index_map = {}
            if u.relay_downstream:
                for i, sym in enumerate(u.symbol_table):
                    sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(
                        sym.datatype_code, "float"
                    )
                    sig = Signal(sym.name, sig_type)
                    signals.append(sig)
                    u._signals.append(signals[offset])
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

        self._server._update_schema_hash()

    def _rebuild_slave_id_map(self, upstream: UpstreamDevice) -> None:
        """Rebuild slave_id_map for one upstream from its current data."""
        if not upstream.relay_downstream:
            return
        hub_slave_idx = 0
        for u in self._upstreams:
            if (
                u is not upstream
                and u.relay_downstream
                and u.slave_id_map
            ):
                hub_slave_idx = max(
                    hub_slave_idx, max(u.slave_id_map.values())
                )
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

    def _rebuild_upstream_indices(self) -> None:
        """Rebuild relayed upstream index_map from current signals list."""
        offset = self._server._local_signal_count
        for upstream in self._upstreams:
            if upstream.relay_downstream:
                for k in range(len(upstream._signals)):
                    upstream.index_map[k] = offset + k
                offset += len(upstream._signals)

    # ── Status frames ────────────────────────────────────────────────

    def _send_upstream_lost_frame(
        self, upstream: UpstreamDevice
    ) -> None:
        """Send one data frame with STATUS_UPSTREAM_LOST."""
        if not upstream.relay_downstream or not self._server.connected:
            return
        hub_indices = sorted(upstream.index_map.values())
        if hub_indices:
            start_idx = hub_indices[0]
            end_idx = hub_indices[-1]
        else:
            start_idx = 0
            end_idx = -2  # empty range: range(0, -1) produces no signals
        auto_reconnect_byte = (
            b"\x01" if upstream.auto_reconnect else b"\x00"
        )
        status_payload = auto_reconnect_byte + b"\x00\x00\x00"
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
                status_payload=status_payload,
            )
            + b"/BLAECK>\r\n"
        )
        self._server._tcp_send_data(data)

    def _send_upstream_reconnected_frame(
        self, upstream: UpstreamDevice
    ) -> None:
        """Send one data frame with STATUS_UPSTREAM_RECONNECTED."""
        if not upstream.relay_downstream or not self._server.connected:
            return
        hub_indices = sorted(upstream.index_map.values())
        signals = self._server.signals
        for hub_idx in hub_indices:
            signals[hub_idx].updated = True
        if hub_indices:
            start_idx = hub_indices[0]
            end_idx = hub_indices[-1]
        else:
            start_idx = 0
            end_idx = -2
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
                status=STATUS_UPSTREAM_RECONNECTED,
            )
            + b"/BLAECK>\r\n"
        )
        self._server._tcp_send_data(data)

    def _send_upstream_restarted_frame(
        self, upstream: UpstreamDevice
    ) -> None:
        """Build and send a restart frame with upstream's device name."""
        if not self._server.connected:
            return
        info = (
            upstream.device_infos[0] if upstream.device_infos else None
        )
        name = (
            info.device_name if info else upstream.device_name
        ).encode()
        hw = info.hw_version.encode() if info else b""
        fw = info.fw_version.encode() if info else b""
        lib_ver = info.lib_version.encode() if info else b""
        lib_name = info.lib_name.encode() if info else b""
        payload = (
            bytes([decoder.MSGKEY_RESTART])
            + b":\x01\x00\x00\x00:"
            + MSC_SLAVE
            + b"\x01"
            + name
            + b"\0"
            + hw
            + b"\0"
            + fw
            + b"\0"
            + lib_ver
            + b"\0"
            + lib_name
            + b"\0"
        )
        data = b"<BLAECK:" + payload + b"/BLAECK>\r\n"
        self._server._tcp_send_data(data)
        upstream._restart_c0_sent = True

    # ── Reconnection ─────────────────────────────────────────────────

    def _resend_activate(self, upstream: UpstreamDevice) -> None:
        """Re-send ACTIVATE/DEACTIVATE after upstream restart or reconnect."""
        if upstream.interval_ms >= 0:
            b = upstream.interval_ms.to_bytes(4, "little")
            params = ",".join(str(x) for x in b)
            if not upstream.transport.send_command(f"BLAECK.ACTIVATE,{params}"):
                self._logger.warning(
                    f"Upstream '{upstream.device_name}' failed to resend ACTIVATE"
                )
            else:
                self._logger.info(
                    f"Upstream '{upstream.device_name}' interval restored: "
                    f"{upstream.interval_ms} ms"
                )
        elif upstream.interval_ms == IntervalMode.OFF:
            if not upstream.transport.send_command("BLAECK.DEACTIVATE"):
                self._logger.warning(
                    f"Upstream '{upstream.device_name}' failed to resend DEACTIVATE"
                )
        elif (
            upstream.interval_ms == IntervalMode.CLIENT
            and self._server._last_client_activate_cmd
        ):
            if not upstream.transport.send_command(
                self._server._last_client_activate_cmd
            ):
                self._logger.warning(
                    f"Upstream '{upstream.device_name}' failed to resend activate command"
                )
            else:
                self._logger.info(
                    f"Upstream '{upstream.device_name}' interval restored: client controlled"
                )

    def _replay_custom_commands(self, upstream: UpstreamDevice) -> None:
        """Replay stored custom commands after upstream restart or reconnect."""
        if not upstream.replay_commands or not upstream.transport.connected:
            return
        fcc = upstream.forward_custom_commands
        replayed = []
        failed = []
        for command in upstream.replay_commands:
            full_cmd = self._server._last_custom_commands.get(command)
            if full_cmd is None:
                continue
            if isinstance(fcc, list) and command not in fcc:
                continue
            if not fcc:
                continue
            if upstream.transport.send_command(full_cmd):
                replayed.append(f"<{full_cmd}>")
            else:
                failed.append(command)
        if replayed:
            self._logger.info(
                f"Upstream '{upstream.device_name}' replayed: "
                + ", ".join(replayed)
            )
        for command in failed:
            self._logger.warning(
                f"Upstream '{upstream.device_name}' failed to replay {command}"
            )

    def _handle_upstream_disconnect(
        self, upstream: UpstreamDevice
    ) -> None:
        """Reset upstream state on disconnect and notify downstream."""
        if upstream.auto_reconnect:
            self._logger.warning(
                f"Upstream '{upstream.device_name}' disconnected, reconnecting..."
            )
        else:
            self._logger.warning(
                f"Upstream '{upstream.device_name}' disconnected"
            )
        upstream.connected = False
        upstream._reconnecting = False
        upstream._awaiting_symbols = False
        upstream._awaiting_devices = False
        upstream._restart_detected = False
        upstream._discovery_retry_at = 0.0
        self._zero_upstream_signals(upstream)
        self._send_upstream_lost_frame(upstream)
        if self._server._upstream_disconnect_callback is not None:
            self._server._upstream_disconnect_callback(
                upstream.device_name
            )

    def _start_discovery(self, upstream: UpstreamDevice) -> None:
        """Begin discovery Phase 1: send DEACTIVATE + WRITE_SYMBOLS."""
        upstream.transport.send_command("BLAECK.DEACTIVATE")
        upstream.transport.send_command("BLAECK.WRITE_SYMBOLS")
        upstream._awaiting_symbols = True
        upstream._discovery_retry_at = time.time() + 1.0

    def _handle_symbol_list(
        self, upstream: UpstreamDevice, frame: bytes
    ) -> None:
        """Handle a symbol list frame during reconnect or schema refresh."""
        new_symbols = None
        try:
            new_symbols = decoder.parse_symbol_list(frame)
            if new_symbols is not None:
                self._rebuild_upstream_schema(upstream, new_symbols)
                self._rebuild_slave_id_map(upstream)
                if upstream.schema_stale:
                    upstream.transport.send_command(
                        f"BLAECK.GET_DEVICES{self._hub_identity}"
                    )
                upstream.schema_stale = False
                if not upstream._awaiting_symbols:
                    self._logger.info(
                        f"Schema refreshed for '{upstream.device_name}': "
                        f"{len(new_symbols)} signals"
                    )
        except (ValueError, IndexError, UnicodeDecodeError) as e:
            self._logger.warning(
                f"Schema re-discovery failed for "
                f"'{upstream.device_name}': {e}"
            )
        if not upstream._awaiting_symbols:
            return
        if new_symbols is not None:
            upstream._awaiting_symbols = False
            upstream.transport.send_command(
                f"BLAECK.GET_DEVICES{self._hub_identity}"
            )
            upstream._awaiting_devices = True
            upstream._discovery_retry_at = time.time() + 1.0

    def _handle_device_info(
        self, upstream: UpstreamDevice, frame: bytes
    ) -> None:
        """Handle a device info frame during reconnect or runtime."""
        infos = None
        try:
            infos = decoder.parse_all_devices(frame)
            if infos:
                upstream.device_infos = infos
                self._rebuild_slave_id_map(upstream)
                for info in infos:
                    if info.server_restarted == "1":
                        self._logger.info(
                            f"Upstream '{upstream.device_name}' "
                            f"restart detected via device info"
                        )
                        if upstream._awaiting_devices:
                            upstream._restart_detected = True
                        else:
                            self._send_upstream_restarted_frame(
                                upstream
                            )
                            self._resend_activate(upstream)
        except (ValueError, IndexError, UnicodeDecodeError) as e:
            self._logger.warning(
                f"Device info processing for "
                f"'{upstream.device_name}': {e}"
            )
        if not upstream._awaiting_devices:
            return
        if infos is not None:
            upstream._awaiting_devices = False
            if upstream._reconnecting:
                self._finalize_reconnect(
                    upstream, upstream._restart_detected
                )
                upstream._restart_detected = False

    def _retry_discovery(self, upstream: UpstreamDevice) -> None:
        """Retry the current discovery command if response hasn't arrived."""
        if not (
            upstream._awaiting_symbols or upstream._awaiting_devices
        ):
            return
        now = time.time()
        if now >= upstream._discovery_retry_at:
            upstream._discovery_retry_at = now + 1.0
            if upstream._awaiting_symbols:
                upstream.transport.send_command("BLAECK.WRITE_SYMBOLS")
            elif upstream._awaiting_devices:
                upstream.transport.send_command(
                    f"BLAECK.GET_DEVICES{self._hub_identity}"
                )
            self._logger.debug(
                f"Upstream '{upstream.device_name}' discovery retry"
            )

    def _finalize_reconnect(
        self,
        upstream: UpstreamDevice,
        restart_detected: bool = False,
    ) -> None:
        """Complete reconnect: notify downstream, re-send ACTIVATE."""
        upstream._reconnecting = False
        self._send_upstream_reconnected_frame(upstream)
        self._logger.info(
            f"Upstream '{upstream.device_name}' reconnected"
        )
        if restart_detected:
            self._send_upstream_restarted_frame(upstream)
        self._replay_custom_commands(upstream)
        self._resend_activate(upstream)

    # ── Helpers ──────────────────────────────────────────────────────

    @property
    def _hub_identity(self) -> str:
        """Identity suffix for GET_DEVICES commands."""
        return (
            f",0,0,0,0,{self._server._device_name.decode()},hub"
        )
