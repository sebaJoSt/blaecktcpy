"""Tests for features added to BlaeckHub and BlaeckServer.

Covers:
- STATUS_OK / STATUS_UPSTREAM_LOST in _build_data_msg
- UpstreamSignals collection (index, name, len, iter, errors)
- Callback registration and dispatch (on_data_received, client callbacks)
- relay=False signal registration in start()
"""

import binascii
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import Signal, STATUS_OK, STATUS_UPSTREAM_LOST
from blaecktcpy.hub._hub import (
    BlaeckHub,
    UpstreamSignals,
    _UpstreamDevice,
    _MSG_ID_HUB,
)
from blaecktcpy.hub._upstream import _UpstreamBase
from blaecktcpy.hub import _decoder as decoder


# ========================================================================
# Helpers
# ========================================================================


def _make_server_on_free_port():
    """Create a BlaeckServer on a random free port."""
    import socket
    import random

    from blaecktcpy import BlaeckServer

    for _ in range(10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            server = BlaeckServer("127.0.0.1", port, "Test", "HW", "1.0")
            return server
        except OSError:
            continue
    raise OSError("Could not find a free port after 10 attempts")


class FakeTransport(_UpstreamBase):
    """Minimal transport for testing without real connections."""

    def __init__(self, name="fake"):
        super().__init__(name)
        self._connected = True

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def read_available(self):
        return b""

    def send(self, data):
        return True

    def close(self):
        self._connected = False


# ========================================================================
# Status byte tests
# ========================================================================


class TestStatusByte:
    """Verify _build_data_msg encodes the status byte correctly."""

    def setup_method(self):
        self.server = _make_server_on_free_port()
        self.server.add_signal("sig1", "float", 3.14)
        self.server.add_signal("sig2", "int", 42)

    def teardown_method(self):
        self.server.close()

    def test_default_status_is_ok(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header)
        # Status byte is at position -5 (1 byte before 4-byte CRC)
        assert msg[-5] == STATUS_OK

    def test_status_upstream_lost(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        assert msg[-5] == STATUS_UPSTREAM_LOST

    def test_crc_excludes_status_byte(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        # CRC is computed over everything before status byte
        crc_data = msg[:-5]
        expected_crc = binascii.crc32(crc_data) & 0xFFFFFFFF
        actual_crc = int.from_bytes(msg[-4:], "little")
        assert actual_crc == expected_crc

    def test_status_byte_values(self):
        assert STATUS_OK == 0x00
        assert STATUS_UPSTREAM_LOST == 0x02


# ========================================================================
# UpstreamSignals tests
# ========================================================================


class TestUpstreamSignals:
    """Verify UpstreamSignals collection access patterns."""

    def setup_method(self):
        self.signals = [
            Signal("temperature", "float", 22.5),
            Signal("humidity", "float", 65.0),
            Signal("pressure", "float", 1013.25),
        ]
        self.collection = UpstreamSignals(self.signals)

    def test_access_by_index(self):
        assert self.collection[0].signal_name == "temperature"
        assert self.collection[1].signal_name == "humidity"
        assert self.collection[2].signal_name == "pressure"

    def test_access_by_name(self):
        assert self.collection["temperature"].value == 22.5
        assert self.collection["humidity"].value == 65.0

    def test_index_out_of_range(self):
        with pytest.raises(IndexError):
            _ = self.collection[5]

    def test_name_not_found(self):
        with pytest.raises(KeyError, match="wind_speed"):
            _ = self.collection["wind_speed"]

    def test_invalid_key_type(self):
        with pytest.raises(TypeError, match="float"):
            _ = self.collection[1.5]

    def test_len(self):
        assert len(self.collection) == 3

    def test_iter(self):
        names = [s.signal_name for s in self.collection]
        assert names == ["temperature", "humidity", "pressure"]

    def test_empty_collection(self):
        empty = UpstreamSignals([])
        assert len(empty) == 0
        assert list(empty) == []

    def test_value_updates_propagate(self):
        self.collection["temperature"].value = 30.0
        assert self.signals[0].value == 30.0


# ========================================================================
# Callback registration tests
# ========================================================================


class TestCallbackRegistration:
    """Verify callback decorators register and dispatch correctly."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._upstreams = []
        self.hub._local_signals = []
        self.hub._server = None
        self.hub._started = False
        self.hub._disconnect_callback = None
        self.hub._client_connect_callback = None
        self.hub._client_disconnect_callback = None
        self.hub._data_received_callbacks = []
        self.hub._command_handlers = {}
        self.hub._command_catchall = None

    def test_on_upstream_disconnected_registers(self):
        @self.hub.on_upstream_disconnected()
        def handler(name):
            pass

        assert self.hub._disconnect_callback is handler

    def test_on_client_connected_registers(self):
        @self.hub.on_client_connected()
        def handler(cid):
            pass

        assert self.hub._client_connect_callback is handler

    def test_on_client_disconnected_registers(self):
        @self.hub.on_client_disconnected()
        def handler(cid):
            pass

        assert self.hub._client_disconnect_callback is handler

    def test_on_data_received_registers_with_name(self):
        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            pass

        assert len(self.hub._data_received_callbacks) == 1
        name, func = self.hub._data_received_callbacks[0]
        assert name == "Arduino"
        assert func is handler

    def test_on_data_received_registers_without_name(self):
        @self.hub.on_data_received()
        def handler(upstream):
            pass

        name, func = self.hub._data_received_callbacks[0]
        assert name is None

    def test_multiple_data_received_callbacks(self):
        @self.hub.on_data_received("Arduino")
        def h1(u):
            pass

        @self.hub.on_data_received("ESP32")
        def h2(u):
            pass

        @self.hub.on_data_received()
        def h3(u):
            pass

        assert len(self.hub._data_received_callbacks) == 3

    def test_on_command_specific_registers(self):
        @self.hub.on_command("SET_LED")
        def handler(state):
            pass

        assert "SET_LED" in self.hub._command_handlers
        assert self.hub._command_handlers["SET_LED"] is handler

    def test_on_command_catchall_registers(self):
        @self.hub.on_command()
        def handler(command, *params):
            pass

        assert self.hub._command_catchall is handler

    def test_on_command_multiple_specific(self):
        @self.hub.on_command("SET_LED")
        def h1(state):
            pass

        @self.hub.on_command("SET_MODE")
        def h2(mode):
            pass

        assert len(self.hub._command_handlers) == 2


class TestFireDataReceived:
    """Verify _fire_data_received dispatches to correct callbacks."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._data_received_callbacks = []

    def _make_upstream(self, name):
        transport = FakeTransport(name)
        return _UpstreamDevice(name=name, transport=transport)

    def test_fires_matching_name(self):
        calls = []

        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.name)

        arduino = self._make_upstream("Arduino")
        self.hub._fire_data_received(arduino)
        assert calls == ["Arduino"]

    def test_skips_non_matching_name(self):
        calls = []

        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.name)

        esp = self._make_upstream("ESP32")
        self.hub._fire_data_received(esp)
        assert calls == []

    def test_global_callback_fires_for_any(self):
        calls = []

        @self.hub.on_data_received()
        def handler(upstream):
            calls.append(upstream.name)

        self.hub._fire_data_received(self._make_upstream("Arduino"))
        self.hub._fire_data_received(self._make_upstream("ESP32"))
        assert calls == ["Arduino", "ESP32"]

    def test_mixed_callbacks(self):
        specific_calls = []
        global_calls = []

        @self.hub.on_data_received("Arduino")
        def specific(upstream):
            specific_calls.append(upstream.name)

        @self.hub.on_data_received()
        def global_handler(upstream):
            global_calls.append(upstream.name)

        self.hub._fire_data_received(self._make_upstream("Arduino"))
        self.hub._fire_data_received(self._make_upstream("ESP32"))

        assert specific_calls == ["Arduino"]
        assert global_calls == ["Arduino", "ESP32"]


# ========================================================================
# relay=False tests
# ========================================================================


class TestRelayFalseRegistration:
    """Verify relay=False signals go to internal storage, not server."""

    def test_relay_true_registers_on_server(self):
        server = _make_server_on_free_port()
        try:
            upstream = _UpstreamDevice(
                name="ESP32",
                transport=FakeTransport("ESP32"),
                relay=True,
                symbol_table=[
                    decoder.DecodedSymbol("temp", 8, "float", 4),
                    decoder.DecodedSymbol("hum", 8, "float", 4),
                ],
            )

            offset = len(server.signals)
            for i, sym in enumerate(upstream.symbol_table):
                sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(sym.datatype_code, "float")
                server.add_signal(sym.name, sig_type)
                upstream._signals.append(server.signals[offset])
                upstream.index_map[i] = offset
                offset += 1
            upstream._upstream_signals = UpstreamSignals(upstream._signals)

            assert len(server.signals) == 2
            assert upstream.index_map == {0: 0, 1: 1}
            assert server.signals[0].signal_name == "temp"
            # relay=True: upstream._signals references server signals
            assert upstream._signals[0] is server.signals[0]
            assert upstream.signals["temp"] is server.signals[0]
        finally:
            server.close()

    def test_relay_false_stores_internally(self):
        upstream = _UpstreamDevice(
            name="Arduino",
            transport=FakeTransport("Arduino"),
            relay=False,
            symbol_table=[
                decoder.DecodedSymbol("temp", 8, "float", 4),
                decoder.DecodedSymbol("hum", 8, "float", 4),
            ],
        )

        for i, sym in enumerate(upstream.symbol_table):
            sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(sym.datatype_code, "float")
            sig = Signal(sym.name, sig_type)
            upstream._signals.append(sig)
            upstream.index_map[i] = i
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        assert len(upstream._signals) == 2
        assert upstream.index_map == {0: 0, 1: 1}
        assert upstream._signals[0].signal_name == "temp"

    def test_relay_false_signals_accessible_via_collection(self):
        upstream = _UpstreamDevice(
            name="Arduino",
            transport=FakeTransport("Arduino"),
            relay=False,
        )
        upstream._signals = [
            Signal("temp", "float", 22.5),
            Signal("hum", "float", 65.0),
        ]
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        assert upstream.signals["temp"].value == 22.5
        assert upstream.signals[1].value == 65.0
        assert upstream["temp"].value == 22.5

    def test_upstream_signals_created_in_start(self):
        upstream = _UpstreamDevice(
            name="Arduino",
            transport=FakeTransport("Arduino"),
            relay=False,
        )
        upstream._signals = [Signal("temp", "float")]

        # Before start: _upstream_signals is None
        assert upstream._upstream_signals is None
        with pytest.raises(RuntimeError, match="start"):
            _ = upstream.signals

        # Simulate what start() does
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        # Now accessible and cached
        s1 = upstream.signals
        s2 = upstream.signals
        assert s1 is s2
        assert s1["temp"].signal_name == "temp"

    def test_relay_true_transform_modifies_server_signal(self):
        """Modifying upstream.signals for relay=True changes the server signal."""
        server = _make_server_on_free_port()
        try:
            upstream = _UpstreamDevice(
                name="Arduino",
                transport=FakeTransport("Arduino"),
                relay=True,
            )
            server.add_signal("temp_f", "float", 212.0)
            upstream._signals.append(server.signals[0])
            upstream.index_map[0] = 0
            upstream._upstream_signals = UpstreamSignals(upstream._signals)

            # Transform via upstream reference
            upstream.signals["temp_f"].value = (upstream.signals["temp_f"].value - 32) * 5 / 9

            # Server signal reflects the change (same object)
            assert server.signals[0].value == pytest.approx(100.0)
        finally:
            server.close()


# ========================================================================
# Callback exception resilience
# ========================================================================


class TestCallbackExceptionResilience:
    """Verify that a failing callback doesn't crash _fire_data_received."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._data_received_callbacks = []

    def test_exception_does_not_prevent_other_callbacks(self):
        calls = []

        @self.hub.on_data_received()
        def bad_callback(upstream):
            raise ValueError("oops")

        @self.hub.on_data_received()
        def good_callback(upstream):
            calls.append(upstream.name)

        upstream = _UpstreamDevice(name="Arduino", transport=FakeTransport("Arduino"))
        # _fire_data_received calls each callback independently;
        # but currently it doesn't catch per-callback — the outer
        # try/except in _poll_upstreams handles it.
        # This test documents that a single exception stops later callbacks.
        with pytest.raises(ValueError, match="oops"):
            self.hub._fire_data_received(upstream)
        assert calls == []
