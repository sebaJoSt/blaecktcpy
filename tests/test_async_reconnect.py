"""Tests for non-blocking async reconnect in _poll_upstreams."""

import socket
import time

import pytest

from blaecktcpy import BlaeckTCPy, Signal, SignalList
from blaecktcpy._server import _UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import (
    _make_server_on_free_port,
    _start_retry,
    FakeTransport,
    RecordingTransport,
)


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def _build_b0_frame(symbols: list[tuple[str, int]]):
    """Build a B0 symbol list frame content (between markers)."""
    msg_key = b"\xb0"
    msg_id = (1).to_bytes(4, "little")
    payload = b""
    for name, dtype_code in symbols:
        payload += b"\x00\x00" + name.encode("utf-8") + b"\x00" + bytes([dtype_code])
    return msg_key + b":" + msg_id + b":" + payload


def _build_b6_frame(
    device_name: str = "ESP32",
    hw: str = "HW",
    fw: str = "1.0",
    lib_ver: str = "2.0.0",
    lib_name: str = "blaecktcpy",
    server_restarted: str = "0",
):
    """Build a B6 device info frame content (between markers)."""
    msg_key = b"\xb6"
    msg_id = (1).to_bytes(4, "little")
    payload = (
        b"\x00\x00"  # MSC=0, SlaveID=0
        + device_name.encode() + b"\x00"
        + hw.encode() + b"\x00"
        + fw.encode() + b"\x00"
        + lib_ver.encode() + b"\x00"
        + lib_name.encode() + b"\x00"
        + b"0\x00"  # assigned_client_id
        + b"1\x00"  # data_enabled
        + b"\x00"  # client_name (empty)
        + b"\x00"  # client_type (empty -> "unknown")
        + server_restarted.encode() + b"\x00"
        + b"server\x00"  # device_type
        + b"0\x00"  # parent
    )
    return msg_key + b":" + msg_id + b":" + payload


def _make_hub_with_reconnectable_upstream(
    symbols: list[tuple[str, int]],
    transport_cls=RecordingTransport,
    device_name: str = "ESP32",
):
    """Create a started hub with one auto_reconnect upstream."""
    device = _make_server_on_free_port()
    _start_retry(device)

    transport = transport_cls(device_name)
    upstream = _UpstreamDevice(
        device_name=device_name,
        transport=transport,
        relay_downstream=True,
        auto_reconnect=True,
    )

    sym_objs = [
        decoder.DecodedSymbol(
            name, code, decoder._DTYPE_INFO[code][0], decoder._DTYPE_INFO[code][1]
        )
        for name, code in symbols
    ]
    upstream.symbol_table = sym_objs
    upstream.expected_schema_hash = decoder.compute_schema_hash(symbols)
    upstream._initial_restart_seen = True

    offset = device._local_signal_count
    for i, (name, code) in enumerate(symbols):
        sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(code, "float")
        sig = Signal(name, sig_type)
        device.signals.append(sig)
        upstream._signals.append(device.signals[offset])
        upstream.index_map[i] = offset
        offset += 1
    upstream._upstream_signals = SignalList(upstream._signals)
    upstream.connected = True

    device._upstreams.append(upstream)
    device._update_schema_hash()

    return device, upstream, transport


# ---------------------------------------------------------------------------
# State machine unit tests (FakeTransport / RecordingTransport)
# ---------------------------------------------------------------------------

class TestAsyncReconnectStateMachine:
    """Verify the non-blocking reconnect state machine transitions."""

    def test_disconnect_starts_async_connect(self):
        """When an upstream disconnects, start_connect is called (not blocking connect)."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            # Simulate disconnect
            transport._connected = False
            upstream._reconnect_cooldown = 0  # bypass cooldown

            device._poll_upstreams()

            assert upstream.connected is False
            assert transport.connect_pending is True
        finally:
            device.close()

    def test_connect_pending_continues_without_blocking(self):
        """While connect is pending, tick returns immediately."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            transport._connected = False
            upstream.connected = False
            transport._connect_pending = True

            t0 = time.monotonic()
            device._poll_upstreams()
            elapsed = time.monotonic() - t0

            assert elapsed < 0.1
            # Still pending
            assert transport.connect_pending is True
        finally:
            device.close()

    def test_connect_success_sends_discovery_commands(self):
        """After async connect succeeds, DEACTIVATE + WRITE_SYMBOLS are sent (Phase 1)."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            # Simulate: transport just connected
            transport._connected = False
            upstream.connected = False
            transport._connect_pending = True

            # Resolve the connect
            transport.complete_connect(success=True)

            device._poll_upstreams()

            assert upstream.connected is True
            assert upstream._reconnecting is True
            assert upstream._awaiting_symbols is True
            assert upstream._awaiting_devices is False  # Phase 1: only symbols

            # Verify commands sent (only DEACTIVATE + WRITE_SYMBOLS, not GET_DEVICES yet)
            sent_cmds = [s.decode(errors="replace") for s in transport.sent]
            assert any("BLAECK.DEACTIVATE" in c for c in sent_cmds)
            assert any("BLAECK.WRITE_SYMBOLS" in c for c in sent_cmds)
            assert not any("BLAECK.GET_DEVICES" in c for c in sent_cmds)
        finally:
            device.close()

    def test_connect_failure_resets_state(self):
        """If async connect fails, upstream stays disconnected."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            transport._connected = False
            upstream.connected = False
            transport._connect_pending = True

            transport.complete_connect(success=False)

            device._poll_upstreams()

            assert upstream.connected is False
            assert upstream._reconnecting is False
        finally:
            device.close()

    def test_b0_and_b6_complete_reconnect(self):
        """Injecting B0 and B6 frames completes the reconnect cycle."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            # Put upstream in awaiting state (as if connect just succeeded)
            upstream._reconnecting = True
            upstream._awaiting_symbols = True
            upstream._awaiting_devices = True

            # Inject B0 (symbol list)
            b0 = _build_b0_frame([("temp", 8)])
            transport.inject_frame(b0)
            device._poll_upstreams()

            assert upstream._awaiting_symbols is False
            # Still waiting for B6
            assert upstream._reconnecting is True

            # Inject B6 (device info)
            b6 = _build_b6_frame()
            transport.inject_frame(b6)
            device._poll_upstreams()

            assert upstream._awaiting_devices is False
            assert upstream._reconnecting is False  # finalized
        finally:
            device.close()

    def test_sequential_phases_b0_then_b6(self):
        """B0 triggers GET_DEVICES (Phase 2), then B6 finalizes (Phase 3)."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            upstream._reconnecting = True
            upstream._awaiting_symbols = True

            # Phase 1 → Phase 2: B0 arrives
            b0 = _build_b0_frame([("temp", 8)])
            transport.inject_frame(b0)
            device._poll_upstreams()

            assert upstream._awaiting_symbols is False
            assert upstream._awaiting_devices is True  # GET_DEVICES sent
            assert upstream._reconnecting is True
            # Verify GET_DEVICES was sent
            sent_cmds = [s.decode(errors="replace") for s in transport.sent]
            assert any("BLAECK.GET_DEVICES" in c for c in sent_cmds)

            # Phase 2 → Phase 3: B6 arrives
            b6 = _build_b6_frame()
            transport.inject_frame(b6)
            device._poll_upstreams()

            assert upstream._awaiting_devices is False
            assert upstream._reconnecting is False  # finalized
        finally:
            device.close()

    def test_restart_detected_during_reconnect(self):
        """server_restarted=1 in B6 is deferred to _finalize_reconnect."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            upstream._reconnecting = True
            upstream._awaiting_symbols = True

            # Phase 1: B0 arrives → Phase 2
            b0 = _build_b0_frame([("temp", 8)])
            transport.inject_frame(b0)
            device._poll_upstreams()

            # Phase 2: B6 with server_restarted=1
            b6 = _build_b6_frame(server_restarted="1")
            transport.inject_frame(b6)
            device._poll_upstreams()

            assert upstream._reconnecting is False
            # _restart_detected should have been cleared after finalize
            assert upstream._restart_detected is False
        finally:
            device.close()

    def test_full_disconnect_reconnect_cycle(self):
        """Full cycle: connected → disconnect → async connect → Phase 1 → Phase 2 → reconnected."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            # 1. Disconnect
            transport._connected = False
            upstream._reconnect_cooldown = 0

            device._poll_upstreams()
            assert upstream.connected is False
            assert transport.connect_pending is True

            # 2. Connect completes → Phase 1 (awaiting symbols)
            transport.complete_connect(success=True)
            device._poll_upstreams()
            assert upstream.connected is True
            assert upstream._awaiting_symbols is True
            assert upstream._awaiting_devices is False

            # 3. B0 arrives → Phase 2 (awaiting devices)
            b0 = _build_b0_frame([("temp", 8)])
            transport.inject_frame(b0)
            device._poll_upstreams()
            assert upstream._awaiting_symbols is False
            assert upstream._awaiting_devices is True

            # 4. B6 arrives → Phase 3 (finalized)
            b6 = _build_b6_frame()
            transport.inject_frame(b6)
            device._poll_upstreams()

            assert upstream._reconnecting is False
            assert upstream._awaiting_symbols is False
            assert upstream._awaiting_devices is False
            assert upstream.connected is True
        finally:
            device.close()


class TestAsyncReconnectTiming:
    """Verify that reconnect does not block the event loop."""

    def test_tick_does_not_block_during_reconnect(self):
        """tick() should return quickly even when upstream is reconnecting."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            # Simulate disconnect + start reconnect
            transport._connected = False
            upstream.connected = False
            transport._connect_pending = True

            t0 = time.monotonic()
            for _ in range(10):
                device._poll_upstreams()
            elapsed = time.monotonic() - t0

            # 10 polls should take < 0.5s (no time.sleep)
            assert elapsed < 0.5, f"10 polls took {elapsed:.2f}s — likely blocking"
        finally:
            device.close()

    def test_tick_does_not_block_full_cycle(self):
        """Full reconnect cycle via tick() completes without blocking."""
        device, upstream, transport = _make_hub_with_reconnectable_upstream(
            [("temp", 8)]
        )
        try:
            transport._connected = False
            upstream._reconnect_cooldown = 0

            t0 = time.monotonic()

            # Tick 1: detect disconnect, start connect
            device._poll_upstreams()

            # Tick 2: connect completes → Phase 1
            transport.complete_connect(success=True)
            device._poll_upstreams()

            # Tick 3: B0 arrives → Phase 2
            b0 = _build_b0_frame([("temp", 8)])
            transport.inject_frame(b0)
            device._poll_upstreams()

            # Tick 4: B6 arrives → Phase 3 (finalized)
            b6 = _build_b6_frame()
            transport.inject_frame(b6)
            device._poll_upstreams()

            elapsed = time.monotonic() - t0
            assert elapsed < 0.5, f"Full reconnect took {elapsed:.2f}s"
            assert upstream._reconnecting is False
            assert upstream.connected is True
        finally:
            device.close()


# ---------------------------------------------------------------------------
# Integration tests (real TCP loopback)
# ---------------------------------------------------------------------------

class TestAsyncReconnectIntegration:
    """Integration tests using real TCP sockets (BlaeckTCPy-as-upstream)."""

    def _make_upstream_server(self, port=0):
        """Create a BlaeckTCPy that acts as an upstream device."""
        server = BlaeckTCPy("127.0.0.1", port, "ESP32", "HW", "1.0")
        server.add_signal("temp", "float")
        server.add_signal("humidity", "float")
        server.start()
        actual_port = server._server_socket.getsockname()[1]
        return server, actual_port

    @staticmethod
    def _run_server_loop(server, stop_event):
        """Tick a BlaeckTCPy server in a background thread until stopped."""
        while not stop_event.is_set():
            try:
                server.read()
            except Exception:
                if stop_event.is_set():
                    break
                continue
            stop_event.wait(0.005)

    def test_reconnect_with_real_tcp(self):
        """Full reconnect cycle over real TCP loopback sockets."""
        import threading

        upstream_server, port = self._make_upstream_server()
        stop = threading.Event()
        t = threading.Thread(
            target=self._run_server_loop, args=(upstream_server, stop), daemon=True
        )
        t.start()

        hub = BlaeckTCPy("127.0.0.1", 0, "Hub", "HW", "1.0")
        try:
            hub.add_tcp("127.0.0.1", port, name="ESP32", auto_reconnect=True)
            hub.start()
            hub_upstream = hub._upstreams[0]

            assert hub_upstream.connected is True
            assert len(hub_upstream.symbol_table) == 2

            # Simulate disconnect from the hub side (close the transport)
            hub_upstream.transport.close()

            # Hub detects disconnect
            hub._poll_upstreams()

            assert hub_upstream.connected is False

            # Bypass cooldown and let async reconnect proceed
            hub_upstream._reconnect_cooldown = 0

            # Poll until reconnected (up to 3 seconds)
            deadline = time.time() + 3.0
            while hub_upstream._reconnecting or not hub_upstream.connected:
                hub._poll_upstreams()
                time.sleep(0.01)
                if time.time() > deadline:
                    break

            assert hub_upstream.connected is True
            assert hub_upstream._reconnecting is False
            assert len(hub_upstream.symbol_table) == 2

        finally:
            stop.set()
            t.join(timeout=2)
            hub.close()
            upstream_server.close()

    def test_other_upstream_served_during_reconnect(self):
        """A second upstream continues getting polled while one is reconnecting."""
        import threading

        upstream_a, port_a = self._make_upstream_server()
        upstream_b, port_b = self._make_upstream_server()
        stop = threading.Event()
        t_a = threading.Thread(
            target=self._run_server_loop, args=(upstream_a, stop), daemon=True
        )
        t_b = threading.Thread(
            target=self._run_server_loop, args=(upstream_b, stop), daemon=True
        )
        t_a.start()
        t_b.start()

        hub = BlaeckTCPy("127.0.0.1", 0, "Hub", "HW", "1.0")
        try:
            hub.add_tcp("127.0.0.1", port_a, name="DevA", auto_reconnect=True)
            hub.add_tcp("127.0.0.1", port_b, name="DevB")
            hub.start()

            up_a = hub._upstreams[0]
            up_b = hub._upstreams[1]

            assert up_a.connected is True
            assert up_b.connected is True

            # Disconnect upstream A from hub side
            up_a.transport.close()

            hub._poll_upstreams()

            # A is disconnected (or reconnecting), B still connected
            assert up_a.connected is False
            assert up_b.connected is True

        finally:
            stop.set()
            t_a.join(timeout=2)
            t_b.join(timeout=2)
            hub.close()
            upstream_a.close()
            upstream_b.close()
